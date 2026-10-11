import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

import finisher_core as core


class FinisherTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).parent.resolve()
        self.temp = tempfile.TemporaryDirectory(dir=self.base)
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to(self.base)
        self.out = self.root / '导出 结果'

    def tearDown(self):
        assert self.root.is_relative_to(self.base)
        self.temp.cleanup()

    def make_zip(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('目录/one.mp4', b'video-placeholder')
        return stream.getvalue()

    def run_source(self, data, name='测试.dat.downloading', record=None, **options):
        path = self.root / name
        path.write_bytes(data)
        return path, core.process(path, record or {}, self.out, **options)

    def test_video_prefixed_zip_overrides_extension_and_wrong_hash(self):
        zip_data = self.make_zip()
        cover = b'\x1aE\xdf\xa3-matroska-cover'
        path, result = self.run_source(cover + zip_data, '压缩包.mkv.downloading',
                                       {'md5': 'f' * 32, 'status': 5, 'error_code': 1000022})
        self.assertEqual(result['status'], '已导出')
        self.assertEqual(Path(result['output']).suffix, '.zip')
        self.assertEqual(Path(result['output']).read_bytes(), zip_data)
        self.assertEqual(path.read_bytes(), cover + zip_data)
        with zipfile.ZipFile(result['output']) as archive:
            self.assertIsNone(archive.testzip())

    def test_zip_crc_error_is_not_exported_as_verified(self):
        damaged = bytearray(self.make_zip())
        position = damaged.index(b'video-placeholder')
        damaged[position] ^= 1
        _, result = self.run_source(damaged)
        self.assertEqual(result['status'], '需要检查')
        self.assertFalse(list(self.out.rglob('*.zip')))

    def test_missing_checksum_has_manual_copy_path(self):
        source, result = self.run_source(b'unknown data')
        self.assertEqual(result['status'], '需要检查')
        copied = core.process(source, {}, self.out, unchecked=True)
        self.assertEqual(copied['status'], '已导出')
        self.assertIn('未验证', copied['detail'])
        self.assertEqual(Path(copied['output']).read_bytes(), b'unknown data')

    def test_full_checksum_matches_and_existing_result_is_reused(self):
        data = b'checksum verified data'
        source, first = self.run_source(data, record={'md5': hashlib.md5(data).hexdigest()})
        second = core.process(source, {'md5': hashlib.md5(data).hexdigest()}, self.out)
        self.assertEqual(first['status'], '已导出')
        self.assertEqual(second['status'], '已处理过')
        self.assertEqual(first['output'], second['output'])

    def test_matching_size_does_not_bypass_hash(self):
        _, result = self.run_source(b'bad', record={'file_size': 3, 'md5': hashlib.md5(b'abc').hexdigest()})
        self.assertEqual(result['status'], '需要检查')
        self.assertEqual(result['output'], '')

    def test_incomplete_stopped_task_waits(self):
        _, result = self.run_source(b'abc', record={'file_size': 3, 'complete_size': 1, 'status': 5})
        self.assertEqual(result['status'], '等待下载完成')

    def test_incomplete_zip_is_rejected(self):
        _, result = self.run_source(self.make_zip()[:-30])
        self.assertEqual(result['status'], '需要检查')
        self.assertFalse(result['output'])

    def test_corrupt_runtime_record_is_recreated(self):
        self.out.mkdir()
        (self.out / '处理记录.json').write_text('{', encoding='utf-8')
        _, result = self.run_source(self.make_zip())
        self.assertEqual(result['status'], '已导出')

    def test_disabled_encrypted_export_requires_choice(self):
        data = bytearray(self.make_zip())
        # Flags in both local and central headers mark an encrypted entry.
        data[6] |= 1
        central = data.index(b'PK\x01\x02')
        data[central + 8] |= 1
        source, result = self.run_source(data, encrypted_ok=False)
        self.assertEqual(result['status'], '需要检查')
        exported = core.process(source, {}, self.out)
        self.assertEqual(exported['status'], '已导出')
        self.assertIn('加密内容未验证', exported['detail'])

    def test_changing_source_during_copy_is_rejected(self):
        data = b'a' * (core.CHUNK + 32)
        source = self.root / 'changing.downloading'
        source.write_bytes(data)
        changed = []
        def update(label, percent):
            if label == '复制文件' and not changed:
                with source.open('r+b') as writer:
                    writer.write(b'b')
                changed.append(True)
        result = core.process(source, {'md5': hashlib.md5(data).hexdigest()}, self.out, notify=update)
        self.assertEqual(result['status'], '需要检查')
        self.assertIn('变化', result['detail'])
        self.assertFalse(list(self.out.rglob('*.处理中')))
        self.assertFalse(list(self.out.rglob('changing')))

    def test_reader_allows_client_rename_and_write(self):
        source = self.root / 'original'
        source.write_bytes(b'abc')
        renamed = self.root / 'renamed'
        with core.shared_reader(source) as reader:
            source.rename(renamed)
            with renamed.open('r+b') as writer:
                writer.write(b'xyz')
            reader.seek(0)
            self.assertEqual(reader.read(), b'xyz')

    def test_preallocated_active_task_waits(self):
        _, result = self.run_source(b'\0' * 100, record={'status': 2, 'file_size': 100, 'complete_size': 1})
        self.assertEqual(result['status'], '等待下载停止')
        self.assertFalse(self.out.exists())

    def test_cancel_preserves_source(self):
        cancel = threading.Event()
        cancel.set()
        source, result = self.run_source(b'data', cancel=cancel)
        self.assertEqual(result['status'], '已停止')
        self.assertEqual(source.read_bytes(), b'data')

    def test_records_are_read_only_and_orphan_files_are_found(self):
        users = self.root / 'users'
        user = users / 'account'
        user.mkdir(parents=True)
        db = user / 'transmission.db'
        source = self.root / 'file.bin.baiduyun.p.downloading'
        source.write_bytes(b'data')
        final = str(self.root / 'file.bin')
        with sqlite3.connect(db) as con:
            con.execute('CREATE TABLE download_file(local_path TEXT,file_size INTEGER,complete_size INTEGER,status INTEGER,error_code INTEGER,md5 TEXT)')
            con.execute('INSERT INTO download_file VALUES (?,?,?,?,?,?)', (final, 4, 4, 5, 1000022, ''))
        con.close()
        before = db.read_bytes()
        records, warnings = core.task_records(users)
        found = core.discover(self.root, records)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1]['error_code'], 1000022)
        self.assertFalse(warnings)
        self.assertEqual(db.read_bytes(), before)
        self.assertEqual(len(core.discover(self.root, {})), 1)

    def make_video(self):
        source = self.root / 'video.mp4'
        result = subprocess.run([core.find_ffmpeg(), '-nostdin', '-v', 'error', '-f', 'lavfi',
                                 '-i', 'color=c=blue:s=160x90:r=12', '-t', '1', '-c:v', 'libx264',
                                 '-pix_fmt', 'yuv420p', str(source)], capture_output=True,
                                creationflags=core.HIDDEN_PROCESS)
        self.assertEqual(result.returncode, 0)
        return source.read_bytes()

    @unittest.skipUnless(core.find_ffmpeg(), 'FFmpeg optional dependency is unavailable')
    def test_missing_video_checksum_uses_full_decode(self):
        data = self.make_video()
        source, result = self.run_source(data, '视频.zip.downloading')
        self.assertEqual(result['status'], '已导出')
        self.assertEqual(Path(result['output']).suffix, '.mp4')
        self.assertIn('解码通过', result['detail'])
        self.assertEqual(Path(result['output']).read_bytes(), data)

    @unittest.skipUnless(core.find_ffmpeg(), 'FFmpeg optional dependency is unavailable')
    def test_video_failure_keeps_original_and_no_success_output(self):
        data = self.make_video()
        source, result = self.run_source(data[:-300], '截断.mp4.downloading')
        self.assertEqual(result['status'], '需要检查')
        self.assertEqual(source.read_bytes(), data[:-300])
        self.assertFalse(list(self.out.rglob('截断.mp4')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
