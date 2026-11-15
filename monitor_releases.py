#!/usr/bin/env python3
"""Watch followed artists for new Deezer releases and download them.

Replaces deemon's new-release monitoring. Followed artists, every release
seen, and its status live in state/releases.db. Fill the artist list with
import_artists.py (from MPD or your music files) or with `follow`.

Commands:
  check       Fetch each followed artist's Deezer albums and queue new ones.
              -d/--download also downloads the queue afterwards.
  download    Download queued releases one at a time with the deemix CLI.
  list        Show releases (filter by status or artist).
  artists     Show followed artists (--unresolved / --unfollowed lists).
  follow      Follow an artist by Deezer ID or exact name.
  unfollow    Stop following an artist (its releases stay recorded).
  queue       Queue a known release by Deezer album ID (resets attempts).
  ignore      Never download a known release.
  test-notify Send a test notification to every APPRISE_URLS entry.

How a release is handled:
  - The first check of a newly followed artist records its existing
    releases as "baseline"; they are not downloaded. Use `queue` for any
    you want anyway.
  - Later checks queue a release that is new to the database when its type
    is selected, it came out within RELEASE_MAX_AGE_DAYS (releases Deezer
    adds late still count), its title has no RELEASE_EXCLUDE_KEYWORDS word,
    and no other edition of it (deluxe/explicit/clean) is already queued,
    downloaded or in the baseline. Releases dated in the future are left
    until their date passes.
  - Downloads go to STAGING_DIR at BITRATE by album URL, inside the shared
    operation lock; the sync timer moves them on as usual. deemix exit 0
    marks a release "downloaded". Missing or region-blocked tracks stay in
    deemix's errors.txt for retry_failed_tracks.py. A failed download is
    retried on later runs, up to MAX_DOWNLOAD_ATTEMPTS, then marked "failed".

Settings from .env (all optional except STAGING_DIR for downloads):
  RELEASE_TYPES             album,ep,single,compile,featured (default: all)
  RELEASE_MAX_AGE_DAYS      default 90
  RELEASE_EXCLUDE_KEYWORDS  comma-separated title words, e.g. karaoke,live
  MAX_DOWNLOAD_ATTEMPTS     default 3
  APPRISE_URLS              comma/space-separated Apprise URLs; empty = no
                            notifications. Uses Apprise from the private
                            .release-monitor-venv, created on first use.
  STAGING_DIR, BITRATE      shared with download_artists.py
  DEEMIX                    deemix executable (default: deemix)

Exit status: 0 on success; 1 if any artist check, download, notification or
the operation lock failed (the rest of the run still completes).

Examples:
    python3 monitor_releases.py follow "Daft Punk"
    python3 monitor_releases.py check --dry-run
    python3 monitor_releases.py check --download
    python3 monitor_releases.py list --status queued
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_state as rs  # noqa: E402
from batch_safety import (  # noqa: E402
    OperationBusyError,
    inherited_lock_fds,
    operation_lock,
)

BITRATES = ("128", "320", "flac")
NOTIFY_MAX_LINES = 40


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    """Release filters and limits, from .env and command-line overrides."""

    types: list[str]
    max_age_days: int
    exclude_keywords: list[str]
    max_attempts: int

    @classmethod
    def from_env(cls, types_override: str | None = None) -> "Settings":
        """Read and validate settings; raises ValueError naming the setting."""
        try:
            types = rs.parse_release_types(
                types_override
                if types_override is not None
                else os.environ.get("RELEASE_TYPES")
            )
        except ValueError as exc:
            source = (
                "--types" if types_override is not None else "RELEASE_TYPES"
            )
            raise ValueError(f"{source}: {exc}") from None
        return cls(
            types=types,
            max_age_days=rs.env_positive_int("RELEASE_MAX_AGE_DAYS", 90),
            exclude_keywords=rs.split_comma_list(
                os.environ.get("RELEASE_EXCLUDE_KEYWORDS")
            ),
            max_attempts=rs.env_positive_int("MAX_DOWNLOAD_ATTEMPTS", 3),
        )

    def excluded_by_keyword(self, title: str) -> str | None:
        """Return the matching exclude keyword (whole words, any case)."""
        for keyword in self.exclude_keywords:
            if re.search(
                rf"(?<!\w){re.escape(keyword)}(?!\w)", title, re.IGNORECASE
            ):
                return keyword
        return None


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
class Notifier:
    """Send run summaries through Apprise. URLs are never printed or logged."""

    def __init__(self, urls: list[str]):
        import apprise

        self._apprise = apprise.Apprise()
        self.invalid = [
            index
            for index, url in enumerate(urls, 1)
            if not self._apprise.add(url)
        ]

    def send(self, title: str, body: str) -> bool:
        """Deliver one message; True only if every service accepted it."""
        if not len(self._apprise):
            return False
        delivered = bool(self._apprise.notify(title=title, body=body))
        return delivered and not self.invalid


def needs_apprise(command: str, dry_run: bool) -> bool:
    """True when this run will send notifications."""
    if command not in ("check", "download", "test-notify") or dry_run:
        return False
    return bool(rs.split_list(os.environ.get("APPRISE_URLS")))


def build_notifier() -> Notifier | None:
    """Notifier for APPRISE_URLS, or None when it is empty."""
    urls = rs.split_list(os.environ.get("APPRISE_URLS"))
    if not urls:
        return None
    notifier = Notifier(urls)
    for index in notifier.invalid:
        print(
            f"Warning: APPRISE_URLS entry {index} is not a valid Apprise URL; "
            "skipped.",
            file=sys.stderr,
        )
    return notifier


def format_release(row) -> str:
    """One readable line for a release row or dict."""
    artist = row["main_artist_name"] or row["artist_name"]
    return (
        f"{artist} - {row['title']} "
        f"({row['release_type'] or row['record_type']}, "
        f"{row['release_date'] or 'no date'})"
    )


def summary_message(
    found: list, downloaded: list, failed: list
) -> tuple[str, str] | None:
    """Build the (title, body) for a run, or None when nothing happened."""
    sections = []
    for heading, rows in (
        ("New releases", found),
        ("Downloaded", downloaded),
        ("Download failed", failed),
    ):
        if rows:
            lines = [format_release(row) for row in rows]
            if len(lines) > NOTIFY_MAX_LINES:
                extra = len(lines) - NOTIFY_MAX_LINES
                lines = lines[:NOTIFY_MAX_LINES] + [f"...and {extra} more"]
            sections.append(f"{heading} ({len(rows)}):\n" + "\n".join(lines))
    if not sections:
        return None
    parts = []
    if found:
        parts.append(f"{len(found)} new")
    if downloaded:
        parts.append(f"{len(downloaded)} downloaded")
    if failed:
        parts.append(f"{len(failed)} failed")
    return "deemix-batch releases: " + ", ".join(parts), "\n\n".join(sections)


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #
def insert_release(
    connection,
    artist_id: int,
    album: dict,
    status: str,
    reason: str | None = None,
    detail: dict | None = None,
    release_type: str | None = None,
    key: str | None = None,
) -> None:
    """Record one release the first time it is seen."""
    detail = detail or {}
    main = detail.get("artist") or {}
    now = rs.utc_now()
    connection.execute(
        "INSERT OR IGNORE INTO releases (album_id, artist_id, title, "
        "record_type, release_type, release_date, main_artist_id, "
        "main_artist_name, nb_tracks, explicit, upc, edition_key, status, "
        "reason, first_seen_at, status_changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(album["id"]),
            artist_id,
            str(album.get("title") or ""),
            str(album.get("record_type") or ""),
            release_type,
            album.get("release_date") or detail.get("release_date"),
            main.get("id"),
            main.get("name"),
            detail.get("nb_tracks"),
            (
                None
                if detail.get("explicit_lyrics") is None
                else int(bool(detail["explicit_lyrics"]))
            ),
            detail.get("upc"),
            # Baseline/early-filtered releases have no album detail; the
            # followed artist stands in for the main artist in the key.
            key
            or rs.edition_key(
                artist_id,
                str(album.get("title") or ""),
                str(album.get("record_type") or ""),
            ),
            status,
            reason,
            now,
            now,
        ),
    )


def set_status(
    connection, album_id: int, status: str, reason: str | None = None
) -> None:
    """Change a release's status and note when."""
    connection.execute(
        "UPDATE releases SET status = ?, reason = ?, status_changed_at = ? "
        "WHERE album_id = ?",
        (status, reason, rs.utc_now(), album_id),
    )


def check_artist(
    connection,
    client: rs.DeezerClient,
    artist,
    settings: Settings,
    today: date | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Check one followed artist; return the releases newly queued.

    Every release is written in one transaction per artist, so an
    interrupted check never leaves an artist half-recorded. Raises
    DeezerError if Deezer could not be read (nothing is written then).
    """
    artist_id = int(artist["deezer_id"])
    albums = client.artist_albums(artist_id)
    known = (
        {
            row[0]
            for row in connection.execute(
                "SELECT album_id FROM releases WHERE album_id IN (%s)"
                % ",".join("?" * len(albums)),
                [int(a["id"]) for a in albums],
            )
        }
        if albums
        else set()
    )
    new_albums = [album for album in albums if int(album["id"]) not in known]
    baseline = not artist["baseline_done"]
    if baseline:
        # Upcoming releases stay unrecorded, so a later check can still
        # queue them once their date has passed.
        new_albums = [
            album
            for album in new_albums
            if (rs.release_age_days(album.get("release_date"), today) or 0)
            >= 0
        ]
    # All network requests happen here, before the write transaction, so a
    # slow Deezer never holds the database lock a download needs.
    details = {
        int(album["id"]): client.album(int(album["id"]))
        for album in new_albums
        if needs_detail(album, settings, today, baseline)
    }
    queued: list[dict] = []
    try:
        # The context manager commits on success and rolls back on any
        # exception, including the one a dry run raises on purpose.
        with connection:
            if baseline:
                recorded = 0
                for album in new_albums:
                    owner = followed_owner(
                        connection, details.get(int(album["id"])), artist_id
                    )
                    if owner is not None and owner["baseline_done"]:
                        continue  # A new release of theirs; they decide it.
                    detail = details.get(int(album["id"]))
                    main_id = int(
                        ((detail or {}).get("artist") or {}).get("id")
                        or artist_id
                    )
                    insert_release(
                        connection,
                        artist_id,
                        album,
                        rs.STATUS_BASELINE,
                        detail=detail,
                        key=rs.edition_key(
                            main_id,
                            str(album.get("title") or ""),
                            str(album.get("record_type") or ""),
                        ),
                    )
                    recorded += 1
                verb = "would be recorded" if dry_run else "recorded"
                print(f"  baseline: {recorded} existing release(s) {verb}")
            else:
                for album in new_albums:
                    entry = consider_release(
                        connection,
                        details.get(int(album["id"])),
                        artist_id,
                        artist["name"],
                        album,
                        settings,
                        today,
                    )
                    if entry is not None:
                        queued.append(entry)
            connection.execute(
                "UPDATE artists SET last_checked_at = ?, baseline_done = 1 "
                "WHERE deezer_id = ?",
                (rs.utc_now(), artist_id),
            )
            if dry_run:
                raise _DryRunRollback
    except _DryRunRollback:
        pass
    return queued


class _DryRunRollback(Exception):
    """Internal: undo a dry-run check after computing its result."""


def needs_detail(
    album: dict, settings: Settings, today: date | None, baseline: bool
) -> bool:
    """True when deciding this new album needs its Deezer album details.

    That is every album still inside the age window: on a later check to
    apply the filters, and on a first check to spot a shared album that is
    another followed artist's new release. Keyword-excluded albums never
    need them.
    """
    age = rs.release_age_days(album.get("release_date"), today)
    if age is not None and (age < 0 or age > settings.max_age_days):
        return False
    return baseline or not settings.excluded_by_keyword(
        str(album.get("title") or "")
    )


def followed_owner(connection, detail: dict | None, artist_id: int):
    """The followed artist row whose own release this is, if not artist_id."""
    main_id = int(((detail or {}).get("artist") or {}).get("id") or artist_id)
    if main_id == artist_id:
        return None
    return connection.execute(
        "SELECT deezer_id, name, baseline_done FROM artists "
        "WHERE deezer_id = ? AND followed = 1",
        (main_id,),
    ).fetchone()


def consider_release(
    connection,
    detail: dict | None,
    artist_id: int,
    artist_name: str,
    album: dict,
    settings: Settings,
    today: date | None,
) -> dict | None:
    """Apply the filters to one new release; return it if queued.

    detail is the album's Deezer details, fetched beforehand for every
    album that passes the age and keyword filters (see needs_detail).
    """
    title = str(album.get("title") or "")
    age = rs.release_age_days(album.get("release_date"), today)
    if age is not None and age < 0:
        return None  # Future date: leave it unrecorded until it is out.
    if age is not None and age > settings.max_age_days:
        insert_release(connection, artist_id, album, rs.STATUS_IGNORED, "age")
        return None
    keyword = settings.excluded_by_keyword(title)
    if keyword:
        insert_release(
            connection,
            artist_id,
            album,
            rs.STATUS_IGNORED,
            f"keyword: {keyword}",
        )
        return None
    detail = detail or {}
    main_id = int((detail.get("artist") or {}).get("id") or artist_id)
    # Another followed artist's own release is decided as theirs, so the
    # result never depends on which artist was checked first.
    owner = followed_owner(connection, detail, artist_id)
    if owner is not None:
        if not owner["baseline_done"]:
            return None  # Their first check records it as baseline.
        artist_id, artist_name = main_id, owner["name"]
    release_type = rs.classify_release(album, detail, artist_id)
    key = rs.edition_key(main_id, title, str(album.get("record_type") or ""))
    if release_type not in settings.types:
        insert_release(
            connection,
            artist_id,
            album,
            rs.STATUS_IGNORED,
            f"type: {release_type}",
            detail,
            release_type,
            key,
        )
        return None
    existing = connection.execute(
        "SELECT album_id, status, nb_tracks FROM releases "
        "WHERE edition_key = ? "
        "AND status IN (?, ?, ?, ?)",
        (
            key,
            rs.STATUS_BASELINE,
            rs.STATUS_DOWNLOADING,
            rs.STATUS_DOWNLOADED,
            rs.STATUS_QUEUED,
        ),
    ).fetchall()
    tracks = detail.get("nb_tracks") or 0
    for row in existing:
        if row["status"] != rs.STATUS_QUEUED or tracks <= (
            row["nb_tracks"] or 0
        ):
            insert_release(
                connection,
                artist_id,
                album,
                rs.STATUS_IGNORED,
                f"duplicate of {row['album_id']}",
                detail,
                release_type,
                key,
            )
            return None
    for row in existing:  # Only queued editions with fewer tracks remain.
        set_status(
            connection,
            row["album_id"],
            rs.STATUS_IGNORED,
            f"duplicate of {album['id']}",
        )
    insert_release(
        connection,
        artist_id,
        album,
        rs.STATUS_QUEUED,
        None,
        detail,
        release_type,
        key,
    )
    entry = {
        "album_id": int(album["id"]),
        "title": title,
        "record_type": album.get("record_type"),
        "release_type": release_type,
        "release_date": album.get("release_date"),
        "main_artist_name": (detail.get("artist") or {}).get("name"),
        "artist_name": artist_name,
    }
    print(f"  new: {format_release(entry)}")
    return entry


def run_check(
    connection,
    client,
    settings: Settings,
    only: list[int] | None,
    dry_run: bool,
) -> tuple[list[dict], list[str]]:
    """Check every followed artist (or only the given IDs)."""
    query = "SELECT * FROM artists WHERE followed = 1"
    params: list = []
    if only:
        query += " AND deezer_id IN (%s)" % ",".join("?" * len(only))
        params = only
    artists = connection.execute(
        query + " ORDER BY name COLLATE NOCASE", params
    ).fetchall()
    if not artists:
        print(
            "No followed artists. Add some with import_artists.py or `follow`."
        )
    found, errors = [], []
    for artist in artists:
        print(f"{artist['name']} (ID: {artist['deezer_id']})")
        try:
            found += check_artist(
                connection, client, artist, settings, dry_run=dry_run
            )
        except rs.DeezerError as exc:
            errors.append(f"{artist['name']}: {exc}")
            print(f"  check failed: {exc}", file=sys.stderr)
    return found, errors


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #
def queued_releases(connection, limit: int | None = None):
    """Queued releases, oldest release date first."""
    query = (
        "SELECT r.*, a.name AS artist_name FROM releases r "
        "JOIN artists a ON a.deezer_id = r.artist_id WHERE r.status = ? "
        "ORDER BY r.release_date, r.album_id"
    )
    params: list = [rs.STATUS_QUEUED]
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    return connection.execute(query, params).fetchall()


def download_release(
    row, staging: str, bitrate: str, deemix: str
) -> str | None:
    """Run deemix for one album URL; return None on success or an error."""
    url = f"https://www.deezer.com/album/{int(row['album_id'])}"
    command = [deemix, "--bitrate", bitrate, "--path", staging, url]
    print(shlex.join(command), flush=True)
    try:
        completed = subprocess.run(command, pass_fds=inherited_lock_fds())
    except OSError as exc:
        return f"cannot run deemix: {exc}"
    if completed.returncode:
        return f"deemix exit {completed.returncode}"
    return None


def run_download(
    connection, settings: Settings, dry_run: bool, limit: int | None
) -> tuple[list, list, list[str]]:
    """Download the queue under the shared operation lock.

    Returns (downloaded rows, failed rows, errors). A failed release stays
    queued until it has failed MAX_DOWNLOAD_ATTEMPTS times.
    """
    rows = queued_releases(connection, limit)
    leftover = connection.execute(
        "SELECT 1 FROM releases WHERE status = ? LIMIT 1",
        (rs.STATUS_DOWNLOADING,),
    ).fetchone()
    if not rows and not leftover:
        print("Nothing queued to download.")
        return [], [], []
    staging = os.environ.get("STAGING_DIR")
    bitrate = os.environ.get("BITRATE") or "320"
    deemix_name = os.environ.get("DEEMIX") or "deemix"
    problems = []
    if not staging:
        problems.append("STAGING_DIR is not set in .env (where downloads go)")
    elif not os.path.isdir(os.path.expanduser(staging)):
        problems.append(f"STAGING_DIR does not exist: {staging}")
    if bitrate not in BITRATES:
        problems.append(
            f"BITRATE must be one of {', '.join(BITRATES)}, got {bitrate!r}"
        )
    deemix = shutil.which(os.path.expanduser(deemix_name))
    if deemix is None and not dry_run:
        problems.append(f"deemix executable not found: {deemix_name}")
    if problems:
        for problem in problems:
            print(f"Error: {problem}", file=sys.stderr)
        return [], [], problems
    staging = str(Path(staging).expanduser().resolve())
    if dry_run:
        for row in rows:
            print(f"[dry-run] would download: {format_release(row)}")
        return [], [], []

    downloaded, failed, errors = [], [], []
    try:
        with operation_lock():
            # Holding the lock means no other download is running, so any
            # "downloading" row is left over from an interrupted run.
            with connection:
                connection.execute(
                    "UPDATE releases SET status = ? WHERE status = ?",
                    (rs.STATUS_QUEUED, rs.STATUS_DOWNLOADING),
                )
            rows = queued_releases(connection, limit)
            for row in rows:
                # `ignore` or `check` may have changed the queue while an
                # earlier album was downloading; use the current row.
                current = connection.execute(
                    "SELECT status, attempts FROM releases WHERE album_id = ?",
                    (row["album_id"],),
                ).fetchone()
                if current is None or current["status"] != rs.STATUS_QUEUED:
                    print(
                        f"Skipping (no longer queued): {format_release(row)}"
                    )
                    continue
                # Claim it, so a concurrent check cannot replace this
                # edition with another one while deemix is running.
                with connection:
                    set_status(
                        connection, row["album_id"], rs.STATUS_DOWNLOADING
                    )
                print(f"=== {format_release(row)} ===", flush=True)
                error = download_release(row, staging, bitrate, deemix)
                with connection:
                    if error is None:
                        connection.execute(
                            "UPDATE releases SET attempts = attempts + 1, "
                            "last_error = NULL "
                            "WHERE album_id = ?",
                            (row["album_id"],),
                        )
                        set_status(
                            connection, row["album_id"], rs.STATUS_DOWNLOADED
                        )
                        downloaded.append(row)
                        continue
                    attempts = current["attempts"] + 1
                    connection.execute(
                        "UPDATE releases SET attempts = ?, last_error = ? "
                        "WHERE album_id = ?",
                        (attempts, error, row["album_id"]),
                    )
                    if attempts >= settings.max_attempts:
                        set_status(
                            connection,
                            row["album_id"],
                            rs.STATUS_FAILED,
                            error,
                        )
                    else:
                        set_status(
                            connection, row["album_id"], rs.STATUS_QUEUED
                        )
                errors.append(f"{format_release(row)}: {error}")
                failed.append(row)
                print(
                    f"  FAILED ({error}); attempt {attempts} of "
                    f"{settings.max_attempts}",
                    file=sys.stderr,
                )
    except OperationBusyError as exc:
        print(str(exc), file=sys.stderr)
        errors.append(str(exc))
    return downloaded, failed, errors


# --------------------------------------------------------------------------- #
# Artist and release management
# --------------------------------------------------------------------------- #
def cmd_follow(connection, client, args) -> int:
    """Follow one artist by Deezer ID or exact name."""
    try:
        if args.artist.strip().isdigit():
            artist = client.artist(int(args.artist))
        else:
            artist, reason, candidates = rs.resolve_artist_name(
                client, args.artist
            )
            if artist is None:
                print(
                    f"No single Deezer match for {args.artist!r} ({reason}).",
                    file=sys.stderr,
                )
                for item in candidates:
                    print(
                        f"  {item['name']} (ID: {item['id']}, "
                        f"fans: {item['fans']})",
                        file=sys.stderr,
                    )
                print("Follow one of them by ID instead.", file=sys.stderr)
                return 1
    except rs.DeezerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    deezer_id, name = int(artist["id"]), str(artist["name"])
    with connection:
        added = rs.follow_artist(connection, deezer_id, name, "manual")
        connection.execute(
            "DELETE FROM unresolved_artists WHERE name = ?", (name,)
        )
        if args.for_name and rs.normalize_name(
            args.for_name
        ) != rs.normalize_name(name):
            # Remember the library name as resolved, so later imports skip
            # it instead of listing it as unresolved again.
            rs.record_unresolved(
                connection, args.for_name, "manual", f"resolved: {deezer_id}"
            )
            connection.execute(
                "UPDATE unresolved_artists SET ignored = 1 WHERE name = ?",
                (args.for_name,),
            )
    print(
        f"{'Now following' if added else 'Already following'} "
        f"{name} (ID: {deezer_id})."
    )
    if added:
        print("Its current releases become the baseline at the next check.")
    return 0


def cmd_unfollow(connection, args) -> int:
    """Stop following an artist; imports will not re-follow it."""
    with connection:
        updated = connection.execute(
            "UPDATE artists SET followed = 0 WHERE deezer_id = ?",
            (args.artist_id,),
        ).rowcount
    if not updated:
        print(
            f"No artist with ID {args.artist_id} in the database.",
            file=sys.stderr,
        )
        return 1
    print(f"Unfollowed artist {args.artist_id}; its releases stay recorded.")
    return 0


def cmd_artists(connection, args) -> int:
    """List followed, unfollowed or unresolved artists."""
    if args.unresolved:
        rows = connection.execute(
            "SELECT * FROM unresolved_artists WHERE ignored = 0 ORDER BY name"
        ).fetchall()
        for row in rows:
            print(f"{row['name']}  [{row['reason']}, from {row['source']}]")
            for item in json.loads(row["candidates"] or "[]"):
                print(
                    f"    candidate: {item['name']} (ID: {item['id']}, "
                    f"fans: {item['fans']})"
                )
        print(
            f"{len(rows)} unresolved name(s). Follow the right artist with: "
            'monitor_releases.py follow ID --for "NAME"'
        )
        return 0
    rows = connection.execute(
        "SELECT a.*, (SELECT COUNT(*) FROM releases r "
        "WHERE r.artist_id = a.deezer_id) AS releases FROM artists a "
        "WHERE followed = ? ORDER BY name COLLATE NOCASE",
        (0 if args.unfollowed else 1,),
    ).fetchall()
    for row in rows:
        checked = row["last_checked_at"] or "never checked"
        print(
            f"{row['deezer_id']:>10}  {row['name']}  [{row['source']}; "
            f"{row['releases']} release(s); {checked}]"
        )
    print(
        f"{len(rows)} "
        f"{'unfollowed' if args.unfollowed else 'followed'} artist(s)."
    )
    return 0


def cmd_list(connection, args) -> int:
    """List releases with optional status/artist filters."""
    query = (
        "SELECT r.*, a.name AS artist_name FROM releases r "
        "JOIN artists a ON a.deezer_id = r.artist_id WHERE 1 = 1"
    )
    params: list = []
    if args.status:
        query += " AND r.status = ?"
        params.append(args.status)
    if args.artist:
        if args.artist.isdigit():
            query += " AND r.artist_id = ?"
            params.append(int(args.artist))
        else:
            query += " AND a.name LIKE ?"
            params.append(f"%{args.artist}%")
    query += " ORDER BY r.release_date DESC, r.album_id DESC LIMIT ?"
    params.append(args.limit)
    rows = connection.execute(query, params).fetchall()
    for row in rows:
        reason = f"  ({row['reason']})" if row["reason"] else ""
        print(
            f"{row['album_id']:>10}  {row['status']:<10}  "
            f"{format_release(row)}{reason}"
        )
    print(f"{len(rows)} release(s) shown (limit {args.limit}).")
    return 0


def cmd_set_status(connection, args, status: str) -> int:
    """Queue or ignore known releases by album ID."""
    failures = 0
    with connection:
        for album_id in args.album_ids:
            if status == rs.STATUS_QUEUED:
                updated = connection.execute(
                    "UPDATE releases SET attempts = 0, last_error = NULL "
                    "WHERE album_id = ?",
                    (album_id,),
                ).rowcount
            else:
                updated = connection.execute(
                    "SELECT 1 FROM releases WHERE album_id = ?", (album_id,)
                ).fetchone()
            if not updated:
                print(
                    f"Release {album_id} is not in the database; follow its "
                    "artist and run `check` first.",
                    file=sys.stderr,
                )
                failures += 1
                continue
            set_status(connection, album_id, status, "manual")
            print(f"{album_id}: {status}")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    check = commands.add_parser(
        "check", help="look for new releases and queue them"
    )
    check.add_argument(
        "-d",
        "--download",
        action="store_true",
        help="download the queue after checking",
    )
    check.add_argument(
        "-t",
        "--types",
        metavar="TYPES",
        help="release types for this run, comma-separated: "
        f"{','.join(rs.RELEASE_TYPES)} (default: RELEASE_TYPES "
        "from .env, else all)",
    )
    check.add_argument(
        "-a",
        "--artist",
        type=int,
        action="append",
        metavar="ID",
        help="check only this followed artist ID (repeatable)",
    )
    check.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would be queued/downloaded; change nothing "
        "and send no notification",
    )

    download = commands.add_parser("download", help="download queued releases")
    download.add_argument(
        "-l",
        "--limit",
        type=int,
        metavar="N",
        help="download at most N releases this run",
    )
    download.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would be downloaded; change nothing",
    )

    listing = commands.add_parser("list", help="show releases")
    listing.add_argument(
        "-s",
        "--status",
        choices=rs.STATUSES,
        help="only releases with this status",
    )
    listing.add_argument(
        "-a",
        "--artist",
        metavar="ID|NAME",
        help="only this artist (Deezer ID, or part of the name)",
    )
    listing.add_argument(
        "-l",
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="show at most N releases (default: 50)",
    )

    artists = commands.add_parser("artists", help="show followed artists")
    which = artists.add_mutually_exclusive_group()
    which.add_argument(
        "-u",
        "--unresolved",
        action="store_true",
        help="show imported names that matched no single Deezer artist",
    )
    which.add_argument(
        "-f",
        "--unfollowed",
        action="store_true",
        help="show artists you unfollowed",
    )

    follow = commands.add_parser("follow", help="follow an artist")
    follow.add_argument(
        "artist", help="Deezer artist ID, or exact artist name"
    )
    follow.add_argument(
        "-f",
        "--for",
        dest="for_name",
        metavar="NAME",
        help="also clear this unresolved imported name",
    )

    unfollow = commands.add_parser("unfollow", help="stop following an artist")
    unfollow.add_argument("artist_id", type=int, help="Deezer artist ID")

    queue = commands.add_parser(
        "queue", help="queue known releases for download"
    )
    queue.add_argument(
        "album_ids",
        type=int,
        nargs="+",
        metavar="ALBUM_ID",
        help="Deezer album ID(s), as shown by `list`",
    )

    ignore = commands.add_parser(
        "ignore", help="never download known releases"
    )
    ignore.add_argument(
        "album_ids",
        type=int,
        nargs="+",
        metavar="ALBUM_ID",
        help="Deezer album ID(s), as shown by `list`",
    )

    commands.add_parser("test-notify", help="send a test notification")
    return parser


def notify(notifier: Notifier | None, found, downloaded, failed) -> bool:
    """Send the run summary if anything happened; False on delivery failure."""
    message = summary_message(found, downloaded, failed)
    if notifier is None or message is None:
        return True
    if notifier.send(*message):
        print("Notification sent.")
        return True
    print(
        "Warning: the notification could not be delivered to every service.",
        file=sys.stderr,
    )
    return False


def main(argv: list[str] | None = None) -> int:
    """Dispatch one command."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not arguments:
        parser.print_help()
        return 0
    rs.load_dotenv()
    args = parser.parse_args(arguments)
    if args.command is None:
        parser.print_help()
        return 0
    dry_run = getattr(args, "dry_run", False)
    if needs_apprise(args.command, dry_run):
        try:
            import apprise  # noqa: F401  (only checking it is available)
        except ImportError:
            try:
                rs.ensure_private_env(__file__, arguments)
            except (
                OSError,
                RuntimeError,
                subprocess.CalledProcessError,
            ) as exc:
                print(
                    f"Private Python environment setup failed: {exc}",
                    file=sys.stderr,
                )
                return 1

    try:
        settings = Settings.from_env(getattr(args, "types", None))
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    connection = rs.connect(
        ":memory:" if dry_run and not rs.DB_PATH.exists() else rs.DB_PATH
    )
    client = rs.DeezerClient()
    # Only commands that notify load Apprise (and set up its venv above).
    notifier = (
        build_notifier() if needs_apprise(args.command, dry_run) else None
    )

    if args.command == "follow":
        return cmd_follow(connection, client, args)
    if args.command == "unfollow":
        return cmd_unfollow(connection, args)
    if args.command == "artists":
        return cmd_artists(connection, args)
    if args.command == "list":
        return cmd_list(connection, args)
    if args.command == "queue":
        return cmd_set_status(connection, args, rs.STATUS_QUEUED)
    if args.command == "ignore":
        return cmd_set_status(connection, args, rs.STATUS_IGNORED)
    if args.command == "test-notify":
        if notifier is None:
            print(
                "APPRISE_URLS is empty in .env; nothing to send to.",
                file=sys.stderr,
            )
            return 1
        ok = notifier.send(
            "deemix-batch test", "Release monitor notifications work."
        )
        print("Test notification sent." if ok else "Test notification failed.")
        return 0 if ok else 1

    found, downloaded, failed, errors = [], [], [], []
    if args.command == "check":
        if dry_run:
            found, errors = run_check(
                connection, client, settings, args.artist, True
            )
            print(
                f"\n[dry-run] {len(found)} release(s) would be queued; "
                "nothing changed."
            )
            if args.download:
                print(
                    "[dry-run] Releases already queued that would be "
                    "downloaded:"
                )
                run_download(connection, settings, True, None)
            return 1 if errors else 0
        with rs.recorded_run(connection, "check") as summary:
            found, errors = run_check(
                connection, client, settings, args.artist, False
            )
            summary.update(queued=len(found), errors=len(errors))
        print(
            f"\n{len(found)} new release(s) queued; "
            f"{len(errors)} artist check(s) failed."
        )
    if args.command == "download" or (
        args.command == "check" and args.download
    ):
        limit = getattr(args, "limit", None)
        if dry_run:
            _, _, errors = run_download(connection, settings, True, limit)
            return 1 if errors else 0
        with rs.recorded_run(connection, "download") as summary:
            downloaded, failed, download_errors = run_download(
                connection, settings, False, limit
            )
            summary.update(
                downloaded=len(downloaded),
                failed=len(failed),
                errors=len(download_errors),
            )
        errors += download_errors
        print(f"\n{len(downloaded)} downloaded; {len(failed)} failed.")
    delivered = notify(notifier, found, downloaded, failed)
    return 1 if errors or not delivered else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
