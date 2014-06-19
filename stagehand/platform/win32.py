__all__ = ['start', 'stop']

from sys import platform, exit
from ctypes import (windll, Structure, sizeof, WINFUNCTYPE, pointer, byref, c_uint, c_int, c_char, c_wchar)
from ctypes.wintypes import (HWND, HANDLE, HICON, HBRUSH, HMENU, POINT, LPCWSTR, WPARAM, LPARAM, MSG, RECT, DWORD, WORD)

import os
import asyncio
import webbrowser
import threading

from stagehand.config import config
from stagehand.utils import get_file_from_zip
from stagehand.toolbox.utils import get_temp_path, tobytes

kernel32 = windll.kernel32
gdi32 = windll.gdi32
user32 = windll.user32
shell32 = windll.shell32
comctl32 = windll.comctl32

WNDPROCTYPE = WINFUNCTYPE(c_int, HWND, c_uint, WPARAM, LPARAM)

# Constants copied from win32con.py from pywin32
MF_BYPOSITION = 1024
MF_STRING = 0
MF_SEPARATOR = 2048
TPM_LEFTALIGN = 0
TPM_RIGHTBUTTON = 2
TPM_RETURNCMD = 256
TPM_NONOTIFY = 128
WM_CREATE = 1
WM_DESTROY = 2
WM_COMMAND = 273
WM_QUIT = 18
WM_PAINT = 15
DT_SINGLELINE = 32
DT_CENTER = 1
DT_VCENTER = 4
WM_APP = 32768
WM_LBUTTONUP = 514
WM_MBUTTONUP = 520
WM_RBUTTONUP = 517
WM_LBUTTONDBLCLK = 515
WS_OVERLAPPEDWINDOW = 13565952
SW_SHOWNORMAL = 1

class WNDCLASSEX(Structure):
    _fields_ = [
        ("cbSize", c_uint),
        ("style", c_uint),
        ("lpfnWndProc", WNDPROCTYPE),
        ("cbClsExtra", c_int),
        ("cbWndExtra", c_int),
        ("hInstance", HANDLE),
        ("hIcon", HANDLE),
        ("hCursor", HANDLE),
        ("hBrush", HBRUSH),
        ("lpszMenuName", LPCWSTR),
        ("lpszClassName", LPCWSTR),
        ("hIconSm", HANDLE),
    ]

class PAINTSTRUCT(Structure):
    _fields_ = [
        ('hdc', c_int),
        ('fErase', c_int),
        ('rcPaint', RECT),
        ('fRestore', c_int),
        ('fIncUpdate', c_int),
        ('rgbReserved', c_char * 32)
    ]

class GUID(Structure):
    _fields_ = [
        ('Data1', DWORD),
        ('Data2', WORD),
        ('Data3', WORD),
        ('Data4', c_char * 8)
    ]

class NOTIFYICONDATA(Structure):
    """http://msdn.microsoft.com/en-us/library/windows/desktop/bb773352(v=vs.85).aspx"""
    _fields_ = [
        ('cbSize', DWORD),
        ('hWnd', HWND),
        ('uID', c_uint),
        ('uFlags', c_uint),
        ('uCallbackMessage', c_uint),
        ('hIcon', HICON),
        ('szTip', c_wchar * 128),
        ('dwState', DWORD),
        ('dwStateMask', DWORD),
        ('szInfo', c_wchar * 256),
        ('uVersion', c_uint), # unioned with uTimeout
        ('szInfoTitle', c_wchar * 64),
        ('dwInfoFlags', DWORD),
        ('guidItem', GUID),
        ('hBalloonIcon', HICON)
    ]

user32.DefWindowProcW.argtypes = [HWND, c_uint, WPARAM, LPARAM]

ID_OPEN = 2000
ID_SETTINGS = 2001
ID_OPEN_TVDIR = 2002
ID_OPEN_LOGS = 2003
ID_EXIT = 2010


class Plugin:
    def __init__(self, manager):
        self.manager = manager
        self.hwnd = None
        self.running_event = threading.Event()

    def get_icon_path(self):
        try:
            info, mtime, f = get_file_from_zip('data', 'win32tray.ico')
        except AttributeError:
            return os.path.join(self.manager.paths.data, 'win32tray.ico')
        else:
            path = os.path.join(get_temp_path('stagehand'), 'win32tray.ico')
            with open(path, 'wb') as outf:
                outf.write(f.read())
            f.close()
            return path


    def show_popup_menu(self):
        menu = user32.CreatePopupMenu()
        user32.InsertMenuW(menu, 0, MF_BYPOSITION | MF_STRING, ID_OPEN, 'Open in Browser')
        user32.InsertMenuW(menu, 1, MF_BYPOSITION | MF_SEPARATOR, 0, None)
        user32.InsertMenuW(menu, 2, MF_BYPOSITION | MF_STRING, ID_OPEN_TVDIR, 'Open TV Folder')
        user32.InsertMenuW(menu, 3, MF_BYPOSITION | MF_STRING, ID_OPEN_LOGS, 'Open Logs Folder')
        user32.InsertMenuW(menu, 4, MF_BYPOSITION | MF_SEPARATOR, 0, None)
        user32.InsertMenuW(menu, 5, MF_BYPOSITION | MF_STRING, ID_SETTINGS, 'Settings')
        user32.InsertMenuW(menu, 6, MF_BYPOSITION | MF_STRING, ID_EXIT, 'Exit')
        user32.SetMenuDefaultItem(menu, ID_OPEN, False)
        user32.SetFocus(self.hwnd)

        pt = POINT()
        user32.GetCursorPos(byref(pt))
        cmd = user32.TrackPopupMenu(menu, TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        user32.SendMessageA(self.hwnd, WM_COMMAND, cmd, 0)
        user32.DestroyMenu(menu)

    def stop(self):
        if not self.hwnd:
            return

        nid = NOTIFYICONDATA()
        nid.cbSize = sizeof(NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uId = 1
        shell32.Shell_NotifyIcon(2, byref(nid))
        user32.SendMessageA(self.hwnd, WM_QUIT, 0, 0)


    def PyWndProcedure(self, hwnd, message, wParam, lParam):
        if message == WM_PAINT:
            ps = PAINTSTRUCT()
            rect = RECT()
            hdc = user32.BeginPaint(hwnd, byref(ps))
            user32.GetClientRect(hwnd, byref(rect))
            user32.DrawTextW(hdc, u"Hello, Windows 98!", -1, byref(rect),
                             DT_SINGLELINE | DT_CENTER | DT_VCENTER)
            user32.EndPaint(hwnd, byref(ps))
            return 0
        elif message == WM_DESTROY or message == WM_QUIT:
            user32.PostQuitMessage(0)
            return 0
        elif message == WM_CREATE:
            nid = NOTIFYICONDATA()
            nid.cbSize = sizeof(NOTIFYICONDATA)
            nid.hWnd = hwnd
            nid.uId = 1
            nid.uFlags = 1 | 2 | 4
            nid.szTip = "Stagehand"
            nid.uCallbackMessage = WM_APP

            icon = HICON()
            iconpath = self.get_icon_path()
            shell32.ExtractIconEx(tobytes(iconpath, fs=True), 0, None, byref(icon), 1)
            nid.hIcon = icon
            shell32.Shell_NotifyIconW(0, byref(nid))

            if self.running_event:
                self.running_event.set()
                self.running_event = None


        elif message == WM_APP:
            if 0 and lParam == WM_MBUTTONUP:
                nid = NOTIFYICONDATA()
                nid.cbSize = sizeof(NOTIFYICONDATA)
                nid.hWnd = hwnd
                nid.uId = 1
                nid.uFlags = 0x10
                nid.szInfo = "New episode for Family Guy available"
                nid.szInfoTitle = "New Episodes"
                nid.uVersion = 15000
                nid.dwInfoFlags = 1
                shell32.Shell_NotifyIconW(1, byref(nid))
                return 0

            elif lParam == WM_RBUTTONUP or lParam == WM_LBUTTONUP:
                self.show_popup_menu()
                return 0

            elif lParam == WM_LBUTTONDBLCLK:
                webbrowser.open('http://localhost:{}/downloads/'.format(config.web.port))

        elif message == WM_COMMAND:
            if wParam == ID_OPEN:
                webbrowser.open('http://localhost:{}/'.format(config.web.port))
            elif wParam == ID_SETTINGS:
                webbrowser.open('http://localhost:{}/settings/'.format(config.web.port))
            elif wParam == ID_OPEN_TVDIR:
                path = os.path.expanduser(config.misc.tvdir).replace('/', os.path.sep)
                os.makedirs(path, exist_ok=True)
                shell32.ShellExecuteW(self.hwnd, 'explore', path, None, None, SW_SHOWNORMAL)
            elif wParam == ID_OPEN_LOGS:
                path = self.manager.paths.logs
                shell32.ShellExecuteW(self.hwnd, 'explore', path, None, None, SW_SHOWNORMAL)
            elif wParam == ID_EXIT:
                self.stop()
                self.manager.loop.call_soon_threadsafe(self.manager.loop.stop)
            return 0

        return user32.DefWindowProcW(hwnd, message, wParam, lParam)


    def start(self):
        t = threading.Thread(target=self.run)
        t.start()
        if not self.running_event.wait(2):
            print('timed out waiting for thread')


    def run(self):
        WndProc = WNDPROCTYPE(self.PyWndProcedure)
        hInst = kernel32.GetModuleHandleW(0)
        szAppName = 'Stagehand'
        wndclass = WNDCLASSEX()
        wndclass.cbSize = sizeof(WNDCLASSEX)
        wndclass.lpfnWndProc = WndProc
        wndclass.hInstance = hInst
        wndclass.lpszClassName = 'Stagehand'

        user32.RegisterClassExW(byref(wndclass))
        self.hwnd = user32.CreateWindowExW(0,
                                          'Stagehand',
                                          'Stagehand',
                                          WS_OVERLAPPEDWINDOW,
                                          0, 0, 250, 150,
                                          0, 0, hInst, None)
        msg = MSG()
        while user32.GetMessageW(byref(msg), 0, 0, 0) != 0:
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))



@asyncio.coroutine
def start(manager):
    global plugin
    plugin = Plugin(manager)
    plugin.start()

def stop():
    if plugin:
        plugin.stop()


if __name__ == '__main__':
    p = Plugin()
    p.start(None)
