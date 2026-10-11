import argparse
import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import finisher_core as core


class App:
    def __init__(self, root):
        self.root = root
        self.root.title('网盘文件收尾')
        self.root.geometry('1000x640')
        self.root.minsize(820, 500)
        style = ttk.Style()
        style.theme_use('vista' if 'vista' in style.theme_names() else 'clam')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('Treeview', rowheight=30)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.closing = False
        self.results = []
        self.config_path = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NetdiskFileFinisher/设置.json'
        settings = core.read_json(self.config_path, {})
        self.folder = tk.StringVar(value=settings.get('folder', ''))
        self.output = tk.StringVar(value=settings.get('output', ''))
        self.encrypted = tk.BooleanVar(value=settings.get('encrypted', True))
        panel = ttk.Frame(root, padding=18)
        panel.pack(fill='both', expand=True)
        panel.columnconfigure(1, weight=1)
        panel.rowconfigure(6, weight=1)
        ttk.Label(panel, text='网盘文件收尾', font=('Microsoft YaHei UI', 18, 'bold')).grid(
            row=0, column=0, columnspan=3, sticky='w', pady=(0, 4))
        ttk.Label(panel, text='遇到下载收尾报错时运行。导出副本，保留原文件和网盘任务。').grid(
            row=1, column=0, columnspan=3, sticky='w', pady=(0, 14))
        for row, label, variable in ((2, '下载目录', self.folder), (3, '保存位置', self.output)):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=5)
            ttk.Entry(panel, textvariable=variable).grid(row=row, column=1, sticky='ew', pady=5)
            ttk.Button(panel, text='选择…', command=lambda v=variable: self.choose_folder(v)).grid(
                row=row, column=2, padx=(8, 0), pady=5)
        ttk.Checkbutton(panel, text='导出加密压缩包副本（保留加密，包内内容未经解密验证）',
                        variable=self.encrypted).grid(row=4, column=0, columnspan=3, sticky='w', pady=8)
        actions = ttk.Frame(panel)
        actions.grid(row=5, column=0, columnspan=3, sticky='ew', pady=(6, 12))
        self.buttons = []
        for label, command in (('扫描并自动处理', lambda: self.start()),
                               ('选择一个文件处理…', self.choose_file),
                               ('导出待核查副本…', lambda: self.choose_file(True))):
            button = ttk.Button(actions, text=label, command=command)
            button.pack(side='left', padx=(0, 8))
            self.buttons.append(button)
        self.stop_button = ttk.Button(actions, text='停止', command=self.cancel.set, state='disabled')
        self.stop_button.pack(side='right')
        table_frame = ttk.Frame(panel)
        table_frame.grid(row=6, column=0, columnspan=3, sticky='nsew')
        self.table = ttk.Treeview(table_frame, columns=('file', 'kind', 'status', 'detail'), show='headings')
        for column, title, width in (('file', '文件', 250), ('kind', '实际类型', 80),
                                      ('status', '处理结果', 100), ('detail', '检查依据 / 下一步', 390)):
            self.table.heading(column, text=title)
            self.table.column(column, width=width, minwidth=70)
        self.table.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(table_frame, command=self.table.yview)
        scrollbar.pack(side='right', fill='y')
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind('<<TreeviewSelect>>', self.show_detail)
        self.progress = ttk.Progressbar(panel, maximum=100)
        self.progress.grid(row=7, column=0, columnspan=3, sticky='ew', pady=(14, 5))
        self.status = tk.StringVar(value='请选择下载目录。' if not self.folder.get() else '准备就绪。')
        ttk.Label(panel, textvariable=self.status).grid(row=8, column=0, columnspan=3, sticky='w')
        self.detail = tk.StringVar(value='处理完后，点击一行查看导出位置。')
        ttk.Label(panel, textvariable=self.detail, wraplength=940).grid(
            row=9, column=0, columnspan=3, sticky='w', pady=(8, 4))
        ttk.Button(panel, text='打开保存文件夹', command=self.open_output).grid(
            row=10, column=0, columnspan=3, sticky='e')
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(100, self.pump)

    def choose_folder(self, variable):
        if not self.busy:
            selected = filedialog.askdirectory(initialdir=variable.get())
            if selected:
                if variable is self.folder and (not self.output.get() or
                        self.output.get() == str(Path(self.folder.get()) / '收尾导出')):
                    self.output.set(str(Path(selected) / '收尾导出'))
                variable.set(selected)

    def choose_file(self, unchecked=False):
        if self.busy:
            return
        selected = filedialog.askopenfilename(initialdir=self.folder.get(), title='选择下载临时文件',
                                              filetypes=[('下载临时文件', '*.downloading'), ('所有文件', '*.*')])
        if selected:
            if not self.folder.get().strip():
                self.folder.set(str(Path(selected).parent))
            if not self.output.get():
                self.output.set(str(Path(selected).parent / '收尾导出'))
            self.start(Path(selected), unchecked)

    def start(self, selected=None, unchecked=False):
        folder, output = Path(self.folder.get()), Path(self.output.get())
        if selected is None and (not self.folder.get().strip() or not folder.is_dir()):
            messagebox.showerror('下载目录不存在', '请选择实际的下载目录。')
            return
        if not self.output.get().strip():
            messagebox.showerror('请选择保存位置', '请选择导出副本的保存文件夹。')
            return
        settings = {'folder': str(folder), 'output': str(output), 'encrypted': self.encrypted.get()}
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as error:
            messagebox.showerror('设置保存失败', str(error))
            return
        self.busy = True
        self.cancel.clear()
        self.results = []
        self.table.delete(*self.table.get_children())
        for button in self.buttons:
            button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.status.set('正在读取下载记录…')
        threading.Thread(target=self.worker, args=(folder, output, selected, unchecked,
                                                  self.encrypted.get()), daemon=True).start()

    def worker(self, folder, output, selected, unchecked, encrypted):
        try:
            records, warnings = core.task_records()
            sources = ([(selected, records.get(os.path.normcase(str(selected.resolve())), {}))]
                       if selected else core.discover(folder, records))
            for index, (source, record) in enumerate(sources, 1):
                if self.cancel.is_set():
                    break
                name = core.source_name(source, record)
                self.events.put(('progress', f'[{index}/{len(sources)}] {name}', 0))
                result = core.process(source, record, output, self.cancel,
                                      lambda label, percent: self.events.put(
                                          ('progress', f'[{index}/{len(sources)}] {name}：{label}', percent)),
                                      encrypted_ok=encrypted, unchecked=unchecked)
                self.events.put(('result', result))
            self.events.put(('done', warnings))
        except Exception as error:
            self.events.put(('error', str(error)))
            self.events.put(('done', []))

    def pump(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'progress':
                self.status.set(event[1])
                if event[2] is None:
                    self.progress.configure(mode='indeterminate')
                    self.progress.start(20)
                else:
                    self.progress.stop()
                    self.progress.configure(mode='determinate', value=event[2])
            elif event[0] == 'result':
                result = event[1]
                self.results.append(result)
                self.table.insert('', 'end', iid=str(len(self.results) - 1), values=(
                    result['name'], result.get('kind', ''), result['status'], result['detail']))
            elif event[0] == 'error':
                messagebox.showerror('处理遇到问题', event[1])
            elif event[0] == 'done':
                self.busy = False
                self.progress.stop()
                self.progress.configure(mode='determinate', value=100)
                for button in self.buttons:
                    button.configure(state='normal')
                self.stop_button.configure(state='disabled')
                success = sum(item['status'] in ('已导出', '已处理过') for item in self.results)
                self.status.set(f'处理结束：{success} 个已导出或已处理，{len(self.results) - success} 个需要检查或等待。')
                if not self.results:
                    self.status.set('没有找到下载临时文件。可使用“选择一个文件处理”。')
                if event[1]:
                    self.detail.set('；'.join(event[1]))
        if self.closing and not self.busy:
            self.root.destroy()
        else:
            self.root.after(100, self.pump)

    def show_detail(self, _=None):
        selected = self.table.selection()
        if selected:
            result = self.results[int(selected[0])]
            self.detail.set(result['detail'] + ('\n保存到：' + result['output'] if result['output'] else ''))

    def open_output(self):
        path = Path(self.output.get())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def close(self):
        self.closing = True
        self.cancel.set()
        if not self.busy:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--smoke-test')
    parser.add_argument('--headless-file')
    parser.add_argument('--headless-output')
    parser.add_argument('--report')
    args = parser.parse_args()
    if args.headless_file:
        records, _ = core.task_records()
        source = Path(args.headless_file)
        result = core.process(source, records.get(os.path.normcase(str(source.resolve())), {}),
                              Path(args.headless_output))
        Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return
    root = tk.Tk()
    if args.smoke_test:
        root.withdraw()
    app = App(root)
    if args.smoke_test:
        root.update_idletasks()
        Path(args.smoke_test).write_text(json.dumps({'title': root.title(), 'widgets_ready': True,
                                                    'ffmpeg_available': bool(core.find_ffmpeg())},
                                                   ensure_ascii=False), encoding='utf-8')
        root.destroy()
    else:
        root.mainloop()


if __name__ == '__main__':
    main()
