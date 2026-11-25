import os
import queue
import time
from pathlib import Path

import pywintypes
import win32con
import win32event
import win32file

from archive_flow import STAGE_PREFIX, fingerprint, linked, path_key
from finisher_core import read_json, write_history

STABILITY_DELAY = 2
INCOMPLETE = ('.downloading', '.part', '.crdownload')
FILTERS = (win32con.FILE_NOTIFY_CHANGE_FILE_NAME | win32con.FILE_NOTIFY_CHANGE_DIR_NAME
           | win32con.FILE_NOTIFY_CHANGE_SIZE | win32con.FILE_NOTIFY_CHANGE_LAST_WRITE)


def eligible_name(path):
    return not path.name.lower().endswith(INCOMPLETE)


class NativeChanges:
    def __init__(self, path):
        self.handle = win32file.CreateFile(
            str(path), 1, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None, win32con.OPEN_EXISTING,
            win32con.FILE_FLAG_BACKUP_SEMANTICS | win32con.FILE_FLAG_OVERLAPPED, None)
        self.buffer = win32file.AllocateReadBuffer(65536)
        self.pending = pywintypes.OVERLAPPED()
        self.pending.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            self.arm()
        except BaseException:
            self.close()
            raise

    def arm(self):
        win32event.ResetEvent(self.pending.hEvent)
        win32file.ReadDirectoryChangesW(self.handle, self.buffer, True, FILTERS, self.pending)

    def read(self):
        if win32event.WaitForSingleObject(self.pending.hEvent, 200) == win32event.WAIT_TIMEOUT:
            return ()
        try:
            size = win32file.GetOverlappedResult(self.handle, self.pending, False)
        except pywintypes.error as error:
            if error.winerror != 1022:
                raise
            size = 0
        entries = win32file.FILE_NOTIFY_INFORMATION(self.buffer, size) if size else None
        self.arm()
        return entries

    def close(self):
        try:
            win32file.CancelIo(self.handle)
            try:
                win32file.GetOverlappedResult(self.handle, self.pending, True)
            except pywintypes.error:
                pass
        finally:
            self.handle.Close()
            self.pending.hEvent.Close()


class FolderWatch:
    def __init__(self, folder, state, journal, stop, emit, generation):
        self.folder = folder
        self.state = state
        self.journal = journal
        self.stop = stop
        self.emit = emit
        self.generation = generation
        self.root_key = path_key(folder)
        self.pending = {}
        self.dispatched = {}
        self.completed = queue.SimpleQueue()
        previous = read_json(state, {})
        self.baseline = previous.get('root') != self.root_key
        entries = previous.get('entries')
        self.seen = entries if not self.baseline and isinstance(entries, dict) else {}

    def save(self):
        write_history(self.state, {'root': self.root_key, 'entries': self.seen})

    def ignored(self, path, outputs):
        relative = path.relative_to(self.folder)
        return (any(part.startswith(STAGE_PREFIX) for part in relative.parts)
                or any(path_key(p) in outputs for p in (path, *path.parents)))

    def scan(self, folder, outputs):
        result = {}
        folders = [folder]
        while folders and not self.stop.is_set():
            current = folders.pop()
            with os.scandir(current) as children:
                for entry in children:
                    path = Path(entry.path)
                    if self.ignored(path, outputs):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if linked(info):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        folders.append(path)
                    elif eligible_name(path) and entry.is_file(follow_symlinks=False):
                        result[path_key(path)] = (path, fingerprint(info))
        return result

    def schedule(self, path, identity):
        key = path_key(path)
        if self.seen.get(key) == identity or self.dispatched.get(key) == identity:
            return
        queued = self.pending.get(key)
        if queued is None or queued[1] != identity:
            self.pending[key] = path, identity, time.monotonic() + STABILITY_DELAY

    def reconcile(self):
        candidates = self.scan(self.folder, self.journal.output_paths())
        if self.stop.is_set():
            return
        if self.baseline:
            self.seen = {key: identity for key, (_, identity) in candidates.items()}
            self.baseline = False
        else:
            self.seen = {key: identity for key, identity in self.seen.items() if key in candidates}
            for path, identity in candidates.values():
                self.schedule(path, identity)
        self.save()

    def change(self, path, outputs):
        if self.ignored(path, outputs):
            return
        try:
            info = path.lstat()
        except FileNotFoundError:
            key = path_key(path)
            self.pending.pop(key, None)
            if key in self.seen:
                self.seen.pop(key)
                self.save()
            return
        if linked(info):
            return
        if path.is_dir():
            for candidate, identity in self.scan(path, outputs).values():
                self.schedule(candidate, identity)
        elif eligible_name(path) and path.is_file():
            self.schedule(path, fingerprint(info))

    def dispatch_ready(self):
        now = time.monotonic()
        due = [key for key, (_, _, deadline) in self.pending.items() if deadline <= now]
        if not due:
            return
        outputs = self.journal.output_paths()
        for key in due:
            path, previous, _ = self.pending.pop(key)
            if self.ignored(path, outputs):
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if linked(info) or not path.is_file():
                continue
            identity = fingerprint(info)
            if identity == previous:
                self.dispatched[key] = identity
                self.emit('auto', self.generation, path, identity)
            else:
                self.schedule(path, identity)

    def flush_completed(self):
        changed = False
        while not self.completed.empty():
            path, identity, retry = self.completed.get()
            key = path_key(path)
            if self.dispatched.get(key) == identity:
                self.dispatched.pop(key)
            if retry:
                self.change(path, self.journal.output_paths())
            else:
                self.seen[key] = identity
                changed = True
        if changed:
            self.save()

    def run(self):
        watch = None
        try:
            watch = NativeChanges(self.folder)
            self.reconcile()
            self.emit('watch', self.generation, '正在监听')
            while not self.stop.is_set():
                changes = watch.read()
                if changes is None:
                    self.reconcile()
                elif changes:
                    outputs = self.journal.output_paths()
                    for _, name in changes:
                        self.change(self.folder / name, outputs)
                self.flush_completed()
                self.dispatch_ready()
        except (OSError, pywintypes.error, ValueError):
            self.emit('watch', self.generation, '监听中断。请检查目录后重新开启。')
        finally:
            if watch is not None:
                watch.close()
