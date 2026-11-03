#!/usr/bin/env python3
"""Shared atomic file writes and locking for local download/sync operations."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import urllib.request

LOCK_PATH = Path(__file__).resolve().parent / 'state' / 'operation.lock'
LOCK_ENV = 'DEEMIX_BATCH_LOCK_FD'


class OperationBusyError(RuntimeError):
    """Another download, repair, or sync operation holds the shared lock."""


def inherited_lock_fds():
    """Return a verified inherited operation-lock descriptor, if present."""
    try:
        fd = int(os.environ[LOCK_ENV])
        opened = os.fstat(fd)
        expected = LOCK_PATH.stat()
        if (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino):
            return (fd,)
    except (KeyError, ValueError, OSError):
        pass
    return ()


@contextmanager
def operation_lock():
    """Exclude concurrent operations; children retain ownership if needed.

    A verified inherited descriptor allows the batch's nested downloader to
    reuse its lock rather than deadlocking against its parent.
    """
    if inherited_lock_fds():
        yield
        return
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get(LOCK_ENV)
    with LOCK_PATH.open('a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OperationBusyError(
                'Another download, repair, or sync is running; try again after it finishes.'
            ) from exc
        os.set_inheritable(stream.fileno(), True)
        os.environ[LOCK_ENV] = str(stream.fileno())
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(LOCK_ENV, None)
            else:
                os.environ[LOCK_ENV] = previous
            # Closing releases ownership once all inheriting children exit.


def atomic_write_text(path, text):
    """Replace a text file atomically, preserving its mode and symlink target."""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', newline='',
                dir=target.parent, prefix=f'.{target.name}-', delete=False) as stream:
            temporary = Path(stream.name)
            if target.exists():
                os.chmod(temporary, target.stat().st_mode & 0o777)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_json(path, value):
    """Atomically store JSON with the existing project's indentation/order."""
    atomic_write_text(path, json.dumps(value, indent=1, sort_keys=True))


def track_isrc(track_id):
    """Fetch a track's ISRC without credentials; fail if it cannot be verified."""
    if not str(track_id).isdigit():
        raise ValueError('Track ID must be numeric')
    url = f'https://api.deezer.com/track/{track_id}'
    request = urllib.request.Request(url, headers={'User-Agent': 'deemix-batch/1.0'})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    if not isinstance(data, dict) or data.get('error') or not data.get('isrc'):
        raise ValueError(f'No usable ISRC returned for track {track_id}')
    return str(data['isrc']).strip().upper()


def album_has_isrc(folder, expected):
    """Check readable MP3/FLAC tags in an album, including nested disc folders."""
    import mutagen

    if not expected:
        return False
    for root, _directories, files in os.walk(folder):
        for name in files:
            if not name.lower().endswith(('.mp3', '.flac')):
                continue
            try:
                tags = mutagen.File(os.path.join(root, name), easy=True)
                values = tags.get('isrc', []) if tags is not None else []
                if any(str(value).strip().upper() == expected for value in values):
                    return True
            except (OSError, mutagen.MutagenError):
                continue
    return False
