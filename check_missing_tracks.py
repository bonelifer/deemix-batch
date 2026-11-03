#!/usr/bin/env python3
"""
For every artist in done.txt, check their EXISTING local album folders for
missing tracks and download just those -- via
"deemix --path <root> https://www.deezer.com/track/<id>" per missing track,
same mechanism retry_failed_tracks.py uses.

Each local album folder is identified from a majority vote of sampled ISRC
lookups. An ISRC identifies a recording, which can appear on multiple
releases: the API's selected album is only a candidate. Its title must also
agree with a majority of the local files' embedded album tags before any
tracks are downloaded. Missing or conflicting album tags are left alone.

Only existing local album folders are checked; this does not discover new
releases. A repair is counted only when the expected ISRC can be read from
an audio file in the original album folder after deemix completes. Exit 0
alone does not prove success. Repairs are not stored in a separate ledger;
subsequent runs check the actual files again.

ISRCs don't disappear on reissue, so a small folder (e.g. a 2-track single)
can have every one of its tracks correctly show up on a later, much bigger
album -- that's a genuine re-release relationship, not evidence the single
is an incomplete copy of the album. MIN_COMPLETION_RATIO guards against
"completing" a deliberately smaller release into a full album it was never
meant to be: only a local folder that's already a substantial fraction of
the matched release's tracklist gets topped up.

Two more identification safeguards, both found by comparing against a real
library: (1) the sample must produce a true majority (more than half of the
sampled tracks agreeing), not just a plurality -- a wide-ranging "Best Of"
compilation can scatter one vote each across many different official
albums, and picking whichever got merely one or two votes is not a real
identification. (2) a folder already holding more tracks than the matched
album contains is rejected outright -- that means the matched album is too
small to be the real source and this is probably a different, larger
compilation being compared against the wrong release. Rejected identifications
don't download anything.

done.txt is read-only here: this never removes, reorders, or appends to it.

Config (see .env / .env.example):
    STAGING_DIR   default --source (--output), the folder these artists were
                  downloaded into.
    BITRATE       default passed to deemix as --bitrate.

Examples:
    python3 check_missing_tracks.py --dry-run
    python3 check_missing_tracks.py -d
    python3 check_missing_tracks.py -s
    python3 check_missing_tracks.py -s ./music --dry-run
"""

import argparse
import json
import os
import unicodedata

from batch_safety import (OperationBusyError, inherited_lock_fds, operation_lock)
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import mutagen

DEEZER_API = "https://api.deezer.com"
AUDIO_EXTS = (".flac", ".mp3")
ISRC_SAMPLE_SIZE = 5
MIN_COMPLETION_RATIO = 0.7


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #
def load_dotenv(path=".env"):
    """Minimal .env loader. Real environment variables take precedence."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, val)


def read_artist_lines(path):
    with open(path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    return [ln for ln in lines if ln and not ln.startswith("#")]


# --------------------------------------------------------------------------- #
# local ISRC tags
# --------------------------------------------------------------------------- #
def read_isrc(path):
    """ISRC embedded in an audio file's tags, or None. Handles ID3 (TSRC,
    used by MP3) and Vorbis comments (ISRC, used by FLAC/Ogg)."""
    try:
        f = mutagen.File(path)
    except Exception:  # noqa: BLE001
        return None
    if f is None:
        return None
    if "TSRC" in f:
        val = str(f["TSRC"]).strip().upper()
        return val or None
    for key in ("isrc", "ISRC"):
        if key in f:
            vals = f[key]
            if vals:
                val = str(vals[0]).strip().upper()
                return val or None
    return None


def local_isrcs(folder):
    """{ISRC: filepath} for every audio file under folder, including
    multi-disc albums nested in CD1/CD2/-style subfolders."""
    result = {}
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if name.lower().endswith(AUDIO_EXTS):
                path = os.path.join(root, name)
                isrc = read_isrc(path)
                if isrc:
                    result[isrc] = path
    return result


# --------------------------------------------------------------------------- #
# Deezer metadata (public API, no auth)
# --------------------------------------------------------------------------- #
def deezer(path):
    url = path if path.startswith("http") else f"{DEEZER_API}{path}"
    last = None
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "check-missing-tracks/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read().decode("utf-8"))
            if isinstance(res, dict) and res.get("error"):
                last = res["error"]
                time.sleep(0.4 * (i + 1))
                continue
            return res
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(0.4 * (i + 1))
    raise RuntimeError(f"Deezer API failed: {url} ({last})")


def lookup_track_by_isrc(isrc):
    try:
        return deezer(f"/track/isrc:{urllib.parse.quote(isrc)}")
    except Exception:  # noqa: BLE001
        return None


def get_album_tracks(album_id):
    """[{'id', 'title', 'isrc', ...}] for every track in an album, paginated."""
    tracks, url = [], f"{DEEZER_API}/album/{album_id}/tracks?limit=100"
    while url:
        page = deezer(url)
        tracks.extend(page.get("data") or [])
        url = page.get("next")
        if url:
            time.sleep(0.15)
    return tracks


def normalized_album_title(title):
    """Normalize case, punctuation, and Unicode for conservative title checks."""
    return ''.join(character for character in
                   unicodedata.normalize("NFKC", title).casefold()
                   if character.isalnum())


def album_title_matches(isrc_map, album_title):
    """Require a majority of local ISRC-tagged files to name this album."""
    expected = normalized_album_title(album_title or "")
    if not expected or not isrc_map:
        return False
    matches = 0
    for path in isrc_map.values():
        try:
            tags = mutagen.File(path, easy=True)
            titles = tags.get("album", []) if tags is not None else []
        except Exception:  # Unreadable tags cannot establish an album identity.
            titles = []
        if any(normalized_album_title(title) == expected for title in titles):
            matches += 1
    return matches > len(isrc_map) / 2


def identify_album(isrc_map, sample_size=ISRC_SAMPLE_SIZE):
    """Majority-vote the Deezer album that a folder's local ISRCs belong to,
    from a spread-out sample (not every track, to keep API calls down for
    large albums) so one mistagged file can't misidentify the whole folder.
    Returns (album_id, album_title, votes, sampled) or (None, None, 0, 0) if
    no sampled ISRC resolved to anything."""
    isrcs = list(isrc_map)
    if len(isrcs) <= sample_size:
        sample = isrcs
    else:
        step = len(isrcs) / sample_size
        sample = [isrcs[int(i * step)] for i in range(sample_size)]

    votes = {}
    titles = {}
    for isrc in sample:
        track = lookup_track_by_isrc(isrc)
        album = (track or {}).get("album") or {}
        aid = album.get("id")
        if aid:
            votes[aid] = votes.get(aid, 0) + 1
            titles[aid] = album.get("title")
    if not votes:
        return None, None, 0, len(sample)
    album_id = max(votes, key=votes.get)
    return album_id, titles[album_id], votes[album_id], len(sample)


# --------------------------------------------------------------------------- #
def retry_track(deemix_exe, root, bitrate, track_id):
    """Run deemix on a single track URL. Returns (returncode, combined_output)."""
    url = f"https://www.deezer.com/track/{track_id}"
    cmd = [deemix_exe, "--bitrate", bitrate, "--path", root, url]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            pass_fds=inherited_lock_fds())
    return result.returncode, result.stdout + result.stderr


def parse_args():
    load_dotenv()
    p = argparse.ArgumentParser(
        description="Check existing local albums for every artist in "
                     "done.txt (identified by ISRC, not title matching) "
                     "and download exactly the tracks that are missing.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-d", "--done", nargs="?", const="done.txt", default="done.txt",
                   help="text file listing already-downloaded artists, one "
                        "per line -- read-only, never modified. Bare -d "
                        "(no filename) or omitting it entirely both mean "
                        "done.txt (default: done.txt)")
    p.add_argument("-s", "--source", "-o", "--output", dest="output", nargs="?",
                   const=os.environ.get("STAGING_DIR"),
                   default=os.environ.get("STAGING_DIR"),
                   help="local folder these artists were downloaded into; "
                        "bare -s or omitting it uses $STAGING_DIR from .env. "
                        "-o/--output remain available as aliases")
    p.add_argument("--bitrate", default=os.environ.get("BITRATE", "320"),
                   choices=["128", "320", "flac"],
                   help="audio quality for any missing tracks; should "
                        "normally match whatever the rest of the album was "
                        "downloaded at (default: $BITRATE from .env if "
                        "set, else 320)")
    p.add_argument("--deemix", default="deemix", metavar="EXECUTABLE",
                   help="deemix executable name or path (default: deemix)")
    p.add_argument("--dry-run", action="store_true",
                   help="report what's missing; don't run deemix or touch "
                        "any files")
    if len(sys.argv) == 1:
        p.print_help()
        sys.exit(0)
    return p.parse_args()


def find_artist_directory(output, artist):
    """Return an exact or unique case-insensitive artist folder match.

    Return None for absent or ambiguous matches rather than selecting an
    unrelated folder. Preserve the existing exact-match preference.
    """
    exact = os.path.join(output, artist)
    if os.path.isdir(exact):
        return exact
    with os.scandir(output) as entries:
        matches = [entry.path for entry in entries
                   if entry.is_dir() and entry.name.casefold() == artist.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"  ambiguous case-insensitive folder match for {artist}, skipping")
    return None


def run():
    args = parse_args()
    if not args.output:
        sys.exit("No source directory given, and STAGING_DIR isn't set in .env.")

    try:
        artists = read_artist_lines(args.done)
    except OSError as e:
        sys.exit(f"done.txt not found: {args.done} ({e})")
    if not artists:
        sys.exit(f"No artists found in {args.done}")

    deemix_exe = shutil.which(args.deemix)
    if deemix_exe is None and not args.dry_run:
        sys.exit(f"deemix executable not found: {args.deemix}")

    output = os.path.normpath(os.path.expanduser(args.output))
    if not os.path.isdir(output):
        sys.exit(f"Source directory not found: {output}")

    checked = topped_up = unresolved = 0
    for artist in artists:
        print(f"=== {artist} ===")
        artist_dir = find_artist_directory(output, artist)
        if artist_dir is None:
            print(f"  no unique local folder found for {artist} under {output}, skipping")
            continue

        local_albums = sorted(d for d in os.listdir(artist_dir)
                              if os.path.isdir(os.path.join(artist_dir, d)))
        if not local_albums:
            print("  no local album folders, skipping")
            continue

        for local_name in local_albums:
            local_path = os.path.join(artist_dir, local_name)
            isrc_map = local_isrcs(local_path)
            if not isrc_map:
                print(f"  {local_name}: no ISRC-tagged audio files found, leaving alone")
                continue

            album_id, album_title, votes, sampled = identify_album(isrc_map)
            if album_id is None:
                print(f"  {local_name}: couldn't identify the Deezer album "
                      f"from local ISRCs, leaving alone")
                continue
            if votes <= sampled / 2:
                print(f"  {local_name}: only {votes}/{sampled} sampled local "
                      f"tracks agree this is '{album_title}' -- not a real "
                      f"majority, can't reliably identify this folder, "
                      f"leaving alone")
                continue
            if votes < sampled:
                print(f"  {local_name}: {votes}/{sampled} sampled local "
                      f"tracks agree this is '{album_title}' (id {album_id}); "
                      f"the rest look like a different release mixed in here")

            if not album_title_matches(isrc_map, album_title):
                print(f"  {local_name}: ISRC lookup selected '{album_title}', "
                      "but local album tags do not confirm that release; leaving alone")
                continue

            checked += 1
            try:
                tracks = get_album_tracks(album_id)
            except Exception as e:  # noqa: BLE001
                print(f"  {local_name}: couldn't fetch tracklist for "
                      f"'{album_title}': {e}")
                unresolved += 1
                continue

            if len(isrc_map) > len(tracks):
                print(f"  {local_name}: already has {len(isrc_map)} tracks, "
                      f"more than '{album_title}' contains ({len(tracks)}) "
                      f"-- this is probably a different/larger release, not "
                      f"a match; leaving alone")
                continue

            missing = [t for t in tracks
                      if (t.get("isrc") or "").strip().upper() not in isrc_map]
            if not missing:
                continue
            unidentifiable = [track for track in missing if not track.get("isrc")]
            if unidentifiable:
                print(f"  {local_name}: {len(unidentifiable)} track(s) lack ISRC "
                      "metadata; cannot verify or retry them safely")
                unresolved += len(unidentifiable)
                missing = [track for track in missing if track.get("isrc")]
            if not missing:
                continue

            # A small local folder (e.g. a 2-track single) can have every
            # one of its tracks correctly show up on a later, much bigger
            # album by ISRC -- that's a genuine re-release, not evidence
            # the single itself is an incomplete copy of the album. Only
            # top up when what's local is already a substantial fraction
            # of the matched release.
            if tracks and len(isrc_map) / len(tracks) < MIN_COMPLETION_RATIO:
                print(f"  {local_name}: {len(isrc_map)}/{len(tracks)} tracks "
                      f"match '{album_title}', but that's likely a smaller "
                      f"release (single/EP) that happens to share tracks "
                      f"with it, not an incomplete copy; leaving alone")
                continue

            print(f"  {local_name}: identified as '{album_title}' "
                  f"({len(isrc_map)}/{len(tracks)} tracks, "
                  f"{len(missing)} missing)")
            if args.dry_run:
                for t in missing:
                    print(f"    [dry-run] would download track {t['id']} - {t.get('title')}")
                continue

            fixed = 0
            for t in missing:
                print(f"    downloading track {t['id']} - {t.get('title')} ...")
                returncode, out = retry_track(deemix_exe, output, args.bitrate, t["id"])
                expected_isrc = (t.get("isrc") or "").strip().upper()
                if (returncode == 0 and expected_isrc
                        and expected_isrc in local_isrcs(local_path)):
                    fixed += 1
                else:
                    if returncode == 0:
                        print("      not fixed: expected ISRC not found in this "
                              "album after deemix exited 0")
                    else:
                        print(f"      still failing (exit {returncode})")
                    for line in out.splitlines()[-5:]:
                        print(f"        {line}")
            unresolved += len(missing) - fixed
            topped_up += fixed
            print(f"    fixed {fixed}/{len(missing)} missing track(s)")

    if args.dry_run:
        print(f"\nDRY RUN: checked {checked} local album(s).")
    else:
        print(f"\nDone. Checked {checked} local album(s), fixed {topped_up} track(s).")
    return int(unresolved > 0 and not args.dry_run)


def main():
    """Run exclusively against sync and other download/repair entry points."""
    if len(sys.argv) == 1 or "--help" in sys.argv or "-h" in sys.argv:
        return run()
    try:
        with operation_lock():
            return run()
    except (OperationBusyError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
