import ctypes
import os
import subprocess
from ctypes import wintypes

import pythoncom
import win32api
import win32con
import win32event
import win32file
from win32com.shell import shell, shellcon

RECYCLE_ONLY = 0x00080000
WM_DROPFILES = 0x0233


def reveal(path):
    if path.is_dir():
        os.startfile(path)
    else:
        subprocess.Popen(['explorer.exe', '/select,', str(path)])


def recycle(path):
    # Transfers are performed in an STA by the processing worker.
    pythoncom.CoInitialize()
    try:
        volume = win32file.GetVolumePathName(str(path))
        if win32file.GetDriveType(volume) != win32con.DRIVE_FIXED:
            raise OSError('此磁盘不支持自动回收。')
        shell.SHQueryRecycleBin(volume)
        operation = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None,
                                               pythoncom.CLSCTX_INPROC_SERVER,
                                               shell.IID_IFileOperation)
        operation.SetOperationFlags(RECYCLE_ONLY | shellcon.FOF_ALLOWUNDO | shellcon.FOF_SILENT
                                    | shellcon.FOF_NOERRORUI | shellcon.FOFX_EARLYFAILURE
                                    | shellcon.FOF_WANTNUKEWARNING)
        operation.DeleteItem(shell.SHCreateItemFromParsingName(str(path), None, shell.IID_IShellItem))
        operation.PerformOperations()
        if operation.GetAnyOperationsAborted() or path.exists():
            raise OSError('系统未能回收原件。')
    except pythoncom.com_error as error:
        raise OSError('系统未能回收原件。') from error
    finally:
        pythoncom.CoUninitialize()


def instance_mutex():
    handle = win32event.CreateMutex(None, False, r'Local\NetdiskFileFinisher.v1')
    return handle, win32api.GetLastError() == 183


def accept_drop(root, callback):
    root.update_idletasks()
    user = ctypes.windll.user32
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    window = user.GetAncestor(root.winfo_id(), 2)
    controls = ctypes.windll.comctl32
    shell32 = ctypes.windll.shell32
    shell32.DragFinish.argtypes = [wintypes.HANDLE]
    pointer = ctypes.c_size_t
    procedure = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, pointer,
                                   ctypes.c_ssize_t, pointer, pointer)
    controls.SetWindowSubclass.argtypes = [wintypes.HWND, procedure, pointer, pointer]
    controls.RemoveWindowSubclass.argtypes = [wintypes.HWND, procedure, pointer]
    controls.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, pointer, ctypes.c_ssize_t]
    controls.DefSubclassProc.restype = ctypes.c_ssize_t

    @procedure
    def receive(hwnd, msg, wparam, lparam, subclass, data):
        if msg != WM_DROPFILES:
            return controls.DefSubclassProc(hwnd, msg, wparam, lparam)
        try:
            paths = [shell.DragQueryFileW(wparam, i)
                     for i in range(shell.DragQueryFileW(wparam, -1))]
            root.after_idle(callback, paths)
        finally:
            shell32.DragFinish(wparam)
        return 0

    if not controls.SetWindowSubclass(window, receive, 1, 0):
        raise OSError('拖放不可用。可用“选择文件”打开。')
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    shell32.DragAcceptFiles(window, True)

    def close():
        shell32.DragAcceptFiles(window, False)
        controls.RemoveWindowSubclass(window, receive, 1)

    return close
