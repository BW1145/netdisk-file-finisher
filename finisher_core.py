import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import threading
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path

CHUNK = 8 * 1024 * 1024
SUFFIXES = ('.baiduyun.p.downloading', '.baiduyun.downloading', '.downloading')
HIDDEN_PROCESS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class Stopped(Exception):
    pass


def check_stop(cancel):
    if cancel is not None and cancel.is_set():
        raise Stopped('已停止')


@contextmanager
def shared_reader(path):
    if os.name != 'nt':
        with Path(path).open('rb') as stream:
            yield stream
        return
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    # Sharing delete as well as read/write keeps client finalization possible.
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        yield stream


def normalized_md5(value):
    value = (value or '').strip().lower()
    if re.fullmatch(r'[0-9a-f]{32}', value):
        return value
    if re.fullmatch(r'[0-9a-f]{9}[g-v][0-9a-f]{22}', value):
        decoded = ''.join(format((ord(c) - 103 if i == 9 else int(c, 16)) ^ (i & 15), 'x')
                          for i, c in enumerate(value))
        return decoded[8:16] + decoded[:8] + decoded[24:32] + decoded[16:24]
    return None


def task_records(user_root=None):
    root = Path(user_root) if user_root else (
        Path(os.environ.get('APPDATA', '')) / 'baidu/BaiduNetdisk/module/BrowserEngine/users')
    records, warnings = {}, []
    for db in root.glob('*/transmission.db'):
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
                con.execute('PRAGMA query_only=ON')
                con.row_factory = sqlite3.Row
                for row in con.execute('SELECT * FROM download_file'):
                    data = dict(row)
                    final = Path(data['local_path'])
                    for suffix in SUFFIXES:
                        records[os.path.normcase(str(final.resolve()) + suffix)] = data
        except (sqlite3.Error, OSError) as error:
            warnings.append(f'有一份任务记录无法读取：{error}')
    return records, warnings


def source_name(path, record=None):
    if record and record.get('local_path'):
        return Path(record['local_path']).name
    for suffix in SUFFIXES:
        if path.name.lower().endswith(suffix):
            return path.name[:-len(suffix)]
    return path.name


def discover(folder, records):
    return [(path, records.get(os.path.normcase(str(path.resolve())), {}))
            for path in sorted(Path(folder).rglob('*.downloading')) if path.is_file()]


def inspect(stream):
    stream.seek(0)
    head = stream.read(1024)
    size = os.fstat(stream.fileno()).st_size
    end = zipfile._EndRecData(stream)
    if end is not None:
        with zipfile.ZipFile(stream) as archive:
            info = archive.infolist()
            prefix = archive.start_dir - end[zipfile._ECD_OFFSET]
            if prefix < 0:
                raise ValueError('ZIP 的文件位置记录不正确。')
            for item in info:
                stream.seek(item.header_offset)
                local = stream.read(30)
                if len(local) != 30 or local[:4] != b'PK\x03\x04':
                    raise ValueError('ZIP 有文件头尚未下载完整。')
                fields = struct.unpack('<4s5H3I2H', local)
                data_end = item.header_offset + 30 + fields[-2] + fields[-1] + item.compress_size
                if data_end > archive.start_dir:
                    raise ValueError('ZIP 的文件数据范围不完整。')
            return {'kind': 'ZIP', 'ext': '.zip', 'prefix': prefix,
                    'encrypted': any(item.flag_bits & 1 for item in info),
                    'files': sum(not item.is_dir() for item in info)}
    signatures = [(b'Rar!\x1a\x07', 'RAR', '.rar'), (b'7z\xbc\xaf\x27\x1c', '7Z', '.7z'),
                  (b'\x1f\x8b', 'GZIP', '.gz'), (b'BZh', 'BZIP2', '.bz2'),
                  (b'\xfd7zXZ\0', 'XZ', '.xz')]
    for magic, kind, ext in signatures:
        if head.startswith(magic):
            return {'kind': kind, 'ext': ext, 'prefix': 0, 'encrypted': False}
    if head[257:263] in (b'ustar\0', b'ustar '):
        return {'kind': 'TAR', 'ext': '.tar', 'prefix': 0, 'encrypted': False}
    if head.startswith(b'\x1aE\xdf\xa3'):
        return {'kind': '视频', 'ext': '.webm' if b'webm' in head else '.mkv', 'prefix': 0}
    if head[4:8] == b'ftyp':
        return {'kind': '视频', 'ext': '.mov' if head[8:12] == b'qt  ' else '.mp4', 'prefix': 0}
    return {'kind': '普通文件', 'ext': None, 'prefix': 0}


def hash_stream(stream, length, algorithm, cancel, notify, label):
    expected_end = stream.tell() + length
    digest = hashlib.new(algorithm)
    done = 0
    while done < length:
        check_stop(cancel)
        block = stream.read(min(CHUNK, length - done))
        if not block:
            raise ValueError('读取时文件长度发生变化，请稍后重试。')
        digest.update(block)
        done += len(block)
        notify(label, int(done * 100 / length))
    if stream.read(1) or os.fstat(stream.fileno()).st_size != expected_end:
        raise ValueError('读取时文件长度发生变化，请稍后重试。')
    return digest.hexdigest()


def copy_snapshot(source, prefix, partial, cancel, notify, expected_md5=None):
    with shared_reader(source) as stream:
        total = os.fstat(stream.fileno()).st_size - prefix
        if total < 0:
            raise ValueError('源文件长度不足。')
        if shutil.disk_usage(partial.parent).free < total:
            raise OSError('保存位置空间不足。')
        md5 = hashlib.md5() if expected_md5 else None
        if md5 is not None:
            remaining = prefix
            while remaining:
                check_stop(cancel)
                block = stream.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError('源文件长度不足。')
                md5.update(block)
                remaining -= len(block)
        stream.seek(prefix)
        digest = hashlib.sha256()
        done = 0
        with partial.open('wb') as output:
            while done < total:
                check_stop(cancel)
                block = stream.read(min(CHUNK, total - done))
                if not block:
                    raise ValueError('源文件还在变化，请稍后重试。')
                output.write(block)
                digest.update(block)
                if md5 is not None:
                    md5.update(block)
                done += len(block)
                notify('复制文件', int(done * 100 / total))
            output.flush()
            os.fsync(output.fileno())
        if md5 is not None and md5.hexdigest() != expected_md5:
            raise ValueError('完整文件校验值不一致。可使用“导出待核查副本”。')
        with partial.open('rb') as copied:
            current = hash_stream(copied, total, 'sha256', cancel, notify, '核对导出副本')
        if current != digest.hexdigest():
            raise ValueError('副本数据校验未通过。')
        if md5 is not None and prefix:
            stream.seek(0)
            current = hash_stream(stream, total + prefix, 'md5', cancel, notify, '核对网盘校验值')
            if current != expected_md5:
                raise ValueError('源内容在复制期间发生了变化，请稍后重试。')
        stream.seek(prefix)
        current = hash_stream(stream, total, 'sha256', cancel, notify, '核对源文件')
        if current != digest.hexdigest():
            raise ValueError('源内容在复制期间发生了变化，请稍后重试。')
    return digest.hexdigest()


def find_ffmpeg():
    import sys
    portable = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).parent / 'ffmpeg.exe'
    if portable.is_file():
        return str(portable)
    found = shutil.which('ffmpeg')
    if found:
        return found
    candidate = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft/WinGet/Links/ffmpeg.exe'
    return str(candidate) if candidate.is_file() else None


def validate_media(path, cancel, notify):
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise ValueError('没有找到 FFmpeg，无法检查视频内容。可使用“导出待核查副本”。')
    with tempfile.TemporaryFile(mode='w+b') as errors:
        proc = subprocess.Popen([ffmpeg, '-nostdin', '-hide_banner', '-v', 'error', '-xerror',
                                 '-i', str(path), '-map', '0:v?', '-map', '0:a?', '-sn', '-dn',
                                 '-progress', 'pipe:1', '-nostats', '-f', 'null', os.devnull],
                                stdout=subprocess.PIPE, stderr=errors, text=True,
                                encoding='utf-8', errors='replace', creationflags=HIDDEN_PROCESS)
        lines = queue.Queue()

        def read_progress():
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        try:
            while True:
                check_stop(cancel)
                try:
                    line = lines.get(timeout=0.25)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if line.startswith('out_time_us='):
                    value = line.partition('=')[2].strip()
                    if value.lstrip('-').isdigit():
                        seconds = max(0, int(value) // 1000000)
                        notify(f'检查音视频：已读取 {seconds // 60} 分 {seconds % 60} 秒', None)
            while proc.poll() is None:
                check_stop(cancel)
                try:
                    proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
            check_stop(cancel)
            code = proc.returncode
            errors.seek(0)
            detail = errors.read(3000).decode('utf-8', errors='replace').strip()
            if code or detail:
                raise ValueError('音视频内容检查未通过。可导出待核查副本自行检查。')
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            reader.join()
            proc.stdout.close()


def validate_zip(path, cancel, notify):
    encrypted = False
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        total = sum(item.file_size for item in entries)
        done = 0
        for item in entries:
            check_stop(cancel)
            if item.is_dir():
                continue
            if item.flag_bits & 1:
                encrypted = True
                continue
            with archive.open(item) as content:
                while block := content.read(CHUNK):
                    check_stop(cancel)
                    done += len(block)
                    notify('检查压缩包内容', int(done * 100 / total) if total else 100)
    return '目录可读；加密内容未验证' if encrypted else '压缩包全部文件 CRC 校验通过'


def validate_other_archive(path, cancel, notify):
    check_stop(cancel)
    notify('检查压缩包内容', None)
    with tempfile.TemporaryFile(mode='w+b') as errors:
        proc = subprocess.Popen(['tar', '-xOf', str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=errors, creationflags=HIDDEN_PROCESS)
        try:
            while proc.poll() is None:
                check_stop(cancel)
                try:
                    proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
            errors.seek(0)
            detail = errors.read(6000).decode('utf-8', errors='replace')
            if proc.returncode:
                if any(word in detail.lower() for word in ('passphrase', 'password', 'encrypted')):
                    return '压缩包副本；加密内容未验证'
                raise ValueError('压缩包读取检查未通过。可导出待核查副本自行检查。')
            return '压缩包全部内容读取检查通过'
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)


def write_history(path, records):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         prefix='.' + path.name + '-', suffix='.tmp',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(records, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_history(path):
    fields = {'identity': dict, 'name': str, 'output': str, 'detail': str,
              'sha256': str, 'export_size': int, 'export_mtime': int}
    return [item for item in read_json(path, [])
            if isinstance(item, dict)
            and all(isinstance(item.get(key), kind) for key, kind in fields.items())
            and item['output'] and item['export_size'] >= 0
            and re.fullmatch(r'[0-9a-f]{64}', item['sha256'])]


def verified_export(old, sha, cancel, notify):
    target = Path(old['output'])
    try:
        stat = target.stat()
        if not target.is_file() or stat.st_size != old['export_size'] or stat.st_mtime_ns != old['export_mtime']:
            return False
        with shared_reader(target) as stream:
            return hash_stream(stream, stat.st_size, 'sha256', cancel, notify, '核对已有副本') == sha
    except (OSError, ValueError):
        return False


def process(source, record, output_folder, cancel=None, notify=lambda *_: None,
            encrypted_ok=True, unchecked=False):
    source, output_folder = Path(source), Path(output_folder)
    name = source_name(source, record)
    result = {'name': name, 'source': str(source), 'status': '需要检查', 'output': '', 'detail': ''}
    partial = None
    try:
        check_stop(cancel)
        source_stat = source.stat()
        with shared_reader(source) as stream:
            try:
                layout = inspect(stream)
            except (ValueError, zipfile.BadZipFile):
                if not unchecked:
                    raise
                layout = {'kind': '普通文件', 'ext': None, 'prefix': 0}
        result['kind'] = layout['kind']
        if record.get('status') == 2 and not unchecked:
            result.update(status='等待下载停止', detail='网盘仍在下载该文件。停止或报错后再运行。')
            return result
        if (record.get('file_size') and record.get('complete_size') is not None
                and record['complete_size'] < record['file_size']
                and record.get('error_code') != 1000022 and not unchecked):
            result.update(status='等待下载完成', detail='网盘记录显示尚未下载完整；收尾报错任务可另行检查。')
            return result
        if record.get('file_size') and source_stat.st_size != record['file_size'] and not unchecked:
            raise ValueError('实际长度与网盘记录不同，不能自动认定已经下载完整。')
        if layout['kind'] == 'ZIP' and layout['encrypted'] and not encrypted_ok and not unchecked:
            result.update(status='需要检查', detail='压缩包有加密，需开启“导出加密压缩包副本”。')
            return result
        identity = {'source': str(source.resolve()), 'size': source_stat.st_size,
                    'mtime': source_stat.st_mtime_ns, 'unchecked': unchecked,
                    'md5': record.get('md5', ''), 'prefix': layout['prefix']}
        expected = normalized_md5(record.get('md5'))
        if not unchecked and (layout['kind'] == '普通文件' or (layout['kind'] == 'ZIP' and expected)):
            if not expected:
                raise ValueError('没有可用校验值。可使用“导出待核查副本”。')
            with shared_reader(source) as stream:
                matched = hash_stream(stream, source_stat.st_size, 'md5', cancel, notify,
                                      '核对网盘校验值') == expected
            if not matched:
                raise ValueError('完整文件校验值不一致。可使用“导出待核查副本”。')
        records_file = output_folder / '处理记录.json'
        previous = read_history(records_file)
        for old in previous:
            if old['identity'] == identity and old['export_size'] == source_stat.st_size - layout['prefix']:
                with shared_reader(source) as stream:
                    stream.seek(layout['prefix'])
                    current = hash_stream(stream, old['export_size'], 'sha256', cancel, notify, '核对源文件')
                if current == old['sha256'] and verified_export(old, current, cancel, notify):
                    result.update(status='已处理过', output=old['output'], detail=old['detail'])
                    return result
        output_folder.mkdir(parents=True, exist_ok=True)
        target_name = str(Path(name).with_suffix(layout['ext'])) if layout['ext'] else name
        target_folder = output_folder / ('待核查副本' if unchecked else '导出文件')
        target_folder.mkdir(parents=True, exist_ok=True)
        target = target_folder / target_name
        count = 2
        while target.exists():
            target = target_folder / f'{Path(target_name).stem} ({count}){Path(target_name).suffix}'
            count += 1
        with tempfile.NamedTemporaryFile(prefix='.' + target.stem + '-', suffix='.处理中',
                                         dir=target_folder, delete=False) as temporary:
            partial = Path(temporary.name)
        sha = copy_snapshot(source, layout['prefix'], partial, cancel, notify,
                            expected_md5=expected if layout['kind'] == 'ZIP' and not unchecked else None)
        for old in previous:
            if old.get('sha256') == sha and old.get('name') == name and old.get('identity', {}).get('unchecked') == unchecked:
                if verified_export(old, sha, cancel, notify):
                    partial.unlink()
                    partial = None
                    result.update(status='已处理过', output=old['output'], detail=old['detail'])
                    return result
        if unchecked:
            detail = '副本与源数据一致；内容完整性未验证'
        elif layout['kind'] == 'ZIP':
            with partial.open('rb') as exported:
                checked = inspect(exported)
            if checked['kind'] != 'ZIP' or checked['prefix'] != 0:
                raise ValueError('ZIP 目录未能正确导出。')
            detail = validate_zip(partial, cancel, notify)
        else:
            matched = False
            if expected:
                with partial.open('rb') as stream:
                    matched = hash_stream(stream, partial.stat().st_size, 'md5', cancel, notify,
                                          '核对网盘校验值') == expected
            if matched:
                detail = '完整文件校验值与网盘记录一致'
            elif layout['kind'] == '视频':
                validate_media(partial, cancel, notify)
                detail = '音视频从头到尾解码通过'
            elif layout['kind'] in ('RAR', '7Z', 'GZIP', 'BZIP2', 'XZ', 'TAR'):
                detail = validate_other_archive(partial, cancel, notify)
            else:
                raise ValueError('缺少可用校验依据或校验值不一致。可使用“导出待核查副本”。')
        check_stop(cancel)
        partial.rename(target)
        partial = None
        result.update(status='已导出', output=str(target), detail=detail, sha256=sha,
                      identity=identity, export_size=target.stat().st_size,
                      export_mtime=target.stat().st_mtime_ns)
        previous.append(result)
        try:
            write_history(records_file, previous)
        except OSError as error:
            result['detail'] += f'；处理记录保存失败：{error}。副本已导出，下次处理可能再次导出。'
        return result
    except Stopped:
        result.update(status='已停止', detail='操作已停止，原文件保留。')
        return result
    except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as error:
        result.update(status='需要检查', detail=str(error))
        return result
    finally:
        if partial is not None and partial.exists():
            partial.unlink()


def read_json(path, default):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default
