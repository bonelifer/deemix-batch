"""Regression checks for queue, repair, locking, and sync data safety."""

import contextlib
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import batch_safety as safety
import check_missing_tracks as missing
import download_artists as batch
import retry_failed_tracks as retry
import sync_to_dest as sync


class SafetyTests(unittest.TestCase):
    """Use disposable trees and stub only external download/network boundaries."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.output = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))

    def test_atomic_write_failure_preserves_original(self):
        target = self.root / 'state.json'
        target.write_text('original')
        with patch.object(safety.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                safety.atomic_write_text(target, 'replacement')
        self.assertEqual(target.read_text(), 'original')
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_lock_excludes_independent_process_and_allows_nested_child(self):
        lock = self.root / 'operation.lock'
        code = ('import batch_safety as s; from pathlib import Path; '
                f's.LOCK_PATH = Path({str(lock)!r}); '
                '\ntry:\n with s.operation_lock(): print("acquired")'
                '\nexcept s.OperationBusyError:\n print("busy")')
        env = {**os.environ, 'PYTHONPATH': str(ROOT)}
        env.pop(safety.LOCK_ENV, None)
        with patch.object(safety, 'LOCK_PATH', lock), safety.operation_lock():
            blocked = subprocess.run([sys.executable, '-c', code], env=env,
                                     capture_output=True, text=True, check=True)
            self.assertEqual(blocked.stdout.strip(), 'busy')
            child_env = {**env, safety.LOCK_ENV: os.environ[safety.LOCK_ENV]}
            nested = subprocess.run([sys.executable, '-c', code], env=child_env,
                                    pass_fds=safety.inherited_lock_fds(),
                                    capture_output=True, text=True, check=True)
            self.assertEqual(nested.stdout.strip(), 'acquired')
        released = subprocess.run([sys.executable, '-c', code], env=env,
                                  capture_output=True, text=True, check=True)
        self.assertEqual(released.stdout.strip(), 'acquired')

    def test_child_retains_lock_after_parent_context_exits(self):
        lock = self.root / 'operation.lock'
        code = ('import batch_safety as s; from pathlib import Path; '
                f's.LOCK_PATH = Path({str(lock)!r}); '
                '\ntry:\n with s.operation_lock(): print("acquired")'
                '\nexcept s.OperationBusyError:\n print("busy")')
        env = {**os.environ, 'PYTHONPATH': str(ROOT)}
        env.pop(safety.LOCK_ENV, None)
        child = None
        try:
            with patch.object(safety, 'LOCK_PATH', lock), safety.operation_lock():
                child = subprocess.Popen(
                    [sys.executable, '-c', 'import sys; sys.stdin.read(1)'],
                    stdin=subprocess.PIPE, pass_fds=safety.inherited_lock_fds())
            blocked = subprocess.run([sys.executable, '-c', code], env=env,
                                     capture_output=True, text=True, check=True)
            self.assertEqual(blocked.stdout.strip(), 'busy')
            child.communicate(input=b'x', timeout=5)
            released = subprocess.run([sys.executable, '-c', code], env=env,
                                      capture_output=True, text=True, check=True)
            self.assertEqual(released.stdout.strip(), 'acquired')
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate()

    def test_real_batch_preview_cli_preserves_queue(self):
        artist_file = self.root / 'artists.txt'
        artist_file.write_text('Artist\n')
        child = self.root / 'fake_downloader.py'
        child.write_text('print("Selected Artist (ID: 123)")\n')
        result = subprocess.run(
            [sys.executable, str(ROOT / 'download_artists.py'),
             '-a', str(artist_file), '-o', str(self.root / 'music'),
             '--downloader', str(child), '--', '--dry-run'],
            cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(artist_file.read_text(), 'Artist\n')
        self.assertFalse((self.root / 'done.txt').exists())
        self.assertFalse((self.root / 'state' / 'download_status.json').exists())

    def test_atomic_json_keeps_format_and_unknown_fields(self):
        target = self.root / 'status.json'
        data = {'Artist': {'extra': 'preserved', 'exit_code': 0}}
        safety.atomic_write_json(target, data)
        self.assertEqual(json.loads(target.read_text()), data)

    def test_equal_nested_and_alias_sync_roots_rejected(self):
        source = self.root / 'source'
        source.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(source, target_is_directory=True)
        for destination in [source, source / 'nested', self.root, alias]:
            with self.subTest(destination=destination):
                with self.assertRaises(ValueError):
                    sync.validate_sync_paths(source, destination)
        sync.validate_sync_paths(source, self.root / 'other')

    def test_equal_file_sync_does_not_delete_source(self):
        source = self.root / 'original.mp3'
        source.write_bytes(b'original')
        with self.assertRaises(ValueError):
            sync.merge_move(str(source), str(source), copy_only=True)
        self.assertEqual(source.read_bytes(), b'original')

    def test_failed_copy_keeps_both_files(self):
        source = self.root / 'source.mp3'
        target = self.root / 'target.mp3'
        source.write_bytes(b'new data')
        target.write_bytes(b'old')
        with patch.object(sync.shutil, 'copy2', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                sync.merge_move(str(source), str(target), copy_only=True)
        self.assertEqual(source.read_bytes(), b'new data')
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ['source.mp3', 'target.mp3'])

    def test_move_and_cross_device_fallback(self):
        for cross_device in [False, True]:
            with self.subTest(cross_device=cross_device):
                source = self.root / 'source.mp3'
                target = self.root / 'target.mp3'
                source.write_bytes(b'new data')
                target.write_bytes(b'old')
                replace = os.replace
                calls = 0

                def simulate_move(src, dst):
                    nonlocal calls
                    calls += 1
                    if cross_device and calls == 1:
                        raise OSError(errno.EXDEV, 'different filesystem')
                    return replace(src, dst)

                with patch.object(sync.os, 'replace', side_effect=simulate_move):
                    sync.merge_move(str(source), str(target))
                self.assertFalse(source.exists())
                self.assertEqual(target.read_bytes(), b'new data')

    def test_sync_rejects_nested_symbolic_link(self):
        source = self.root / 'source'
        target = self.root / 'target'
        outside = self.root / 'outside'
        source.mkdir()
        outside.mkdir()
        (outside / 'keep.mp3').write_bytes(b'keep')
        (source / 'alias').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            sync.merge_move(str(source), str(target))
        self.assertEqual((outside / 'keep.mp3').read_bytes(), b'keep')

    def batch_args(self, extra=None):
        artists = self.root / 'artists.txt'
        artists.write_text('Artist\n')
        return SimpleNamespace(downloader_args=extra or [],
                               downloader=str(ROOT / 'deemix_artist_download_all.py'),
                               artists=str(artists), python='unused',
                               output=str(self.root / 'music'), debug=False)

    def run_batch(self, args, result):
        with patch.object(batch, 'parse_args', return_value=args), \
                patch.object(batch, 'run_one', return_value=('Artist', result)), \
                patch.object(batch, 'STATUS_FILE', str(self.root / 'status.json')), \
                patch.object(batch, 'STATE_DIR', str(self.root)), \
                patch.dict(os.environ, {}, clear=True):
            return batch.run()

    def test_preview_and_list_leave_all_state_unchanged(self):
        for flag in ['--dry-run', '--list']:
            with self.subTest(flag=flag):
                args = self.batch_args(['--', flag])
                self.assertEqual(self.run_batch(args, 0), 0)
                self.assertEqual(Path(args.artists).read_text(), 'Artist\n')
                self.assertFalse((self.root / 'done.txt').exists())
                self.assertFalse((self.root / 'status.json').exists())

    def test_failed_batch_has_nonzero_status_and_preserves_queue(self):
        args = self.batch_args()
        self.assertEqual(self.run_batch(args, 1), 1)
        self.assertEqual(Path(args.artists).read_text(), 'Artist\n')
        self.assertFalse((self.root / 'done.txt').exists())

    def test_done_write_failure_preserves_queue(self):
        args = self.batch_args()
        with patch.object(batch, 'append_done', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_batch(args, 0)
        self.assertEqual(Path(args.artists).read_text(), 'Artist\n')

    def test_completion_is_recoverable_and_deduplicated(self):
        args = self.batch_args()
        with patch.object(batch, 'remove_artist_line', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.run_batch(args, 0)
        self.assertEqual((self.root / 'done.txt').read_text(), 'Artist\n')
        self.assertEqual(Path(args.artists).read_text(), 'Artist\n')
        self.assertEqual(self.run_batch(args, 0), 0)
        self.assertEqual((self.root / 'done.txt').read_text(), 'Artist\n')
        self.assertEqual(Path(args.artists).read_text(), '')

    def run_sync(self, *, age=1000, fail=False):
        source = self.root / 'source'
        target = self.root / 'target'
        (source / 'Artist').mkdir(parents=True)
        target.mkdir()
        (source / 'Artist' / 'new.mp3').write_bytes(b'new')
        args = SimpleNamespace(source=str(source), copy=True,
                               dry_run=False, stable_for=300)
        with patch.object(sync, 'parse_args', return_value=args), \
                patch.object(sync, 'setup_logging', return_value=(Mock(), None)), \
                patch.object(sync, 'load_synced', return_value={'Artist'}), \
                patch.object(sync, 'save_synced'), \
                patch.object(sync, 'newest_mtime', return_value=sync.time.time() - age), \
                patch.object(sync, 'merge_move', side_effect=OSError('disk full') if fail else None) as move, \
                patch.dict(os.environ, {'DEST_PATH': str(target), 'DEST_SUBPATH': ''}):
            result = sync.run()
        return result, move

    def test_sync_revisits_previously_synced_artist(self):
        result, move = self.run_sync()
        self.assertEqual(result, 0)
        move.assert_called_once()

    def test_sync_always_checks_stability(self):
        result, move = self.run_sync(age=0)
        self.assertEqual(result, 0)
        move.assert_not_called()

    def test_sync_failure_returns_nonzero(self):
        result, move = self.run_sync(fail=True)
        self.assertEqual(result, 1)
        move.assert_called_once()

    def run_retry(self, *, verified=False, unavailable=False, interrupted=False):
        album = self.root / 'Artist' / 'Album'
        album.mkdir(parents=True)
        errors = album / 'errors.txt'
        errors.write_text('123 | Missing track\n')
        args = SimpleNamespace(source=str(self.root), deemix='unused',
                               dry_run=False, bitrate='128')
        with patch.object(retry, 'parse_args', return_value=args), \
                patch.object(retry.shutil, 'which', return_value='unused'), \
                patch.object(retry, 'track_isrc', side_effect=ValueError('no metadata') if unavailable else None, return_value='EXPECTED'), \
                patch.object(retry, 'album_has_isrc', side_effect=[False, verified]), \
                patch.object(retry, 'retry_track', side_effect=KeyboardInterrupt if interrupted else None, return_value=(0, 'Track unavailable')):
            if interrupted:
                with self.assertRaises(KeyboardInterrupt):
                    retry.run()
                result = 1
            else:
                result = retry.run()
        return result, errors

    def test_retry_exit_zero_without_audio_preserves_error(self):
        result, errors = self.run_retry()
        self.assertEqual(result, 1)
        self.assertIn('123 | Missing track', errors.read_text())

    def test_retry_verified_audio_removes_error(self):
        result, errors = self.run_retry(verified=True)
        self.assertEqual(result, 0)
        self.assertFalse(errors.exists())

    def test_retry_metadata_failure_preserves_error(self):
        result, errors = self.run_retry(unavailable=True)
        self.assertEqual(result, 1)
        self.assertTrue(errors.exists())

    def test_interrupted_retry_preserves_error(self):
        _result, errors = self.run_retry(interrupted=True)
        self.assertIn('123 | Missing track', errors.read_text())

    def test_album_tag_verification_rejects_compilation(self):
        local = {'ISRC': 'fixture.mp3'}
        with patch.object(missing.mutagen, 'File', return_value={'album': ["She's So Unusual"]}):
            self.assertTrue(missing.album_title_matches(local, "She's So Unusual"))
            self.assertFalse(missing.album_title_matches(local, 'The Essential Cyndi Lauper'))

    def test_album_isrc_verification_reads_files(self):
        album = self.root / 'Album'
        album.mkdir()
        (album / 'track.mp3').write_bytes(b'fixture')
        with patch('mutagen.File', return_value={'isrc': ['EXPECTED']}):
            self.assertTrue(safety.album_has_isrc(album, 'EXPECTED'))
            self.assertFalse(safety.album_has_isrc(album, 'OTHER'))

    def test_real_sync_cli_copies_new_files_for_recorded_artist(self):
        source = self.root / 'music'
        destination = self.root / 'destination'
        album = source / 'Artist' / 'Album'
        album.mkdir(parents=True)
        destination.mkdir()
        (album / 'first.mp3').write_bytes(b'first')
        environment = {**os.environ, 'DEST_PATH': str(destination),
                       'DEST_SUBPATH': ''}
        command = [sys.executable, str(ROOT / 'sync_to_dest.py'),
                   '-s', str(source), '--copy', '--stable-for', '0']
        first = subprocess.run(command, cwd=self.root, env=environment,
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(first.returncode, 0, first.stderr)
        (album / 'second.mp3').write_bytes(b'second')
        second = subprocess.run(command, cwd=self.root, env=environment,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((destination / 'Artist' / 'Album' / 'first.mp3').read_bytes(), b'first')
        self.assertEqual((destination / 'Artist' / 'Album' / 'second.mp3').read_bytes(), b'second')
        self.assertTrue((album / 'first.mp3').exists())
        self.assertEqual(json.loads((self.root / 'state' / 'synced.json').read_text()), ['Artist'])

    def test_installer_renders_special_paths_without_installing(self):
        script = (ROOT / 'install.sh').read_text()
        renderer = script.split("<<'PYUNIT'\n", 1)[1].split('\nPYUNIT', 1)[0]
        project = self.root / 'Project & "Music" % $cash'
        (project / 'systemd').mkdir(parents=True)
        (project / 'systemd' / 'deemix-batch.service').write_text(
            (ROOT / 'systemd' / 'deemix-batch.service').read_text())
        staging = self.root / 'Music & Library | 100% ${HOME}'
        unit = self.root / 'test.service'
        subprocess.run([sys.executable, '-', str(project), str(staging), str(unit)],
                       input=renderer, text=True, capture_output=True, check=True)
        text = unit.read_text()
        self.assertNotIn('%h/deemix-downloads', text)
        self.assertIn('100%% $${HOME}', text)
        self.assertIn('Project & \\"Music\\" %% $$cash', text)


if __name__ == '__main__':
    unittest.main()
