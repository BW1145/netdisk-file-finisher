import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import handle_file
from archive_flow import Journal, STAGE_PREFIX, engine_path
from folder_watch import FolderWatch


def make_zip(path, files):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / '来源' / '内容' / '封面.png'
        self.source.parent.mkdir(parents=True)
        self.journal = Journal(self.root / 'state')
        self.cancel = threading.Event()

    def handle(self, **options):
        return handle_file(self.source, self.journal, self.cancel, lambda _: None, **options)

    def check_stages(self):
        self.assertEqual(list(self.source.parent.glob(STAGE_PREFIX + '*')), [])

    def test_prefixed_zip_to_single_file_without_overwrite(self):
        outer = self.root / 'outer.zip'
        make_zip(outer, {'实际.txt': '结果'})
        self.source.write_bytes(b'fake media' + outer.read_bytes())
        occupied = self.source.parent / '实际.txt'
        occupied.write_text('原有', encoding='utf8')
        result = self.handle()
        output = Path(result['output'])
        self.assertEqual(output.name, '实际 (2).txt')
        self.assertEqual(output.read_text(encoding='utf8'), '结果')
        self.assertEqual(occupied.read_text(encoding='utf8'), '原有')
        self.assertEqual(self.handle()['output'], str(output))
        self.check_stages()

    @unittest.skipUnless(engine_path().is_file(), 'run prepare-7zip.ps1')
    def test_nested_7z_uses_parent_password_and_keeps_usable_apk(self):
        apk = self.root / '成品.apk'
        make_zip(apk, {'assets/dat': b'data'})
        inner = self.root / 'encrypted.7z'
        subprocess.run([str(engine_path()), 'a', '-t7z', '-mhe=on', '-p来源', str(inner), str(apk)],
                       check=True, capture_output=True)
        make_zip(self.source, {'伪装.tmp': inner.read_bytes()})
        result = self.handle()
        self.assertEqual(result['layers'], ['ZIP', '7Z'])
        self.assertEqual(Path(result['output']).read_bytes(), apk.read_bytes())
        self.check_stages()

    @unittest.skipUnless(engine_path().is_file(), 'run prepare-7zip.ps1')
    def test_prefixed_aes_zip_with_manual_password(self):
        payload = self.root / 'reply.txt'
        payload.write_bytes(b'content')
        archived = self.root / 'aes.zip'
        subprocess.run([str(engine_path()), 'a', '-tzip', '-mem=AES256', '-pmanual', str(archived), str(payload)],
                       check=True, capture_output=True)
        self.source.write_bytes(b'fake media' + archived.read_bytes())
        self.assertTrue(self.handle()['password_required'])
        self.assertTrue(self.source.is_file())
        self.check_stages()
        self.assertEqual(Path(self.handle(password='manual')['output']).read_bytes(), b'content')
        self.check_stages()

    def test_cancel_disk_full_and_corrupt_zip_never_recycle(self):
        make_zip(self.source, {'data.txt': b'payload' * 1000})
        with patch('app.recycle') as recycle:
            self.cancel.set()
            self.assertTrue(self.handle(recycle_original=True)['canceled'])
            self.cancel.clear()
            with patch('archive_flow.shutil.disk_usage', return_value=shutil._ntuple_diskusage(0, 0, 0)):
                self.assertIn('磁盘空间不足', self.handle(recycle_original=True)['detail'])
            self.source.write_bytes(self.source.read_bytes()[:-22])
            self.assertFalse(self.handle(recycle_original=True).get('output'))
            recycle.assert_not_called()
        self.assertTrue(self.source.is_file())
        self.check_stages()

    def test_recycle_only_after_output_exists(self):
        make_zip(self.source, {'data.txt': b'payload'})
        with patch('app.recycle', side_effect=lambda source: self.assertTrue(
                (source.parent / 'data.txt').is_file())) as recycle:
            self.assertTrue(self.handle(recycle_original=True)['output'])
            recycle.assert_called_once_with(self.source)
        self.check_stages()

    def test_recover_cleans_only_registered_stage(self):
        stage = self.root / (STAGE_PREFIX + '0' * 32)
        unrelated = self.root / (STAGE_PREFIX + '1' * 32)
        stage.mkdir()
        unrelated.mkdir()
        with self.journal.connect() as db:
            db.execute('INSERT INTO stages VALUES (?)', (str(stage),))
        self.journal.recover()
        self.assertFalse(stage.exists())
        self.assertTrue(unrelated.exists())


@unittest.skipUnless(os.name == 'nt', 'native Windows watcher')
class WatchTests(unittest.TestCase):
    def test_native_baseline_new_file_and_catchup(self):
        with tempfile.TemporaryDirectory() as tmp, patch('folder_watch.STABILITY_DELAY', .25):
            root = Path(tmp)
            watched = root / 'watch'
            watched.mkdir()
            for i in range(30):
                make_zip(watched / f'old{i}.zip', {'old.txt': b'old'})
            journal = Journal(root / 'state')
            events = queue.Queue()

            def start():
                stop = threading.Event()
                watcher = FolderWatch(watched, root / 'watch.json', journal, stop,
                                      lambda *event: events.put(event), 0)
                worker = threading.Thread(target=watcher.run, daemon=True)
                worker.start()
                self.assertEqual(events.get(timeout=6), ('watch', 0, '正在监听'))
                return stop, worker, watcher

            stop, thread, watcher = start()
            try:
                temporary = watched / 'fresh.png.downloading'
                temporary.write_bytes(b'not yet complete')
                time.sleep(.35)
                self.assertTrue(events.empty())
                make_zip(temporary, {'new.txt': b'new'})
                downloaded = temporary.with_suffix('')
                temporary.rename(downloaded)
                event = events.get(timeout=6)
                self.assertEqual(event[:3], ('auto', 0, downloaded))
                result = handle_file(downloaded, journal, None, lambda _: None, automatic=True,
                                     expected=event[3])
                self.assertTrue(result.get('output'))
                watcher.completed.put((downloaded, event[3], False))
                time.sleep(.55)
                self.assertTrue(events.empty())
            finally:
                stop.set()
                thread.join(5)

            offline = watched / 'offline.dat'
            make_zip(offline, {'offline.txt': b'data'})
            stop, thread, _ = start()
            try:
                self.assertEqual(events.get(timeout=6)[:3], ('auto', 0, offline))
                time.sleep(.4)
                self.assertTrue(events.empty())
            finally:
                stop.set()
                thread.join(5)


if __name__ == '__main__':
    unittest.main()
