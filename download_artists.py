#!/usr/bin/env python3
"""
Call a synchronous per-artist downloader once per artist (instead of
batching), so its exit code becomes a real per-artist signal, and record each
result in a small status cache that sync_to_dest.py checks before moving a
folder.

Written against deemix_artist_download_all.py's CLI shape: it prints
"Selected <name> (ID: <id>)" for each resolved artist, and accepts an artist
name/ID as a positional arg plus -o/--output. The resolved <name> (parsed
from that line) is used as the status-cache key, since that -- not
necessarily the raw input line -- is what should match the folder deemix
creates on disk.

Writes state/download_status.json:
    {resolved_artist_name: {"exit_code": N, "finished_at": ISO8601, "query": raw_input_line}}

A non-zero exit code is recorded but does NOT stop the loop or block that
artist from being picked up by sync_to_dest.py -- this script only records
what happened, it doesn't gate anything.

On success (exit 0), the artist's line is removed from the artist list file
and appended to done.txt next to it, so a re-run only retries what's left. A
failed artist's line stays in the artist list for the next run.

STAGING_DIR, BITRATE, and DISCOGRAPHY in .env supply defaults for -o,
--bitrate, and --discography, so they don't need to be typed every run.
An explicit -o/--bitrate/--discography (or --no-discography) on the command
line still wins.

Usage:
    python3 download_artists.py -a artists.txt -o ~/deemix-downloads \\
        -- --bitrate flac --discography
    python3 download_artists.py --downloader /other/path.py -a artists.txt -o ~/deemix-downloads
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from batch_safety import (OperationBusyError, atomic_write_json,
                          atomic_write_text, inherited_lock_fds, operation_lock)

STATE_DIR = "state"
STATUS_FILE = os.path.join(STATE_DIR, "download_status.json")
DEFAULT_DOWNLOADER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "deemix_artist_download_all.py"
)

SELECTED_RE = re.compile(r"^Selected (.+) \(ID: \d+\)$")


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


def env_bool(name, default=False):
    """Read a boolean .env setting; "1"/"true"/"yes"/"on" (any case) is True."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_status():
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_status(status):
    os.makedirs(STATE_DIR, exist_ok=True)
    atomic_write_json(STATUS_FILE, status)


def read_artist_lines(path):
    with open(path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def remove_artist_line(path, artist):
    """Remove the first line matching artist (after stripping) from path."""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    removed = False
    kept = []
    for line in lines:
        if not removed and line.strip() == artist:
            removed = True
            continue
        kept.append(line)
    atomic_write_text(path, ''.join(kept))


def append_done(path, artist):
    """Record completion before removing the queue item; reruns are idempotent."""
    try:
        with open(path, encoding="utf-8") as stream:
            existing = stream.read()
    except FileNotFoundError:
        existing = ""
    if artist in (line.strip() for line in existing.splitlines()):
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    atomic_write_text(path, existing + separator + artist + "\n")


def parse_args():
    load_dotenv()
    p = argparse.ArgumentParser(
        description="Call a per-artist downloader once per artist and record "
                     "a completion status sync_to_dest.py can check.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-a", "--artists", nargs="?", const="artists.txt", default="artists.txt",
                   help="text file listing artists to download, one artist "
                        "name or Deezer artist ID per line. Bare -a (no "
                        "filename) or omitting it entirely both mean "
                        "artists.txt (default: artists.txt)")
    p.add_argument("-o", "--output", default=os.environ.get("STAGING_DIR"),
                   required=os.environ.get("STAGING_DIR") is None,
                   help="local folder to save downloaded music into; passed "
                        "straight through as the downloader script's own -o "
                        "(default: $STAGING_DIR from .env, if set)")
    p.add_argument("--debug", action="store_true",
                   help="stream unbuffered child output and show command, PID, "
                        "elapsed time, and a heartbeat every 30 seconds")
    p.add_argument("downloader_args", nargs=argparse.REMAINDER,
                   help="extra command-line options to pass straight through "
                        "to the downloader script. Write a -- by itself first, "
                        "so this script knows everything after it belongs to "
                        "the downloader and not to itself, "
                        "e.g. -- --bitrate flac --discography "
                        "(--bitrate and --discography can also come from "
                        "BITRATE and DISCOGRAPHY in .env, if you always want "
                        "them and don't want to type them every run; pass "
                        "--no-discography here to turn DISCOGRAPHY=true back "
                        "off just for this run)")

    advanced = p.add_argument_group(
        "advanced",
        "Override which downloader/interpreter gets used; the defaults are fine for normal use.")
    advanced.add_argument("--downloader", default=DEFAULT_DOWNLOADER,
                          help="path to the script that does the actual Deezer/deemix "
                               "downloading for one artist at a time (default: "
                               "deemix_artist_download_all.py, right next to this script)")
    advanced.add_argument("--python", default=sys.executable,
                          help="Python interpreter to run the downloader script with "
                               "(default: this script's own interpreter)")
    if len(sys.argv) == 1:
        p.print_help()
        sys.exit(0)
    return p.parse_args()


def run_one(python, downloader, artist, output, extra, debug=False):
    """Stream one artist's output and return (resolved_name, returncode).

    Debug mode disables child Python buffering and reports progress while
    waiting; it does not impose a download timeout.
    """
    cmd = [python, downloader, artist, "-o", output, *extra]
    environment = os.environ.copy()
    started = time.monotonic()
    last_output = started
    if debug:
        environment["PYTHONUNBUFFERED"] = "1"
        environment["DEEMIX_BATCH_DEBUG"] = "1"
        print(f"[debug] command: {shlex.join(cmd)}", file=sys.stderr, flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            env=environment, pass_fds=inherited_lock_fds())
    finished = threading.Event()

    def report_wait():
        """Report waits without reading or changing the child's state."""
        while not finished.wait(30):
            now = time.monotonic()
            print(f"[debug] PID {proc.pid}: elapsed {now - started:.0f}s; "
                  f"no output for {now - last_output:.0f}s",
                  file=sys.stderr, flush=True)

    reporter = None
    if debug:
        print(f"[debug] started PID {proc.pid}", file=sys.stderr, flush=True)
        reporter = threading.Thread(target=report_wait, daemon=True)
        reporter.start()
    resolved = artist
    try:
        for line in proc.stdout:
            last_output = time.monotonic()
            print(line, end="", flush=debug)
            m = SELECTED_RE.match(line.strip())
            if m:
                resolved = m.group(1)
        proc.wait()
    finally:
        finished.set()
        if reporter is not None:
            reporter.join()
        proc.stdout.close()
    if debug:
        print(f"[debug] PID {proc.pid}: exit {proc.returncode}; "
              f"elapsed {time.monotonic() - started:.1f}s",
              file=sys.stderr, flush=True)
    return resolved, proc.returncode


def run():
    args = parse_args()
    extra = args.downloader_args
    if extra and extra[0] == "--":
        extra = extra[1:]

    bitrate = os.environ.get("BITRATE")
    if bitrate and "--bitrate" not in extra and "-b" not in extra:
        extra = ["--bitrate", bitrate, *extra]

    if env_bool("DISCOGRAPHY") and "--discography" not in extra and "--no-discography" not in extra:
        extra = ["--discography", *extra]

    if not os.path.isfile(args.downloader):
        sys.exit(f"Downloader script not found: {args.downloader}")

    try:
        artists = read_artist_lines(args.artists)
    except OSError as e:
        sys.exit(f"Artist file not found: {args.artists} ({e})")
    if not artists:
        sys.exit(f"No artists found in {args.artists}")

    done_path = os.path.join(os.path.dirname(os.path.abspath(args.artists)), "done.txt")

    preview = "--dry-run" in extra or "--list" in extra
    status = load_status()
    failed = 0
    for artist in artists:
        print(f"=== {artist} ===")
        resolved, returncode = run_one(args.python, args.downloader, artist, args.output, extra,
                                        debug=args.debug)

        if returncode != 0:
            failed += 1
        if preview:
            continue

        status[resolved] = {
            "exit_code": returncode,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query": artist,
        }
        save_status(status)

        if returncode == 0:
            append_done(done_path, artist)
            remove_artist_line(args.artists, artist)
        else:
            print(f"  FAILED (exit {returncode}) -- see output above; left in "
                  f"{args.artists} for retry; not blocked from syncing, just recorded",
                  file=sys.stderr)

    if preview:
        print("Preview complete; artist lists and completion status unchanged.")
        return int(failed > 0)
    ok = sum(1 for v in status.values() if v.get("exit_code") == 0)
    print(f"\nDone. {ok}/{len(status)} artist(s) in status cache recorded exit 0.")
    return int(failed > 0)


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
