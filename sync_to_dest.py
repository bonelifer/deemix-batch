#!/usr/bin/env python3
"""
Move finished artist folders from a local download staging directory to a
destination folder -- a NAS mount, a MusicBrainz Picard inbox for fixing and
organizing tags, or any other local directory.

This is meant to run after a synchronous downloader (e.g.
deemix_artist_download_all.py, which shells out to the deemix CLI and blocks
until each artist's download finishes). There is no download queue to poll
here; instead, a folder is only moved once nothing inside it has changed for
--stable-for seconds, so a folder the downloader is still actively writing to
is never touched (moving files out from under it would interfere with the
download). A skipped-as-active folder is simply picked up on a later run.

merge_move() is adapted from deemix-bulk's deemix_sync.py (MIT License,
Copyright (c) 2026 Adam Reue, https://github.com/mylegitches/deemix-bulk).

Config (see .env / .env.example), all overridable by their matching flag:
    STAGING_DIR    default for --source.
    DEST_PATH      local destination directory (NAS mount, Picard inbox,
                   whatever); must already exist -- this script does not
                   mount or create it.
    DEST_SUBPATH   optional path under DEST_PATH, e.g. media/music.
    COPY           default for --copy/--no-copy.
    STABLE_FOR     default for --stable-for.

Examples:
    python3 sync_to_dest.py --source ~/deemix-downloads --dry-run
    python3 sync_to_dest.py --source ~/deemix-downloads
    python3 sync_to_dest.py --source ~/deemix-downloads --copy
"""

import argparse
import errno
import json
import logging
import os
import shutil
import sys
import time
import tempfile
from pathlib import Path
from datetime import datetime

from batch_safety import (OperationBusyError, atomic_write_json, operation_lock)

STATE_DIR = "state"
SYNCED_STATE = os.path.join(STATE_DIR, "synced.json")
DOWNLOAD_STATUS_FILE = os.path.join(STATE_DIR, "download_status.json")


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


def env_int(name, default):
    """Read an integer .env setting, exiting with a clear error if it's not one."""
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        sys.exit(f"Invalid {name} in .env: {val!r} (must be an integer)")


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
def setup_logging(name, logdir="logs", level=logging.INFO):
    """Console + timestamped file logger. Returns (logger, logfile_path)."""
    os.makedirs(logdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile = os.path.join(logdir, f"{name}_{ts}.log")

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.info("log file: %s", logfile)
    return logger, logfile


# --------------------------------------------------------------------------- #
# state (which artist folders we've already moved)
# --------------------------------------------------------------------------- #
def load_synced():
    try:
        with open(SYNCED_STATE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_synced(synced):
    os.makedirs(STATE_DIR, exist_ok=True)
    atomic_write_json(SYNCED_STATE, sorted(synced))


def load_download_status():
    """{artist: {"exit_code", "finished_at", "query"}}, written by
    download_artists.py. Empty if that script was never used -- callers fall
    back to the mtime-staleness check in that case."""
    try:
        with open(DOWNLOAD_STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def newest_mtime(path):
    """Most recent modification time of any file under path (recursive)."""
    newest = os.path.getmtime(path)
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, name)))
            except OSError:
                pass
    return newest


# --------------------------------------------------------------------------- #
# move
# --------------------------------------------------------------------------- #
def validate_sync_paths(src, dst):
    """Reject equal/nested roots, including aliases reached through symlinks."""
    source = Path(src).resolve()
    destination = Path(dst).resolve()
    if (source == destination or source in destination.parents
            or destination in source.parents):
        raise ValueError("Source and destination must not overlap")


def merge_move(src, dst, copy_only=False):
    """Merge safely, rejecting symlinks and replacing files atomically."""
    validate_sync_paths(src, dst)
    if os.path.islink(src) or os.path.islink(dst):
        raise ValueError("Refusing to sync through a symbolic link")
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            merge_move(os.path.join(src, name), os.path.join(dst, name), copy_only)
        if not copy_only:
            os.rmdir(src)
        return

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if copy_only and os.path.isfile(dst):
        source_stat, target_stat = os.stat(src), os.stat(dst)
        if (source_stat.st_size, source_stat.st_mtime_ns) == (
                target_stat.st_size, target_stat.st_mtime_ns):
            return
    if not copy_only:
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".deemix-sync-", dir=os.path.dirname(dst))
        os.close(descriptor)
        shutil.copy2(src, temporary)
        with open(temporary, 'rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, dst)
        if not copy_only:
            os.unlink(src)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


# --------------------------------------------------------------------------- #
def parse_args():
    load_dotenv()
    p = argparse.ArgumentParser(
        description="Move finished artist folders from a local staging "
                     "directory to a destination folder (NAS mount, Picard "
                     "inbox, or any other local directory).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-s", "--source", default=os.environ.get("STAGING_DIR"),
                   required=os.environ.get("STAGING_DIR") is None,
                   help="local folder where downloaded music currently sits "
                        "(the same folder you passed to download_artists.py's "
                        "-o); default comes from STAGING_DIR in .env if set")
    p.add_argument("--copy", action=argparse.BooleanOptionalAction,
                   default=env_bool("COPY"),
                   help="copy files to the destination and keep the local "
                        "copies too, instead of moving them (default: move, "
                        "which deletes the local copy once it's moved; or "
                        "$COPY from .env if set -- pass --no-copy to move "
                        "just for this run even if COPY=true in .env)")
    p.add_argument("--dry-run", action="store_true",
                   help="only print what would be moved/copied; don't touch any files")
    p.add_argument("--stable-for", type=int, default=env_int("STABLE_FOR", 300),
                   help="only move/copy a folder once nothing inside has "
                        "changed for this many seconds, in case a download "
                        "into it is still in progress (default: 300, or "
                        "$STABLE_FOR from .env if set)")
    if len(sys.argv) == 1:
        p.print_help()
        sys.exit(0)
    return p.parse_args()


def run():
    args = parse_args()
    log, _ = setup_logging("sync_to_dest")

    source = os.path.normpath(os.path.expanduser(args.source))
    if not os.path.isdir(source):
        sys.exit(f"Source staging directory not found: {source}")

    dest_path = os.environ.get("DEST_PATH")
    dest_rel = os.environ.get("DEST_SUBPATH", "").strip("/")
    dest_base = os.path.join(dest_path, dest_rel) if dest_path and dest_rel else dest_path
    if dest_base:
        try:
            validate_sync_paths(source, dest_base)
        except ValueError as exc:
            sys.exit(str(exc))
    if args.stable_for < 0:
        sys.exit("--stable-for must be nonnegative")
    if args.dry_run:
        log.info("DRY RUN - nothing will be moved")
    else:
        if not dest_path:
            sys.exit("Missing DEST_PATH in .env (the destination directory).")
        if not os.path.isdir(dest_path):
            sys.exit(f"DEST_PATH does not exist: {dest_path}")
        dest_base = os.path.join(dest_path, dest_rel) if dest_rel else dest_path
        log.info("destination: %s  (%s)", dest_base, "copy" if args.copy else "move")

    synced = load_synced()
    entries = sorted(e for e in os.listdir(source) if os.path.isdir(os.path.join(source, e)))
    if not entries:
        log.info("nothing to sync in %s", source)
        return

    moved = failed = 0
    for artist in entries:
        src = os.path.join(source, artist)
        # Historical completion is informational; every current folder must
        # be stable, and the operation lock excludes current writers.
        try:
            age = time.time() - newest_mtime(src)
        except OSError as exc:
            log.warning("could not check %s, skipping: %s", artist, exc)
            failed += 1
            continue
        if age < args.stable_for:
            log.info("skipping %s: modified %ds ago (< %ds stability window)",
                     artist, int(age), args.stable_for)
            continue

        if args.dry_run:
            log.info("[dry-run] would %s %s -> <DEST>/%s",
                     "copy" if args.copy else "move", src, artist)
            continue

        dst = os.path.join(dest_base, artist)
        try:
            merge_move(src, dst, copy_only=args.copy)
            log.info("%s: %s -> %s", "copied" if args.copy else "moved", artist, dst)
            synced.add(artist)
            save_synced(synced)
            moved += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.error("move FAILED for %s: %s", artist, e)

    if not args.dry_run:
        log.info("done. %d synced, %d failed.", moved, failed)
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
