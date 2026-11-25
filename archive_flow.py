import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path, PureWindowsPath

import finisher_core as core

ARCHIVES = {'ZIP', 'RAR', '7Z'}
ZIP_DOCUMENTS = {'.apk', '.jar', '.docx', '.xlsx', '.pptx', '.odt', '.ods', '.odp', '.epub'}
STAGE_PREFIX = '.netdisk-finisher-'
ZIP_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}


class PasswordRequired(ValueError):
    pass


class SourceChanged(ValueError):
    pass


def fingerprint(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def signature(path):
    return fingerprint(path.stat())


def path_key(path):
    return os.path.normcase(os.path.abspath(path))


def linked(info):
    return bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or stat.S_ISLNK(info.st_mode))


def unused_name(path):
    index = 2
    candidate = path
    while os.path.lexists(candidate):
        candidate = path.with_name(f'{path.stem} ({index}){path.suffix}')
        index += 1
    return candidate


class Journal:
    def __init__(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / 'jobs.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS stages (path TEXT PRIMARY KEY)')
            db.execute('''CREATE TABLE IF NOT EXISTS outputs (
                path TEXT PRIMARY KEY, source TEXT, source_id TEXT, output_id TEXT)''')

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    def cleanup_stage(self, path):
        if path.is_dir() and not linked(path.lstat()):
            shutil.rmtree(path)
        with self.connect() as db:
            db.execute('DELETE FROM stages WHERE path = ?', (str(path),))

    def recover(self):
        with self.connect() as db:
            paths = list(db.execute('SELECT path FROM stages'))
        for (value,) in paths:
            path = Path(value)
            suffix = path.name.removeprefix(STAGE_PREFIX)
            if (path.is_absolute() and path.name.startswith(STAGE_PREFIX)
                    and len(suffix) == 32 and all(c in '0123456789abcdef' for c in suffix)):
                try:
                    self.cleanup_stage(path)
                except OSError:
                    continue

    @contextmanager
    def staging(self, parent):
        path = parent / (STAGE_PREFIX + uuid.uuid4().hex)
        with self.connect() as db:
            db.execute('INSERT INTO stages VALUES (?)', (str(path),))
        try:
            path.mkdir()
            yield path
        finally:
            self.cleanup_stage(path)

    def record_output(self, source, original, staged, target):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO outputs VALUES (?, ?, ?, ?)',
                       (str(target), path_key(source), json.dumps(original),
                        json.dumps(signature(staged))))

    def known_output(self, source, original):
        with self.connect() as db:
            rows = db.execute('SELECT path, output_id FROM outputs WHERE source=? AND source_id=?',
                              (path_key(source), json.dumps(original))).fetchall()
        for path, expected in rows:
            try:
                if signature(Path(path)) == json.loads(expected):
                    return Path(path)
            except OSError:
                continue
        return None

    def output_paths(self):
        with self.connect() as db:
            rows = db.execute('SELECT path, output_id FROM outputs').fetchall()
        result = set()
        for path, expected in rows:
            try:
                info = Path(path).stat()
                original = json.loads(expected)
                if (fingerprint(info) == original
                        or (stat.S_ISDIR(info.st_mode) and fingerprint(info)[:2] == original[:2])):
                    result.add(path_key(path))
            except OSError:
                continue
        return result


def engine_path():
    folder = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    return folder / '7zip' / '7z.exe'


def payload_path(name):
    path = PureWindowsPath(name)
    raw = name.replace('\\', '/').rstrip('/').split('/')
    if (not name or path.anchor or not path.parts
            or any(not part or part in {'.', '..'} or part.endswith((' ', '.'))
                   or PureWindowsPath(part).is_reserved()
                   or any(ord(c) < 32 or c in '<>:"|?*' for c in part) for part in raw)):
        raise ValueError('压缩包中有无法安全保存的文件名。')
    return Path(*path.parts)


def copy_data(src, dst, cancel):
    while block := src.read(core.CHUNK):
        core.check_stop(cancel)
        dst.write(block)


def require_space(path, size):
    if shutil.disk_usage(path).free < size:
        raise OSError('磁盘空间不足。请释放空间后重试。')


def extract_zip(archive, output, cancel):
    items = archive.infolist()
    require_space(output, sum(item.file_size for item in items))
    seen = set()
    for item in items:
        core.check_stop(cancel)
        relative = payload_path(item.filename)
        key = path_key(output / relative)
        if key in seen or stat.S_ISLNK(item.external_attr >> 16):
            raise ValueError('压缩包包含重复路径或链接。')
        seen.add(key)
        target = output / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as src, target.open('xb') as dst:
                copy_data(src, dst, cancel)


def call_engine(action, source, password, cancel, output=None):
    engine = engine_path()
    if not engine.is_file():
        raise OSError('解包组件缺失。请重新获取完整的正式版本。')
    args = [str(engine), action, '-sccUTF-8', '-p' + password]
    if output is None:
        args.append('-slt')
    else:
        args.extend(['-y', '-bso0', '-bsp0', '-o' + str(output)])
    args.extend(['--', str(source)])
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=core.HIDDEN_PROCESS)
    try:
        while True:
            core.check_stop(cancel)
            try:
                stdout, stderr = proc.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    stdout = stdout.decode('utf-8', 'replace')
    if proc.returncode:
        text = (stdout + stderr.decode('utf-8', 'replace')).lower()
        if 'wrong password' in text or 'encrypted archive' in text:
            raise PasswordRequired('需要正确的解包密码。')
        raise ValueError('压缩包读取或校验失败。请检查下载是否完整。')
    return stdout


def extract_with_engine(source, output, passwords, cancel):
    for password in passwords:
        core.check_stop(cancel)
        try:
            listing = call_engine('l', source, password, cancel)
            header, separator, entries = listing.replace('\r\n', '\n').partition('\n----------\n')
            if not separator or any(line.startswith('Volumes = ') and line != 'Volumes = 1'
                                    for line in header.splitlines()):
                raise ValueError('需要一份完整的单卷压缩包。')
            sizes = []
            seen = set()
            for entry in entries.strip().split('\n\n'):
                fields = dict(line.split(' = ', 1) for line in entry.splitlines() if ' = ' in line)
                if 'Path' not in fields:
                    continue
                key = path_key(output / payload_path(fields['Path']))
                if (key in seen or any(fields.get(k) for k in ('Symbolic Link', 'Hard Link', 'Reparse Point'))
                        or 'Reparse' in fields.get('Attributes', '')):
                    raise ValueError('压缩包包含重复路径或链接。')
                seen.add(key)
                sizes.append(int(fields.get('Size', '') or 0))
            require_space(output, sum(sizes))
            call_engine('x', source, password, cancel, output)
            return
        except PasswordRequired:
            shutil.rmtree(output)
            output.mkdir()
    raise PasswordRequired('需要正确的解包密码。')


def archive_layout(path):
    with core.shared_reader(path) as stream:
        layout = core.inspect(stream)
    if layout['kind'] == 'ZIP' and not layout['prefix'] and path.suffix.lower() in ZIP_DOCUMENTS:
        return {'kind': '普通文件'}
    return layout


def extract_layer(source, layout, output, passwords, cancel):
    if layout['kind'] == 'ZIP':
        with core.shared_reader(source) as stream, zipfile.ZipFile(stream) as archive:
            if not layout['encrypted'] and all(i.compress_type in ZIP_METHODS for i in archive.infolist()):
                extract_zip(archive, output, cancel)
                return
        if layout['prefix']:
            cropped = output.parent / (output.name + '.zip')
            require_space(output, source.stat().st_size - layout['prefix'])
            try:
                with core.shared_reader(source) as stream, cropped.open('xb') as dst:
                    stream.seek(layout['prefix'])
                    copy_data(stream, dst, cancel)
                extract_with_engine(cropped, output, passwords, cancel)
            finally:
                cropped.unlink(missing_ok=True)
            return
    extract_with_engine(source, output, passwords, cancel)


def output_files(folder):
    files = []
    for base, dirs, names in os.walk(folder):
        for name in dirs + names:
            path = Path(base) / name
            if linked(path.lstat()):
                raise ValueError('压缩包包含链接。')
        files.extend(Path(base) / name for name in names)
    return files


def extract(source, layout, journal, cancel, notify, password=None, destination=None, recycle=None,
            expected=None):
    original = signature(source)
    if expected is not None and original != expected:
        raise SourceChanged('原文件仍在变化，请等下载结束。')
    if linked(source.lstat()):
        raise ValueError('请选择原始文件。')
    parent = destination or source.parent
    existing = journal.known_output(source, original)
    if existing is not None:
        return {'output': str(existing), 'detail': '成品已就绪。', 'layers': []}
    candidates = dict.fromkeys(p for p in (password, source.parent.parent.name, source.parent.name) if p)
    passwords = list(candidates) or ['']
    layers = []
    with journal.staging(parent) as stage:
        current = source
        for level in range(1, 17):
            core.check_stop(cancel)
            kind = layout['kind']
            layers.append(kind)
            notify(f'正在解包 · 第 {level} 层 {kind}')
            contents = stage / f'layer-{level}'
            contents.mkdir()
            extract_layer(current, layout, contents, passwords, cancel)
            if current != source:
                current.unlink()
            files = output_files(contents)
            if not files:
                raise ValueError('压缩包内没有可交付的文件。')
            if len(files) == 1:
                nested = archive_layout(files[0])
                if nested['kind'] in ARCHIVES:
                    current, layout = files[0], nested
                    continue
            core.check_stop(cancel)
            if signature(source) != original:
                raise SourceChanged('原文件仍在变化，请等下载结束。')
            item = files[0] if len(files) == 1 else contents
            name = core.source_name(source)
            filename = item.name if len(files) == 1 else Path(name).stem or '解包成品'
            while True:
                target = unused_name(parent / filename)
                journal.record_output(source, original, item, target)
                try:
                    item.rename(target)
                    break
                except FileExistsError:
                    continue
            break
        else:
            raise ValueError('压缩包嵌套层数过多。')
    detail = '成品已就绪。'
    if recycle and not (cancel and cancel.is_set()):
        try:
            if signature(source) != original:
                raise SourceChanged('源文件已变化。')
            recycle(source)
        except (OSError, ValueError):
            detail = '成品已就绪，原件已保留。'
    return {'output': str(target), 'detail': detail, 'layers': layers}
