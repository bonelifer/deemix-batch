#!/usr/bin/env python3
"""
Scan a directory tree for deemix's per-album errors.txt files (written when
some tracks in that album failed to download) and retry just the failed
tracks -- not the whole album -- via a direct "deemix --path <root>
https://www.deezer.com/track/<id>" call per track. deemix's own
folder-naming template is deterministic from track/album/artist metadata,
so a retried track lands back in the same album folder it already created,
regardless of whether that folder currently sits under STAGING_DIR (not yet
synced) or under DEST_PATH (already synced) -- --source can point at either.

Retry success requires the expected ISRC to be present in the original
album folder. Public track metadata supplies that ISRC; an unavailable
lookup preserves the error for a later pass. Exit 0 alone is insufficient.
Error records are rewritten atomically, including after interrupted retries.
Requires mutagen (also used by deemix) for MP3/FLAC tag verification.

Config (see .env / .env.example):
    STAGING_DIR   default --source to scan (and the --path handed to
                  deemix for each retry).
    BITRATE       default passed to deemix as --bitrate.

Examples:
    python3 retry_failed_tracks.py --dry-run
    python3 retry_failed_tracks.py
    python3 retry_failed_tracks.py --source ./music --bitrate flac
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

from batch_safety import (OperationBusyError, album_has_isrc, atomic_write_text,
                          inherited_lock_fds, operation_lock, track_isrc)

TRACK_LINE_RE = re.compile(r"^(\d+) \| (.+)$")


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


# --------------------------------------------------------------------------- #
# errors.txt
# --------------------------------------------------------------------------- #
def find_error_files(root):
    for dirpath, _dirs, files in os.walk(root):
        if "errors.txt" in files:
            yield os.path.join(dirpath, "errors.txt")


def read_error_lines(path):
    with open(path, encoding="utf-8") as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


def write_error_lines(path, lines):
    """Rewrite path with lines, or delete it entirely if lines is empty."""
    if lines:
        atomic_write_text(path, "\r\n".join(lines) + "\r\n")
    elif os.path.exists(path):
        os.remove(path)


def retry_track(deemix_exe, root, bitrate, track_id):
    """Run deemix on a single track URL. Returns (returncode, combined_output)."""
    url = f"https://www.deezer.com/track/{track_id}"
    cmd = [deemix_exe, "--bitrate", bitrate, "--path", root, url]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            pass_fds=inherited_lock_fds())
    return result.returncode, result.stdout + result.stderr


# --------------------------------------------------------------------------- #
def parse_args():
    load_dotenv()
    p = argparse.ArgumentParser(
        description="Retry just the failed tracks listed in deemix's "
                     "errors.txt files, without re-downloading whole albums.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-s", "--source", nargs="?",
                   const=os.environ.get("STAGING_DIR"),
                   default=os.environ.get("STAGING_DIR"),
                   help="directory to scan for errors.txt files, searched "
                        "recursively; also passed to deemix as --path for "
                        "each retry, so tracks land back in the same album "
                        "folder deemix already created. Works whether the "
                        "content is still in STAGING_DIR or has already "
                        "been synced to DEST_PATH. Bare -s (no path) or "
                        "omitting it entirely both mean $STAGING_DIR from "
                        ".env, if set (default: $STAGING_DIR from .env, if set)")
    p.add_argument("--bitrate", default=os.environ.get("BITRATE", "320"),
                   choices=["128", "320", "flac"],
                   help="audio quality to retry at; should normally match "
                        "whatever the rest of the album was downloaded at "
                        "(default: $BITRATE from .env if set, else 320)")
    p.add_argument("--deemix", default="deemix", metavar="EXECUTABLE",
                   help="deemix executable name or path (default: deemix)")
    p.add_argument("--dry-run", action="store_true",
                   help="list what would be retried; don't run deemix or "
                        "touch any files")
    if len(sys.argv) == 1:
        p.print_help()
        sys.exit(0)
    return p.parse_args()


def run():
    args = parse_args()
    if not args.source:
        sys.exit("No source directory given, and STAGING_DIR isn't set in .env.")
    source = os.path.normpath(os.path.expanduser(args.source))
    if not os.path.isdir(source):
        sys.exit(f"Source directory not found: {source}")

    deemix_exe = shutil.which(args.deemix)
    if deemix_exe is None and not args.dry_run:
        sys.exit(f"deemix executable not found: {args.deemix}")

    error_files = sorted(find_error_files(source))
    if not error_files:
        print(f"No errors.txt files found under {source}.")
        return

    import mutagen  # Fail before retrying if tag verification is unavailable.

    total_tracks = total_fixed = unresolved = 0
    for errfile in error_files:
        lines = read_error_lines(errfile)
        album_dir = os.path.dirname(errfile)
        print(f"=== {album_dir} ({len(lines)} failed track(s)) ===")
        remaining = list(lines)

        try:
            for line in lines:
                match = TRACK_LINE_RE.match(line)
                if not match:
                    print(f"  (skipping unrecognized line: {line!r})")
                    unresolved += 1
                    continue
                track_id, desc = match.group(1), match.group(2)
                total_tracks += 1
                if args.dry_run:
                    print(f"  [dry-run] would retry {track_id} | {desc}")
                    continue
                try:
                    expected = track_isrc(track_id)
                except Exception as exc:
                    print(f"  cannot verify {track_id}; retaining error: {exc}")
                    unresolved += 1
                    continue
                if album_has_isrc(album_dir, expected):
                    print(f"  {track_id}: verified already present")
                    remaining.remove(line)
                    total_fixed += 1
                    continue
                print(f"  retrying {track_id} | {desc} ...")
                returncode, output = retry_track(
                    deemix_exe, source, args.bitrate, track_id)
                if returncode == 0 and album_has_isrc(album_dir, expected):
                    print("    fixed (verified local ISRC)")
                    remaining.remove(line)
                    total_fixed += 1
                else:
                    unresolved += 1
                    print(f"    not fixed (exit {returncode}; local ISRC not verified)")
                    for out_line in output.splitlines()[-5:]:
                        print(f"      {out_line}")
        finally:
            if not args.dry_run:
                write_error_lines(errfile, remaining)

    if args.dry_run:
        print(f"\nDRY RUN: {total_tracks} failed track(s) across "
              f"{len(error_files)} album(s) would be retried.")
    else:
        print(f"\nDone. {total_fixed}/{total_tracks} track(s) fixed "
              f"across {len(error_files)} album(s).")
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
