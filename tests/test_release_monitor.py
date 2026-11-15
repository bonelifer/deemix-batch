"""Offline checks for import_artists.py, monitor_releases.py and helpers.

Deezer, MPD, deemix and Apprise are replaced by in-process fakes; every
database lives in a temporary directory.
"""

import contextlib
from datetime import date
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import batch_safety as safety  # noqa: E402
import import_artists as importer  # noqa: E402
import monitor_releases as monitor  # noqa: E402
import release_state as rs  # noqa: E402

TODAY = date(2026, 11, 15)


class FakeDeezer:
    """Stands in for rs.DeezerClient with canned data."""

    def __init__(self, albums=None, details=None, searches=None,
                 artists=None, fail=()):
        self.albums = albums or {}
        self.details = details or {}
        self.searches = searches or {}
        self.artists = artists or {}
        self.fail = set(fail)
        self.album_calls = []

    def artist_albums(self, artist_id):
        if ("albums", artist_id) in self.fail:
            raise rs.DeezerError("boom")
        return list(self.albums.get(artist_id, []))

    def album(self, album_id):
        self.album_calls.append(album_id)
        if ("album", album_id) in self.fail:
            raise rs.DeezerError("boom")
        # No canned detail: no main artist, so it counts as the
        # checked artist's own release.
        return self.details.get(album_id, {"artist": {}})

    def search_artists(self, name, limit=25):
        if ("search", name) in self.fail:
            raise rs.DeezerError("search failed")
        return self.searches.get(name, [])

    def artist(self, artist_id):
        return self.artists[artist_id]


def album(album_id, title, released, record_type="album"):
    """A Deezer artist-albums list entry."""
    return {"id": album_id, "title": title, "release_date": released,
            "record_type": record_type}


def detail(main_id, main_name="Main", tracks=10, record_type="album"):
    """A Deezer album-detail response."""
    return {"artist": {"id": main_id, "name": main_name},
            "nb_tracks": tracks, "record_type": record_type,
            "explicit_lyrics": False, "upc": "123"}


class ReleaseMonitorTests(unittest.TestCase):
    """Database-backed behaviour with fake external services."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = rs.connect(self.root / "releases.db")
        self.addCleanup(self.db.close)
        self.output = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))
        self.settings = monitor.Settings(
            types=list(rs.RELEASE_TYPES), max_age_days=90,
            exclude_keywords=["karaoke"], max_attempts=2)

    # helpers -----------------------------------------------------------
    def follow(self, artist_id=1, name="Artist"):
        with self.db:
            rs.follow_artist(self.db, artist_id, name, "manual")

    def artist_row(self, artist_id=1):
        return self.db.execute("SELECT * FROM artists WHERE deezer_id = ?",
                               (artist_id,)).fetchone()

    def statuses(self):
        return {row["album_id"]: (row["status"], row["reason"])
                for row in self.db.execute("SELECT * FROM releases")}

    def check(self, client, dry_run=False):
        return monitor.check_artist(self.db, client, self.artist_row(),
                                    self.settings, TODAY, dry_run)

    # artist lookup -----------------------------------------------------
    def test_resolve_exact_tie_and_misses(self):
        client = FakeDeezer(searches={
            "Solo": [{"id": 5, "name": "solo", "nb_fan": 1},
                     {"id": 6, "name": "Solo Band", "nb_fan": 9}],
            "Twin": [{"id": 7, "name": "Twin", "nb_fan": 50},
                     {"id": 8, "name": "Twin", "nb_fan": 10}],
            "Even": [{"id": 9, "name": "Even", "nb_fan": 3},
                     {"id": 10, "name": "Even", "nb_fan": 3}],
            "Close": [{"id": 11, "name": "Closer", "nb_fan": 3}],
        })
        self.assertEqual(rs.resolve_artist_name(client, "Solo")[0]["id"], 5)
        self.assertEqual(rs.resolve_artist_name(client, "Twin")[0]["id"], 7)
        self.assertEqual(rs.resolve_artist_name(client, "Even")[:2],
                         (None, "ambiguous"))
        self.assertEqual(rs.resolve_artist_name(client, "Close")[:2],
                         (None, "no exact match"))
        self.assertEqual(rs.resolve_artist_name(client, "Nobody")[:2],
                         (None, "no match"))

    def test_edition_key_merges_editions_but_not_live(self):
        base = rs.edition_key(1, "Album")
        self.assertEqual(rs.edition_key(1, "Album (Deluxe Edition)"), base)
        self.assertEqual(rs.edition_key(1, "Album [Explicit]"), base)
        self.assertEqual(rs.edition_key(1, "Album - Deluxe Version"), base)
        self.assertNotEqual(rs.edition_key(1, "Album (Live)"), base)
        self.assertNotEqual(rs.edition_key(2, "Album"), base)

    def test_release_types_validation(self):
        self.assertEqual(rs.parse_release_types(""), list(rs.RELEASE_TYPES))
        self.assertEqual(rs.parse_release_types("EP, album ep"),
                         ["ep", "album"])
        with self.assertRaisesRegex(ValueError, "unknown release type"):
            rs.parse_release_types("album,mixtape")
        with patch.dict(os.environ, {"RELEASE_MAX_AGE_DAYS": "0"}):
            with self.assertRaisesRegex(ValueError, "RELEASE_MAX_AGE_DAYS"):
                monitor.Settings.from_env()

    # checking ----------------------------------------------------------
    def test_first_check_is_baseline_only(self):
        self.follow()
        client = FakeDeezer(albums={1: [album(100, "Recent", "2026-11-01"),
                                        album(99, "Old", "2019-01-01")]})
        self.assertEqual(self.check(client), [])
        self.assertEqual(self.statuses(), {100: ("baseline", None),
                                           99: ("baseline", None)})
        # Only albums inside the age window need their details looked up.
        self.assertEqual(client.album_calls, [100])
        self.assertEqual(self.artist_row()["baseline_done"], 1)

    def test_later_check_filters_and_queues(self):
        self.follow()
        self.check(FakeDeezer(albums={1: [album(100, "Hit", "2025-01-01")]}))
        albums = [
            album(100, "Hit", "2025-01-01"),
            album(101, "Brand New", "2026-11-10"),
            album(102, "Ancient", "2020-01-01"),
            album(103, "Tomorrow", "2026-12-01"),
            album(104, "Karaoke Hits", "2026-11-10"),
            album(105, "Guest Spot", "2026-11-10"),
            album(106, "Hit (Deluxe Edition)", "2026-11-12"),
            album(107, "Single Thing", "2026-11-11", "single"),
        ]
        details = {101: detail(1), 105: detail(2, "Other"), 106: detail(1),
                   107: detail(1, record_type="single")}
        self.settings.types = ["album", "ep", "featured"]
        queued = self.check(FakeDeezer(albums={1: albums}, details=details))
        self.assertEqual([entry["album_id"] for entry in queued], [101, 105])
        statuses = self.statuses()
        self.assertEqual(statuses[101], ("queued", None))
        self.assertEqual(statuses[102], ("ignored", "age"))
        self.assertNotIn(103, statuses)
        self.assertEqual(statuses[104], ("ignored", "keyword: karaoke"))
        self.assertEqual(statuses[105], ("queued", None))
        self.assertEqual(statuses[106], ("ignored", "duplicate of 100"))
        self.assertEqual(statuses[107], ("ignored", "type: single"))
        row = self.db.execute("SELECT release_type FROM releases "
                              "WHERE album_id = 105").fetchone()
        self.assertEqual(row[0], "featured")

    def test_bigger_edition_replaces_queued_one(self):
        self.follow()
        self.check(FakeDeezer())
        self.check(FakeDeezer(albums={1: [album(200, "Record", "2026-11-01")]},
                              details={200: detail(1, tracks=10)}))
        self.check(FakeDeezer(
            albums={1: [album(201, "Record (Deluxe)", "2026-11-02")]},
            details={201: detail(1, tracks=14)}))
        self.assertEqual(self.statuses()[200], ("ignored", "duplicate of 201"))
        self.assertEqual(self.statuses()[201], ("queued", None))

    def test_single_does_not_block_its_album(self):
        self.follow()
        self.check(FakeDeezer(albums={1: [album(600, "Title Track",
                                                "2026-10-01", "single")]}))
        queued = self.check(FakeDeezer(
            albums={1: [album(601, "Title Track", "2026-11-10")]},
            details={601: detail(1, tracks=12)}))
        self.assertEqual([entry["album_id"] for entry in queued], [601])
        self.assertNotEqual(rs.edition_key(1, "X", "single"),
                            rs.edition_key(1, "X", "album"))

    def test_upcoming_release_skips_baseline_and_queues_later(self):
        self.follow()
        upcoming = album(700, "Soon", "2026-12-01")
        self.check(FakeDeezer(albums={1: [upcoming]}))
        self.assertEqual(self.statuses(), {})
        queued = monitor.check_artist(
            self.db, FakeDeezer(albums={1: [upcoming]},
                                details={700: detail(1)}),
            self.artist_row(), self.settings, date(2026, 12, 2))
        self.assertEqual([entry["album_id"] for entry in queued], [700])

    def test_schema_1_edition_keys_are_migrated(self):
        self.follow()
        self.check(FakeDeezer(albums={1: [album(800, "Old (Deluxe)",
                                                "2026-01-01")]}))
        with self.db:
            self.db.execute("UPDATE releases SET edition_key = '1:old'")
            self.db.execute("PRAGMA user_version = 1")
        self.db.close()
        self.db = rs.connect(self.root / "releases.db")
        row = self.db.execute("SELECT edition_key FROM releases").fetchone()
        self.assertEqual(row[0], rs.edition_key(1, "Old", "album"))
        self.assertEqual(
            self.db.execute("PRAGMA user_version").fetchone()[0],
            rs.SCHEMA_VERSION)

    def test_management_commands_never_load_apprise(self):
        env = {"APPRISE_URLS": "json://127.0.0.1:9/", "RELEASE_TYPES": ""}
        with patch.dict(os.environ, env), \
                patch.object(rs, "DB_PATH", self.root / "releases.db"), \
                patch.object(monitor, "build_notifier",
                             side_effect=AssertionError("loaded")), \
                patch.object(rs, "ensure_private_env",
                             side_effect=AssertionError("venv")):
            self.assertEqual(monitor.main(["artists"]), 0)
            self.assertEqual(monitor.main(["list"]), 0)

    def test_dry_run_and_failed_check_change_nothing(self):
        self.follow()
        client = FakeDeezer(albums={1: [album(100, "Old", "2026-11-01")]})
        self.check(client, dry_run=True)
        self.assertEqual(self.statuses(), {})
        self.assertEqual(self.artist_row()["baseline_done"], 0)
        self.check(FakeDeezer())
        broken = FakeDeezer(albums={1: [album(300, "New", "2026-11-10")]},
                            fail={("album", 300)})
        with self.assertRaises(rs.DeezerError):
            self.check(broken)
        self.assertNotIn(300, self.statuses())

    def test_run_check_continues_after_one_artist_fails(self):
        self.follow(1, "Broken")
        self.follow(2, "Fine")
        client = FakeDeezer(albums={2: [album(400, "A", "2026-01-01")]},
                            fail={("albums", 1)})
        found, errors = monitor.run_check(self.db, client, self.settings,
                                          None, False)
        self.assertEqual(found, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.statuses(), {400: ("baseline", None)})

    # downloading -------------------------------------------------------
    def queue_one(self, album_id=500):
        self.follow()
        self.check(FakeDeezer())
        self.check(FakeDeezer(albums={1: [album(album_id, "Q", "2026-11-10")]},
                              details={album_id: detail(1)}))

    def download(self, returncode):
        staging = self.root / "staging"
        staging.mkdir(exist_ok=True)
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, returncode)

        env = {"STAGING_DIR": str(staging), "BITRATE": "flac"}
        with patch.dict(os.environ, env), \
                patch.object(safety, "LOCK_PATH", self.root / "op.lock"), \
                patch.object(monitor.shutil, "which",
                             return_value="/usr/bin/deemix"), \
                patch.object(monitor.subprocess, "run", side_effect=fake_run):
            result = monitor.run_download(self.db, self.settings, False, None)
        return result, calls

    def test_download_success_marks_downloaded(self):
        self.queue_one()
        (downloaded, failed, errors), calls = self.download(0)
        self.assertEqual([row["album_id"] for row in downloaded], [500])
        self.assertEqual((failed, errors), ([], []))
        self.assertEqual(calls[0][1:3], ["--bitrate", "flac"])
        self.assertEqual(calls[0][-1], "https://www.deezer.com/album/500")
        self.assertEqual(self.statuses()[500], ("downloaded", None))

    def test_download_failure_retries_then_gives_up(self):
        self.queue_one()
        self.download(2)
        self.assertEqual(self.statuses()[500][0], "queued")
        (_, failed, errors), _ = self.download(2)
        self.assertEqual(len(failed), 1)
        self.assertEqual(self.statuses()[500], ("failed", "deemix exit 2"))
        with self.db:
            self.db.execute("UPDATE releases SET status = 'queued', "
                            "attempts = 0 WHERE album_id = 500")
        self.download(0)
        self.assertEqual(self.statuses()[500][0], "downloaded")

    def test_download_skips_release_ignored_meanwhile(self):
        self.queue_one(500)
        self.check(FakeDeezer(albums={1: [album(501, "R", "2026-11-11")]},
                              details={501: detail(1)}))

        def ignore_next(row, *args):
            with self.db:
                monitor.set_status(self.db, 501, rs.STATUS_IGNORED, "manual")
            return None

        with patch.object(monitor, "download_release",
                          side_effect=ignore_next):
            (downloaded, _, _), _ = self.download(0)
        self.assertEqual([row["album_id"] for row in downloaded], [500])
        self.assertEqual(self.statuses()[501], ("ignored", "manual"))

    def test_shared_release_is_decided_for_its_main_artist(self):
        self.follow(1, "Guest")
        self.follow(2, "Main")
        for artist_id in (1, 2):
            monitor.check_artist(self.db, FakeDeezer(),
                                 self.artist_row(artist_id), self.settings,
                                 TODAY)
        self.settings.types = ["album"]
        shared = album(900, "Their Album", "2026-11-10")
        client = FakeDeezer(albums={1: [shared], 2: [shared]},
                            details={900: detail(2, "Main")})
        found, errors = monitor.run_check(self.db, client, self.settings,
                                          None, False)
        self.assertEqual(errors, [])
        self.assertEqual([entry["album_id"] for entry in found], [900])
        row = self.db.execute("SELECT artist_id, release_type FROM releases "
                              "WHERE album_id = 900").fetchone()
        self.assertEqual(tuple(row), (2, "album"))

    def test_new_guest_baseline_leaves_main_artists_new_release(self):
        self.follow(2, "Main")
        monitor.check_artist(self.db, FakeDeezer(), self.artist_row(2),
                             self.settings, TODAY)
        self.follow(1, "Guest")
        shared = album(910, "Fresh", "2026-11-12")
        client = FakeDeezer(albums={1: [shared], 2: [shared]},
                            details={910: detail(2, "Main")})
        found, _ = monitor.run_check(self.db, client, self.settings,
                                     None, False)
        self.assertEqual([entry["album_id"] for entry in found], [910])
        self.assertEqual(self.statuses()[910], ("queued", None))

    def test_check_holds_no_write_lock_while_fetching(self):
        self.follow()
        self.check(FakeDeezer())
        db_path = self.root / "releases.db"

        class Probe(FakeDeezer):
            def album(probe, album_id):
                other = rs.connect(db_path)
                other.execute("PRAGMA busy_timeout = 0")
                with other:
                    other.execute("UPDATE runs SET summary = 'x'")
                other.close()
                return super().album(album_id)

        albums = [album(920, "One", "2026-11-10"),
                  album(921, "Two", "2026-11-11")]
        queued = self.check(Probe(albums={1: albums},
                                  details={920: detail(1), 921: detail(1)}))
        self.assertEqual(len(queued), 2)

    def test_follow_for_name_survives_reimport(self):
        with self.db:
            rs.record_unresolved(self.db, "USA for Africa", "mpd",
                                 "no exact match")
        client = FakeDeezer(artists={62: {"id": 62,
                                          "name": "U.S.A. for Africa"}})
        args = monitor.build_parser().parse_args(
            ["follow", "62", "--for", "USA for Africa"])
        self.assertEqual(monitor.cmd_follow(self.db, client, args), 0)
        result = importer.import_names(
            self.db, FakeDeezer(), [("USA for Africa", None)], "mpd", [])
        self.assertEqual((result["skipped"], result["unresolved"]), (1, []))
        listed = self.db.execute("SELECT COUNT(*) FROM unresolved_artists "
                                 "WHERE ignored = 0").fetchone()[0]
        self.assertEqual(listed, 0)

    def test_downloading_edition_cannot_be_replaced(self):
        self.queue_one(500)

        def check_meanwhile(row, *args):
            self.assertEqual(self.statuses()[500][0], "downloading")
            self.check(FakeDeezer(
                albums={1: [album(502, "Q (Deluxe)", "2026-11-12")]},
                details={502: detail(1, tracks=20)}))
            return None

        with patch.object(monitor, "download_release",
                          side_effect=check_meanwhile):
            self.download(0)
        self.assertEqual(self.statuses()[500], ("downloaded", None))
        self.assertEqual(self.statuses()[502], ("ignored", "duplicate of 500"))

    def test_interrupted_download_is_queued_again(self):
        self.queue_one(500)
        with self.db:
            monitor.set_status(self.db, 500, rs.STATUS_DOWNLOADING)
        (downloaded, _, _), _ = self.download(0)
        self.assertEqual([row["album_id"] for row in downloaded], [500])

    def test_baseline_keys_use_main_artist(self):
        self.follow(1, "Guest")
        shared = album(930, "Record", "2026-11-01")
        self.check(FakeDeezer(albums={1: [shared]},
                              details={930: detail(2, "Other")}))
        row = self.db.execute("SELECT edition_key, main_artist_id "
                              "FROM releases WHERE album_id = 930").fetchone()
        self.assertEqual(tuple(row), (rs.edition_key(2, "Record", "album"), 2))

    def test_rejected_apprise_url_counts_as_failure(self):
        notifier = monitor.Notifier.__new__(monitor.Notifier)
        notifier._apprise = type("A", (), {
            "__len__": lambda self: 1,
            "notify": lambda self, title, body: True})()
        notifier.invalid = [2]
        self.assertFalse(notifier.send("t", "b"))
        notifier.invalid = []
        self.assertTrue(notifier.send("t", "b"))

    def test_download_respects_busy_lock(self):
        self.queue_one()
        with patch.object(monitor, "operation_lock",
                          side_effect=safety.OperationBusyError("busy")):
            (downloaded, _, errors), calls = self.download(0)
        self.assertEqual((downloaded, calls), ([], []))
        self.assertEqual(errors, ["busy"])
        self.assertEqual(self.statuses()[500][0], "queued")

    def test_download_needs_staging_dir(self):
        self.queue_one()
        with patch.dict(os.environ, {"STAGING_DIR": "", "BITRATE": "320"}):
            _, _, errors = monitor.run_download(self.db, self.settings,
                                                True, None)
        self.assertTrue(any("STAGING_DIR" in error for error in errors))

    # importing ---------------------------------------------------------
    def test_import_follows_records_and_skips(self):
        self.follow(9, "Already")
        with self.db:
            self.db.execute("UPDATE artists SET followed = 0 "
                            "WHERE deezer_id = 9")
        client = FakeDeezer(
            searches={"Good": [{"id": 1, "name": "Good", "nb_fan": 4}],
                      "Already": [{"id": 9, "name": "Already"}],
                      "Fuzzy": [{"id": 3, "name": "Fuzzy Band"}]},
            fail={("search", "Down")})
        names = [("Good", "mbid-1"), ("Already", None), ("Fuzzy", None),
                 ("Various Artists", None), ("Skip Me", None), ("Down", None)]
        result = importer.import_names(self.db, client, names, "mpd",
                                       ["skip me"])
        self.assertEqual(result["followed"], ["Good (1)"])
        self.assertEqual(result["known"], 1)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["unresolved"], ["Fuzzy (no exact match)"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.artist_row(1)["mbid"], "mbid-1")
        self.assertEqual(self.artist_row(9)["followed"], 0)
        unresolved = [row["name"] for row in
                      self.db.execute("SELECT name FROM unresolved_artists")]
        self.assertEqual(unresolved, ["Fuzzy"])

    def test_import_splits_joined_names_only_when_whole_fails(self):
        client = FakeDeezer(searches={
            "AC/DC": [{"id": 1, "name": "AC/DC"}],
            "Cash/Nelson & Friends": [],
            "Cash": [{"id": 2, "name": "Cash"}],
            "Nelson": [{"id": 3, "name": "Nelson"}],
            "Friends": [],
        })
        result = importer.import_names(
            self.db, client, [("AC/DC", None), ("Cash/Nelson & Friends", None),
                              ("Cash", None)], "mpd", [])
        self.assertEqual(result["followed"],
                         ["AC/DC (1)", "Cash (2)", "Nelson (3)"])
        self.assertEqual(result["unresolved"], ["Friends (no match)"])
        self.assertEqual(result["known"], 1)
        self.assertEqual(rs.split_joined_artists("A feat. B, C; A"),
                         ["A", "B", "C"])

    def test_import_dry_run_writes_nothing(self):
        client = FakeDeezer(searches={"Good": [{"id": 1, "name": "Good"}],
                                      "Miss": []})
        importer.import_names(self.db, client, [("Good", None),
                                                ("Miss", None)],
                              "mpd", [], dry_run=True)
        self.assertIsNone(self.artist_row(1))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM unresolved_artists").fetchone()[0], 0)

    def test_scan_reads_album_artist_then_artist(self):
        music = self.root / "music"
        (music / "a").mkdir(parents=True)
        for name in ("1.mp3", "2.flac", "3.mp3", "cover.jpg", "4.mp3"):
            (music / "a" / name).write_bytes(b"")
        tags = {
            "1.mp3": {"albumartist": ["Band"],
                      "musicbrainz_albumartistid": ["mb-band"]},
            "2.flac": {"artist": ["Solo"]},
            "3.mp3": {"albumartist": ["Band"]},
            "4.mp3": None,
        }
        fake = type(sys)("mutagen")
        fake.MutagenError = Exception
        fake.File = lambda path, easy: tags[os.path.basename(path)]
        with patch.dict(sys.modules, {"mutagen": fake}):
            names, unreadable = importer.scan_album_artists(music)
        self.assertEqual(sorted(names), [("Band", "mb-band"), ("Solo", None)])
        self.assertEqual(unreadable, 1)

    def test_mpd_protocol_with_password(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        received = []

        def serve():
            connection, _ = server.accept()
            with connection, connection.makefile("rwb") as stream:
                stream.write(b"OK MPD 0.24.0\n")
                stream.flush()
                for _ in range(2):
                    line = stream.readline().decode().strip()
                    received.append(line)
                    if line.startswith("password"):
                        stream.write(b"OK\n")
                    else:
                        stream.write(b"AlbumArtist: Band\nAlbumArtist: \n"
                                     b"AlbumArtist: Solo\nOK\n")
                    stream.flush()

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            names = importer.mpd_album_artists(f'se"cret@127.0.0.1:{port}')
        finally:
            thread.join(5)
            server.close()
        self.assertEqual(names, [("Band", None), ("Solo", None)])
        self.assertEqual(received, ['password "se\\"cret"',
                                    "list albumartist"])

    def test_mpd_target_parsing(self):
        with patch.dict(os.environ, {"MPD_HOST": "", "MPD_PORT": ""}):
            self.assertEqual(importer.parse_mpd_target(None),
                             ("localhost", 6600, None))
            self.assertEqual(importer.parse_mpd_target("pw@host:6601"),
                             ("host", 6601, "pw"))
            self.assertEqual(importer.parse_mpd_target("/run/mpd/socket"),
                             ("/run/mpd/socket", None, None))
            with self.assertRaises(importer.SourceError):
                importer.parse_mpd_target("host:port")

    # notifications -----------------------------------------------------
    def test_summary_message(self):
        self.assertIsNone(monitor.summary_message([], [], []))
        rows = [{"main_artist_name": None, "artist_name": "A",
                 "title": f"T{i}", "release_type": "album",
                 "record_type": "album", "release_date": "2026-11-01"}
                for i in range(monitor.NOTIFY_MAX_LINES + 3)]
        title, body = monitor.summary_message(rows, rows[:1], [])
        self.assertEqual(title,
                         "deemix-batch releases: 43 new, 1 downloaded")
        self.assertIn("...and 3 more", body)
        self.assertIn("Downloaded (1):\nA - T0 (album, 2026-11-01)", body)

    def test_notify_reports_delivery_failure(self):
        class Fake:
            def __init__(self, ok):
                self.ok = ok
                self.sent = []

            def send(self, title, body):
                self.sent.append(title)
                return self.ok

        row = {"main_artist_name": "M", "artist_name": "A", "title": "T",
               "release_type": "ep", "record_type": "ep",
               "release_date": None}
        quiet = Fake(True)
        self.assertTrue(monitor.notify(quiet, [], [], []))
        self.assertEqual(quiet.sent, [])
        self.assertTrue(monitor.notify(None, [row], [], []))
        self.assertFalse(monitor.notify(Fake(False), [row], [], []))


class DeezerClientTests(unittest.TestCase):
    """Retry and safety behaviour of the HTTP client."""

    def client(self, responses):
        sleeps = []
        queue = list(responses)

        def opener(request, timeout):
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return io.BytesIO(json.dumps(item).encode())

        client = rs.DeezerClient(min_interval=0, sleep=sleeps.append,
                                 opener=opener)
        return client, sleeps

    def test_retries_quota_then_succeeds(self):
        client, sleeps = self.client(
            [{"error": {"code": 4, "message": "Quota limit exceeded"}},
             urllib.error.URLError("down"), {"id": 1}])
        self.assertEqual(client.get("artist/1"), {"id": 1})
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_permanent_errors_are_not_retried(self):
        client, sleeps = self.client(
            [{"error": {"code": 800, "message": "no data"}}])
        with self.assertRaisesRegex(rs.DeezerError, "no data"):
            client.get("artist/1")
        self.assertEqual(sleeps, [])

    def test_gives_up_and_follows_pages_safely(self):
        client, _ = self.client([urllib.error.URLError("down")] * 4)
        with self.assertRaisesRegex(rs.DeezerError, "after 4 attempts"):
            client.get("artist/1")
        client, _ = self.client([
            {"data": [1], "next": "https://api.deezer.com/x?index=1"},
            {"data": [2]}])
        self.assertEqual(list(client.paged("x")), [1, 2])
        client, _ = self.client([{"data": [1],
                                  "next": "https://evil.example/x"}])
        with self.assertRaisesRegex(rs.DeezerError, "outside"):
            list(client.paged("x"))


class InstallScriptTests(unittest.TestCase):
    """install.sh renders units and only installs the monitor when asked."""

    def run_install(self, *arguments):
        with tempfile.TemporaryDirectory() as home:
            bin_dir = Path(home) / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "systemctl"
            stub.write_text("#!/usr/bin/bash\n"
                            'echo "$@" >> "$HOME/systemctl.log"\n')
            stub.chmod(0o755)
            project = Path(home) / "project"
            project.mkdir()
            for name in ("install.sh", "systemd"):
                source = ROOT / name
                if source.is_dir():
                    (project / name).mkdir()
                    for unit in source.iterdir():
                        (project / name / unit.name).write_text(
                            unit.read_text())
                else:
                    (project / name).write_text(source.read_text())
            (project / ".env").write_text("STAGING_DIR=/srv/staging\n")
            env = {**os.environ, "HOME": home,
                   "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            completed = subprocess.run(
                ["bash", str(project / "install.sh"), *arguments], env=env,
                capture_output=True, text=True)
            units = Path(home) / ".config/systemd/user"
            files = {path.name: path.read_text()
                     for path in units.iterdir()} if units.exists() else {}
            log = (Path(home) / "systemctl.log").read_text() \
                if (Path(home) / "systemctl.log").exists() else ""
            return completed, files, log, project

    def test_default_installs_only_sync(self):
        completed, files, log, project = self.run_install()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(sorted(files),
                         ["deemix-batch.service", "deemix-batch.timer"])
        self.assertIn(f'"{project}/sync_to_dest.py" --source "/srv/staging"',
                      files["deemix-batch.service"])
        self.assertIn("enable --now deemix-batch.timer\n", log)

    def test_release_monitor_option(self):
        completed, files, log, project = self.run_install("-r")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(f'ExecStart="{project}/monitor_releases.py" '
                      "check --download",
                      files["release-monitor.service"])
        self.assertIn(f"WorkingDirectory={project}",
                      files["release-monitor.service"])
        self.assertIn('Environment="STAGING_DIR=/srv/staging"',
                      files["release-monitor.service"])
        self.assertIn("release-monitor.timer", files)
        self.assertIn("enable --now deemix-batch.timer "
                      "release-monitor.timer", log)
        completed, _, _, _ = self.run_install("--bogus")
        self.assertEqual(completed.returncode, 1)


if __name__ == "__main__":
    unittest.main()
