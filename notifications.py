# -*- coding: utf-8 -*-
"""Системные уведомления Windows без сторонних библиотек.

Используется значок в области уведомлений и обычный balloon: он появляется
поверх всех окон и остаётся в центре уведомлений, даже если окно программы
свёрнуто. Значок живёт только несколько секунд после показа — постоянной
иконки в трее у программы нет.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import wintypes

LOGGER = logging.getLogger("media_downloader.notifications")

_ICON_TTL = 12.0


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HANDLE),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeout", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HANDLE),
    ]


_WNDPROC_TYPE = getattr(ctypes, "WINFUNCTYPE", None)
if _WNDPROC_TYPE is not None:
    _WNDPROC = _WNDPROC_TYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
else:  # pragma: no cover - вне Windows модуль не используется
    _WNDPROC = None


if _WNDPROC is not None:

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

else:  # pragma: no cover - вне Windows модуль не используется
    _WNDCLASSW = None

_NIM_ADD = 0
_NIM_MODIFY = 1
_NIM_DELETE = 2
_NIM_SETVERSION = 4
_NIF_MESSAGE = 0x01
_NIF_ICON = 0x02
_NIF_TIP = 0x04
_NIF_INFO = 0x10
_NIF_SHOWTIP = 0x80
_NIIF_INFO = 0x01
_NOTIFYICON_VERSION_4 = 4
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x10
_LR_DEFAULTSIZE = 0x40


def _bind_prototypes() -> None:
    """Без явных restype ctypes обрезает 64-битные дескрипторы до int."""
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    shell32 = ctypes.windll.shell32

    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]


if os.name == "nt":
    try:
        _bind_prototypes()
    except Exception:  # pragma: no cover - зависит от версии Windows
        LOGGER.warning("Could not configure Win32 prototypes", exc_info=True)


class SystemNotifier:
    """Показывает уведомление Windows, если система это позволяет."""

    def __init__(self, icon_path: str | None = None, tooltip: str = "Media Downloader"):
        self.icon_path = str(icon_path) if icon_path else None
        self.tooltip = tooltip
        self._enabled = os.name == "nt"
        self._hwnd = None
        self._hicon = None
        self._visible = False
        self._proc = None
        self._class = f"MediaDownloaderNotify{os.getpid()}"
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None

    def available(self) -> bool:
        return self._enabled

    # ---------- window plumbing ----------

    def _ensure_window(self) -> bool:
        if self._hwnd:
            return True
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            self._proc = _WNDPROC(self._wndproc)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = kernel32.GetModuleHandleW(None)
            wc.lpszClassName = self._class
            user32.RegisterClassW(ctypes.byref(wc))

            hwnd = user32.CreateWindowExW(
                0, self._class, self._class, 0, 0, 0, 0, 0,
                None, None, wc.hInstance, None,
            )
            if not hwnd:
                LOGGER.warning("Could not create notification window")
                return False
            self._hwnd = hwnd
            self._hicon = self._load_icon()
            return True
        except Exception:
            LOGGER.warning("Could not prepare notifications", exc_info=True)
            self._enabled = False
            return False

    def _load_icon(self):
        try:
            user32 = ctypes.windll.user32
            if self.icon_path and os.path.isfile(self.icon_path):
                icon = user32.LoadImageW(
                    None, self.icon_path, _IMAGE_ICON, 32, 32,
                    _LR_LOADFROMFILE | _LR_DEFAULTSIZE,
                )
                if icon:
                    return icon
            return user32.LoadIconW(None, 32512)  # IDI_APPLICATION
        except Exception:
            LOGGER.debug("Could not load notification icon", exc_info=True)
            return None

    def _wndproc(self, hwnd, message, wparam, lparam):
        try:
            return ctypes.windll.user32.DefWindowProcW(hwnd, message, wparam, lparam)
        except Exception:
            return 0

    def _data(self) -> _NOTIFYICONDATAW:
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.szTip = self.tooltip[:127]
        return data

    def _add_icon(self) -> None:
        flags = _NIF_MESSAGE | _NIF_TIP | _NIF_SHOWTIP
        if self._hicon:
            flags |= _NIF_ICON
        data = self._data()
        data.uFlags = flags
        data.uCallbackMessage = 0x0400 + 1
        data.hIcon = self._hicon
        ctypes.windll.shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data))
        data.uTimeout = _NOTIFYICON_VERSION_4
        ctypes.windll.shell32.Shell_NotifyIconW(_NIM_SETVERSION, ctypes.byref(data))
        self._visible = True

    # ---------- public ----------

    def show(self, title: str, message: str) -> bool:
        if not self._enabled or not message:
            return False
        try:
            with self._lock:
                if not self._ensure_window():
                    return False
                if not self._visible:
                    self._add_icon()
                data = self._data()
                data.uFlags = _NIF_INFO
                data.szInfoTitle = (title or self.tooltip)[:63]
                data.szInfo = message[:255]
                data.dwInfoFlags = _NIIF_INFO
                data.uTimeout = 10000
                ok = bool(ctypes.windll.shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data)))
                if not ok:
                    LOGGER.info("Shell_NotifyIcon rejected the notification")
                self._schedule_hide()
                return ok
        except Exception:
            LOGGER.warning("Could not show notification", exc_info=True)
            self._enabled = False
            return False

    def _schedule_hide(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(_ICON_TTL, self._hide)
        self._timer.daemon = True
        self._timer.start()

    def _hide(self) -> None:
        with self._lock:
            if not self._visible or not self._hwnd:
                return
            try:
                data = self._data()
                ctypes.windll.shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
            except Exception:
                LOGGER.debug("Could not remove notification icon", exc_info=True)
            self._visible = False

    def close(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._hide()
        with self._lock:
            if self._hwnd:
                try:
                    ctypes.windll.user32.DestroyWindow(self._hwnd)
                except Exception:
                    LOGGER.debug("Could not destroy notification window", exc_info=True)
                self._hwnd = None
        self._enabled = False
