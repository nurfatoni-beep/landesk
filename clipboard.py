"""Akses clipboard teks lintas platform tanpa dependency tambahan.

Windows: Win32 API via ctypes. Linux: xclip / xsel / wl-clipboard (yang tersedia).
get_text() mengembalikan None kalau clipboard tidak bisa dibaca.
"""
import shutil
import subprocess
import sys
import time

MAX_LEN = 1_000_000

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as wt

    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32.OpenClipboard.argtypes = [wt.HWND]
    _u32.OpenClipboard.restype = wt.BOOL
    _u32.GetClipboardData.argtypes = [wt.UINT]
    _u32.GetClipboardData.restype = wt.HANDLE
    _u32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    _u32.SetClipboardData.restype = wt.HANDLE
    _u32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
    _k32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    _k32.GlobalAlloc.restype = wt.HGLOBAL
    _k32.GlobalLock.argtypes = [wt.HGLOBAL]
    _k32.GlobalLock.restype = ctypes.c_void_p
    _k32.GlobalUnlock.argtypes = [wt.HGLOBAL]
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002

    def _open():
        for _ in range(10):
            if _u32.OpenClipboard(None):
                return True
            time.sleep(0.02)
        return False

    def get_text():
        if not _open():
            return None
        try:
            if not _u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            h = _u32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            p = _k32.GlobalLock(h)
            try:
                return ctypes.wstring_at(p)[:MAX_LEN] if p else ""
            finally:
                _k32.GlobalUnlock(h)
        finally:
            _u32.CloseClipboard()

    def set_text(text: str) -> bool:
        data = text[:MAX_LEN].encode("utf-16-le") + b"\x00\x00"
        if not _open():
            return False
        try:
            _u32.EmptyClipboard()
            h = _k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            p = _k32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            _k32.GlobalUnlock(h)
            return bool(_u32.SetClipboardData(CF_UNICODETEXT, h))
        finally:
            _u32.CloseClipboard()

    def available() -> bool:
        return True

else:
    def _tool():
        if shutil.which("xclip"):
            return (["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"])
        if shutil.which("xsel"):
            return (["xsel", "-b", "-o"], ["xsel", "-b", "-i"])
        if shutil.which("wl-paste"):
            return (["wl-paste", "-n"], ["wl-copy"])
        return None

    def available() -> bool:
        return _tool() is not None

    def get_text():
        t = _tool()
        if not t:
            return None
        try:
            r = subprocess.run(t[0], capture_output=True, timeout=2)
            return r.stdout.decode("utf-8", "replace")[:MAX_LEN] if r.returncode == 0 else ""
        except Exception:
            return None

    def set_text(text: str) -> bool:
        t = _tool()
        if not t:
            return False
        try:
            # xclip/xsel tetap hidup sebagai pemilik clipboard -> jangan ditunggu
            p = subprocess.Popen(t[1], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            p.stdin.write(text[:MAX_LEN].encode("utf-8"))
            p.stdin.close()
            return True
        except Exception:
            return False
