import argparse
import json
import os
import queue
import threading
import tkinter as tk
import zipfile
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import archive_flow as archives
import finisher_core as core
from folder_watch import FolderWatch
from windows_io import accept_drop, instance_mutex, recycle, reveal

VERSION = '1.1.0'


def data_directory():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NetdiskFileFinisher'


def handle_file(source, journal, cancel, notify, *, automatic=False, password=None,
                recycle_original=False, destination=None, expected=None):
    try:
        original = archives.signature(source)
        if expected is not None and original != expected:
            raise archives.SourceChanged('文件仍在变化。')
        layout = archives.archive_layout(source)
        if layout['kind'] in archives.ARCHIVES:
            records = {}
            if source.name.lower().endswith(core.SUFFIXES):
                records, _ = core.task_records()
            record = records.get(archives.path_key(source), {})
            if (record.get('status') == 2 or
                    (record.get('file_size') and record.get('complete_size') is not None
                     and record['complete_size'] < record['file_size'] and record.get('error_code') != 1000022)):
                return {'detail': '网盘仍在下载。下载结束后再试。'}
            expected_hash = core.normalized_md5(record.get('md5'))
            if expected_hash and layout['kind'] == 'ZIP':
                with core.shared_reader(source) as stream:
                    current = core.hash_stream(stream, source.stat().st_size, 'md5', cancel,
                                               lambda label, _: notify(label), '核对网盘校验值')
                if current != expected_hash:
                    return {'detail': '网盘校验值不一致。请确认下载完整后重试。'}
            return archives.extract(source, layout, journal, cancel, notify, password, destination,
                                    recycle if recycle_original else None, original)
        if automatic:
            return {'ignored': True}
        if source.suffix.lower() in archives.ZIP_DOCUMENTS:
            return {'detail': '该文件可以用对应应用打开。'}
        records, _ = core.task_records()
        result = core.process(source, records.get(archives.path_key(source), {}),
                              destination or source.parent / '收尾导出', cancel, lambda text, _: notify(text))
        return {'output': result.get('output'), 'detail': result['detail'],
                'canceled': result['status'] == '已停止'}
    except archives.PasswordRequired as error:
        return {'detail': str(error), 'password_required': True}
    except archives.SourceChanged as error:
        return {'detail': str(error), 'changed': True}
    except core.Stopped:
        return {'detail': '已停止。', 'canceled': True}
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        if isinstance(error, OSError) and not source.exists():
            return {'detail': '文件已移动或不可访问。'}
        if isinstance(error, OSError) and getattr(error, 'errno', None) is not None:
            return {'detail': ('磁盘空间不足。请释放空间后重试。' if error.errno == 28
                               else '无法读取或保存文件。请检查文件占用和目录权限。')}
        return {'detail': str(error)}


class App:
    def __init__(self, root, settings_path, journal, smoke=False):
        self.root = root
        self.settings_path = settings_path
        self.journal = journal
        self.settings = core.read_json(settings_path, {})
        self.events = queue.Queue()
        self.jobs = deque()
        self.queued = set()
        self.current = None
        self.cancel = threading.Event()
        self.closing = False
        self.watcher = None
        self.watch_stop = threading.Event()
        self.watch_thread = None
        self.generation = 0
        self.watch_folder = tk.StringVar(value=self.settings.get('watch_folder', ''))
        self.watch_enabled = tk.BooleanVar(value=self.settings.get('watch_enabled', False))
        self.recycle_original = tk.BooleanVar(value=self.settings.get('recycle_original', False))
        self.status = tk.StringVar(value='请选择要处理的文件。')
        self.watch_status = tk.StringVar(value='监听未开启')
        self.action = None
        self.password_needed = None
        root.title('网盘文件收尾')
        root.geometry('680x360')
        root.minsize(560, 340)
        style = ttk.Style()
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        panel = ttk.Frame(root, padding=22)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='拖入文件', font=('Microsoft YaHei UI', 22, 'bold')).pack(anchor='w')
        ttk.Button(panel, text='选择文件…', command=self.pick).pack(anchor='w', pady=(10, 20))
        settings = ttk.Frame(panel)
        settings.pack(fill='x')
        settings.columnconfigure(1, weight=1)
        self.watch_toggle = ttk.Checkbutton(settings, text='自动解包', variable=self.watch_enabled,
                                            command=self.toggle_watch)
        self.watch_toggle.grid(row=0, column=0, sticky='w')
        ttk.Entry(settings, textvariable=self.watch_folder, state='readonly').grid(
            row=0, column=1, sticky='ew', padx=10)
        ttk.Button(settings, text='选择目录…', command=self.pick_folder).grid(row=0, column=2)
        ttk.Label(panel, textvariable=self.watch_status).pack(anchor='w', pady=(4, 6))
        ttk.Checkbutton(panel, text='解包成功后将原件移到回收站', variable=self.recycle_original,
                        command=self.save).pack(anchor='w')
        ttk.Separator(panel).pack(fill='x', pady=14)
        ttk.Label(panel, textvariable=self.status, wraplength=500).pack(anchor='w')
        controls = ttk.Frame(panel)
        controls.pack(fill='x', pady=(8, 0))
        self.extra = ttk.Button(controls, command=lambda: self.action and self.action())
        self.stop_button = ttk.Button(controls, text='停止当前任务', command=self.cancel.set, state='disabled')
        self.stop_button.pack(side='right')
        try:
            self.drop_close = accept_drop(root, self.add_manual)
        except OSError as error:
            self.status.set(str(error))
            self.drop_close = None
        root.protocol('WM_DELETE_WINDOW', self.close)
        if not smoke:
            if self.watch_enabled.get():
                self.start_watch()
            root.after(80, self.pump)

    def save(self):
        self.settings.update(watch_folder=self.watch_folder.get(), watch_enabled=self.watch_enabled.get(),
                             recycle_original=self.recycle_original.get())
        try:
            core.write_history(self.settings_path, self.settings)
        except OSError:
            self.status.set('设置保存失败。请检查应用数据目录。')

    def pick_folder(self):
        answer = filedialog.askdirectory(initialdir=self.watch_folder.get() or self.settings.get('folder') or None)
        if answer and str(Path(answer)) != self.watch_folder.get():
            self.stop_watch(reset=True)
            self.watch_folder.set(str(Path(answer)))
            self.save()
            if self.watch_enabled.get():
                self.start_watch()

    def toggle_watch(self):
        self.stop_watch(reset=True)
        if self.watch_enabled.get() and not self.watch_folder.get():
            answer = filedialog.askdirectory(initialdir=self.settings.get('folder') or None)
            if answer:
                self.watch_folder.set(str(Path(answer)))
            else:
                self.watch_enabled.set(False)
        self.save()
        if self.watch_enabled.get():
            self.start_watch()

    def emit(self, *values):
        self.events.put(values)

    @property
    def watch_state(self):
        return self.settings_path.with_name('watch.json')

    def start_watch(self):
        self.watch_stop = threading.Event()
        self.watcher = FolderWatch(Path(self.watch_folder.get()), self.watch_state, self.journal,
                                   self.watch_stop, self.emit, self.generation)
        self.watch_status.set('正在登记目录…')
        self.watch_thread = threading.Thread(target=self.watcher.run, daemon=True)
        self.watch_thread.start()

    def stop_watch(self, reset=False):
        self.watch_stop.set()
        self.generation += 1
        if self.watch_thread is not None:
            self.watch_thread.join()
        self.watch_thread = None
        if reset:
            self.watch_state.unlink(missing_ok=True)
            if self.current is not None and self.current[1] is not None:
                self.cancel.set()
            self.watch_status.set('监听未开启')

    def pick(self):
        files = filedialog.askopenfilenames(initialdir=self.settings.get('folder') or None, title='选择要处理的文件')
        self.add_manual(files)

    def add_manual(self, files):
        for value in files:
            source = Path(value).absolute()
            if source.is_file():
                self.enqueue(source)

    def enqueue(self, source, generation=None, identity=None, password=None):
        key = archives.path_key(source)
        if key in self.queued:
            if generation == self.generation and self.watcher:
                self.watcher.completed.put((source, identity, True))
            return
        self.queued.add(key)
        job = source, generation, identity, password
        if generation is None:
            self.jobs.appendleft(job)
        else:
            self.jobs.append(job)
        self.start_next()

    def start_next(self):
        if self.current is not None or self.closing:
            return
        while self.jobs:
            job = self.jobs.popleft()
            if job[1] is None or job[1] == self.generation:
                break
            self.queued.discard(archives.path_key(job[0]))
        else:
            return
        self.current = job
        self.cancel.clear()
        self.stop_button.config(state='normal')
        self.extra.pack_forget()
        self.action = None
        source, generation, identity, password = job
        self.status.set(f'正在检查 {source.name}')
        threading.Thread(target=self.work, args=(source, generation is not None, password, identity,
                                                 self.recycle_original.get()), daemon=True).start()

    def work(self, source, automatic, password, identity, should_recycle):
        try:
            result = handle_file(source, self.journal, self.cancel,
                                 lambda label: self.emit('progress', f'{label} · {source.name}'),
                                 automatic=automatic, password=password, recycle_original=should_recycle,
                                 expected=identity)
        except Exception:
            result = {'detail': '处理失败。请重新打开文件后重试。'}
        self.emit('done', result)

    def try_password(self, source):
        value = simpledialog.askstring('解包密码', '输入密码', show='•', parent=self.root)
        if value:
            self.enqueue(source, password=value)

    def done(self, result):
        source, generation, identity, _ = self.current
        self.current = None
        self.queued.discard(archives.path_key(source))
        self.stop_button.config(state='disabled')
        if generation is not None and generation == self.generation and self.watcher:
            if not (self.closing and result.get('canceled')):
                self.watcher.completed.put((source, identity, bool(result.get('changed'))))
        if not self.closing:
            if result.get('password_required'):
                self.password_needed = source
            elif self.password_needed == source:
                self.password_needed = None
            if self.password_needed is not None:
                pending = self.password_needed
                self.status.set(f'{pending.name} · 需要解包密码。')
                self.extra.config(text='输入密码并重试')
                self.action = lambda: self.try_password(pending)
                self.extra.pack(side='left')
            else:
                self.status.set('等待新下载文件。' if result.get('ignored') else result['detail'])
            if result.get('output'):
                output = Path(result['output'])
                if self.password_needed is None:
                    self.extra.config(text='查看成品')
                    self.action = lambda: self.reveal_output(output)
                    self.extra.pack(side='left')
                if generation is None:
                    self.reveal_output(output)
        self.start_next()

    def reveal_output(self, output):
        try:
            reveal(output)
        except OSError:
            self.status.set(f'成品位置 · {output}')

    def pump(self):
        try:
            for _ in range(100):
                event, *args = self.events.get_nowait()
                if event == 'auto' and args[0] == self.generation and not self.closing:
                    self.enqueue(args[1], args[0], args[2])
                elif event == 'watch' and args[0] == self.generation:
                    self.watch_status.set(args[1])
                elif event == 'progress':
                    self.status.set(args[0])
                elif event == 'done':
                    self.done(args[0])
        except queue.Empty:
            pass
        if self.closing and self.current is None:
            self.destroy()
        else:
            self.root.after(80, self.pump)

    def destroy(self):
        if self.drop_close:
            self.drop_close()
        self.root.destroy()

    def close(self):
        self.closing = True
        self.stop_watch()
        self.cancel.set()
        if self.current is None:
            self.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--smoke-test', type=Path)
    parser.add_argument('--headless-file', type=Path)
    parser.add_argument('--extract-file', type=Path)
    parser.add_argument('--headless-output', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    data = data_directory()
    if args.headless_file:
        records, _ = core.task_records()
        result = core.process(args.headless_file, records.get(archives.path_key(args.headless_file), {}),
                              args.headless_output)
    elif args.extract_file:
        journal = archives.Journal(data)
        if args.headless_output:
            args.headless_output.mkdir(parents=True, exist_ok=True)
        result = handle_file(args.extract_file.absolute(), journal, None, lambda _: None,
                             destination=args.headless_output)
    else:
        mutex, already_open = instance_mutex()
        try:
            if already_open:
                return
            journal = archives.Journal(data)
            if not args.smoke_test:
                journal.recover()
            root = tk.Tk()
            if args.smoke_test:
                root.withdraw()
            app = App(root, data / '设置.json', journal, smoke=bool(args.smoke_test))
            if args.smoke_test:
                engine = archives.engine_path()
                args.smoke_test.write_text(json.dumps({
                    'version': VERSION, 'title': root.title(), 'widgets_ready': True,
                    'drag_ready': bool(app.drop_close),
                    'engine_ready': engine.is_file() and engine.with_suffix('.dll').is_file(),
                    'ffmpeg_available': bool(core.find_ffmpeg())}, ensure_ascii=False), encoding='utf-8')
                app.destroy()
            else:
                root.mainloop()
        finally:
            mutex.Close()
        return
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
