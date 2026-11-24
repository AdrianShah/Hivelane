"""The desktop bee on Windows: an animated icon in the system tray.

Pure standard library: ctypes for the Win32 tray (Shell_NotifyIcon), a plain
socket for the daemon's GET /bee. No WinForms, no PowerShell and no .NET, so it
stays a few MB. hivelane/bee.py runs this file by path with `pythonw -I -S`
(stdlib only, nothing from site-packages), as:

    pythonw -I -S bee_win.py PORT TOKEN_FILE HIVELANE HOME

It draws the logo bee (frontend/public/favicon.svg) in the brand palette that
suits the taskbar, light or dark. When all is clear it is just the bee; an
amber or red dot in the corner, and a face to match, say what needs a look.
The wings beat like the web bee's while the room is connected (faster when
amber or red) and fold when the daemon is away. Each frame is drawn once and
cached.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import math
import os
import shutil
import socket
import sys
import time
from typing import Any, Optional

w = ctypes.wintypes

POLL_MS = 2000
GIVE_UP_SECONDS = 300
FRAMES = 12
# Milliseconds per animation frame, by state; absent = wings at rest. The
# web bee's pace: a calm flap when clear, a fast buzz when something is up.
FRAME_MS = {"live": 90, "connecting": 140, "warn": 45, "block": 40}
# The state badge in the icon's corner; none when the room is clear.
TONE = {
    "warn": (0xF5, 0xA5, 0x24),
    "block": (0xE5, 0x48, 0x4D),
    "error": (0xE5, 0x48, 0x4D),
    "connecting": (0x8A, 0x8D, 0x93),
}
# The brand palettes (frontend/src/brand/mascot.ts), picked to suit the
# taskbar: the light one on a light taskbar, the dark one on a dark taskbar.
LIGHT = {
    "ink": (0x18, 0x18, 0x1B),
    "body": (0xFF, 0xFF, 0xFF),
    "accent": (0x00, 0x62, 0xD1),
    "wing": (0xEE, 0xF1, 0xF5),
}
DARK = {"ink": (0xF5, 0xF5, 0xF7), "body": (0x14, 0x14, 0x16), "accent": (0x3B, 0x95, 0xFF), "wing": (0x20, 0x20, 0x22)}
LAYERS = ("wing", "ink", "body", "accent", "badge")

# --- drawing: the logo bee (frontend/public/favicon.svg), supersampled --------------
#
# Coordinates are the favicon's 160-unit design space, with its thicker
# small-size strokes. Every shape is tested per sample, top layer first.

_HEX = [(80, 52), (118.1, 74), (118.1, 118), (80, 140), (41.9, 118), (41.9, 74)]
_SS = 4  # samples per pixel per axis
_BOX = (4.0, 7.5, 152.0)  # the square of design space the icon shows: x, y, side
_BADGE = (133.0, 134.0, 23.0, 31.0)  # x, y, radius, cut-out radius

Point = tuple[float, float]


def _seg_dist2(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    ex, ey = px - (ax + t * dx), py - (ay + t * dy)
    return ex * ex + ey * ey


def _quad(x0: float, y0: float, cx: float, cy: float, x1: float, y1: float, n: int = 8) -> list[Point]:
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        pts.append((u * u * x0 + 2 * u * t * cx + t * t * x1, u * u * y0 + 2 * u * t * cy + t * t * y1))
    return pts


def _cubic(p0: Point, p1: Point, p2: Point, p3: Point, n: int = 10) -> list[Point]:
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return pts


class _Stroke:
    """A polyline drawn with round caps and joins, `width` units wide."""

    def __init__(self, pts: list[Point], width: float) -> None:
        self.segs = [(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) for i in range(len(pts) - 1)]
        self.r2 = (width / 2) ** 2
        h = width / 2
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.box = (min(xs) - h, min(ys) - h, max(xs) + h, max(ys) + h)

    def hit(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.box
        if x < x0 or x > x1 or y < y0 or y > y1:
            return False
        return any(_seg_dist2(x, y, *s) <= self.r2 for s in self.segs)


def _line(x0: float, y0: float, x1: float, y1: float, width: float) -> _Stroke:
    return _Stroke([(x0, y0), (x1, y1)], width)


_ANTENNAE = [
    _Stroke(_cubic((72, 57), (69, 44), (62, 37), (56, 30)), 8.5),
    _Stroke(_cubic((88, 57), (91, 44), (98, 37), (104, 30)), 8.5),
]
_TIPS = [(55.0, 28.0), (105.0, 28.0)]
_HEX_EDGE = _Stroke([*_HEX, _HEX[0]], 10.2)

# Faces, after the avatar states in mascot.ts: (ink strokes, ink dots, highlight dots).
Face = tuple[list[_Stroke], list[tuple[float, float, float]], list[tuple[float, float, float]]]
_HAPPY: Face = (
    [
        _Stroke(_quad(60, 84, 66, 76, 72, 84), 8.5),
        _Stroke(_quad(88, 84, 94, 76, 100, 84), 8.5),
        _Stroke(_quad(73, 93, 80, 99, 87, 93), 6.8),
    ],
    [],
    [],
)
_FACES: dict[str, Face] = {
    "live": _HAPPY,
    "connecting": _HAPPY,
    # Wide eyes, a small frown: someone is nearby.
    "warn": (
        [_Stroke(_quad(75, 96, 80, 92, 85, 96), 6.8)],
        [(66, 83, 7), (94, 83, 7)],
        [(68.2, 80.6, 2.2), (96.2, 80.6, 2.2)],
    ),
    # Brows down, mouth flat: an edit is held.
    "block": (
        [_line(58, 71, 72, 75, 7.6), _line(102, 71, 88, 75, 7.6), _line(73, 95, 87, 95, 6.8)],
        [(66, 83, 6), (94, 83, 6)],
        [(68.2, 80.6, 2.2), (96.2, 80.6, 2.2)],
    ),
    # Eyes shut: nobody home.
    "off": ([_line(59, 84, 73, 84, 8.5), _line(87, 84, 101, 84, 8.5), _line(75, 95, 85, 95, 6.8)], [], []),
}
_FACES["error"] = _FACES["block"]

Wing = tuple[float, float, float, float, float, float, float, float]


def _in_hex(px: float, py: float) -> bool:
    n = len(_HEX)
    for i in range(n):
        ax, ay = _HEX[i]
        bx, by = _HEX[(i + 1) % n]
        if (bx - ax) * (py - ay) - (by - ay) * (px - ax) < 0:
            return False
    return True


def flap(frame: int) -> float:
    """0 (wings open) to 1 (wings beaten down), eased; frame -1 is at rest."""
    return 0.0 if frame < 0 else (1 - math.cos(2 * math.pi * frame / FRAMES)) / 2


def _wings(t: float, fold: float) -> list[Wing]:
    """Per wing: its centre x, the root it beats about (x, y), cos and sin of
    the beat, the squash, and cos and sin of its own tilt. The beat is the
    web bee's: up to 14 degrees about the root, squashed to 55% (bee-flap in
    index.css)."""
    out = []
    for cx, root_x, sign, tilt in ((34.0, 53.0, -1, -32), (126.0, 107.0, 1, 32)):
        beat = math.radians(sign * 14 * t)
        e = math.radians(tilt)
        out.append((cx, root_x, math.cos(beat), math.sin(beat), (1 - 0.45 * t) * fold, math.cos(e), math.sin(e), fold))
    return out


def _sample(x: float, y: float, state: str, wings: list[Wing]) -> int:
    """The layer (an index into LAYERS) at a design-space point, or -1."""
    if state in TONE:
        bx, by, r, cut = _BADGE
        d2 = (x - bx) ** 2 + (y - by) ** 2
        if d2 <= r * r:
            return 4
        if d2 <= cut * cut:
            return -1  # a clear ring that sets the badge off from the bee
    strokes, dots, lights = _FACES.get(state, _HAPPY)
    for cx, cy, r in lights:
        if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
            return 2
    for cx, cy, r in dots:
        if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
            return 1
    if _HEX_EDGE.hit(x, y) or any(s.hit(x, y) for s in strokes):
        return 1
    if _in_hex(x, y):
        if 103 <= y <= 114:
            return 3
        if 119 <= y <= 127:
            return 1
        return 2
    if any(a.hit(x, y) for a in _ANTENNAE) or any((x - tx) ** 2 + (y - ty) ** 2 <= 36 for tx, ty in _TIPS):
        return 1
    for cx, root_x, cb, sb, squash, ce, se, fold in wings:
        # Undo the beat (rotation about the root, then the squash), then
        # test the wing's own tilted ellipse.
        dx, dy = x - root_x, y - 80.3
        qx, qy = (dx * cb + dy * sb) / fold, (-dx * sb + dy * cb) / squash
        ex, ey = qx + root_x - cx, qy + 80.3 - 68
        u, v = ex * ce + ey * se, -ex * se + ey * ce
        if (u / 31.25) ** 2 + (v / 16.25) ** 2 <= 1:
            return 0 if (u / 22.75) ** 2 + (v / 7.75) ** 2 <= 1 else 1
    return -1


def coverage(size: int, frame: int, state: str = "live") -> list[tuple[int, ...]]:
    """Per pixel, how many of its samples land on each of LAYERS. Colours
    are applied later, so one map serves both taskbar themes."""
    x0, y0, side = _BOX
    k = size / side
    wings = _wings(flap(frame), 0.8 if state == "off" else 1.0)  # grounded: folded, as on the web
    out: list[tuple[int, ...]] = []
    step = 1 / _SS
    for py_ in range(size):
        ys = [y0 + (py_ + (sy + 0.5) * step) / k for sy in range(_SS)]
        for px_ in range(size):
            counts = [0, 0, 0, 0, 0]
            for y in ys:
                for sx in range(_SS):
                    layer = _sample(x0 + (px_ + (sx + 0.5) * step) / k, y, state, wings)
                    if layer >= 0:
                        counts[layer] += 1
            out.append(tuple(counts))
    return out


def pixels(cov: list[tuple[int, ...]], state: str, palette: dict[str, tuple[int, int, int]]) -> bytes:
    """Top-down BGRA (straight alpha) for CreateIconIndirect."""
    total = _SS * _SS
    colours = [palette["wing"], palette["ink"], palette["body"], palette["accent"], TONE.get(state, (0, 0, 0))]
    fade = 0.55 if state == "off" else 1.0  # greyed out while the daemon is away
    buf = bytearray(len(cov) * 4)
    for i, counts in enumerate(cov):
        hit = sum(counts)
        if not hit:
            continue
        r = sum(c[0] * n for c, n in zip(colours, counts)) // hit
        g = sum(c[1] * n for c, n in zip(colours, counts)) // hit
        b = sum(c[2] * n for c, n in zip(colours, counts)) // hit
        buf[i * 4 : i * 4 + 4] = bytes((b, g, r, int(hit * 255 * fade) // total))
    return bytes(buf)


def menu_lines(tip: str, width: int = 64, most: int = 5) -> list[str]:
    """The status at the top of the bee's menu: the tip wrapped to menu
    width, at most `most` lines. "&" is doubled: menus read a single one as
    a keyboard shortcut marker."""
    import textwrap

    lines: list[str] = []
    for part in tip.split("\n"):
        lines.extend(textwrap.wrap(part, width))
    if len(lines) > most:
        lines = [*lines[: most - 1], lines[most - 1][: width - 1] + "\u2026"]
    return [line.replace("&", "&&") for line in lines] or ["Hivelane"]


def light_taskbar() -> bool:
    """Whether the taskbar is light. Windows 10 and 11 ship with it dark."""
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:  # type: ignore[attr-defined,unused-ignore]
            return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])  # type: ignore[attr-defined,unused-ignore]
    except (OSError, ImportError):
        return False


# --- Win32 ---------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no branch - this file only runs on Windows
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
# WINFUNCTYPE (stdcall) exists only on Windows; elsewhere this module is
# imported just for its drawing code (the tests), so any type will do.
_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
WNDPROC = _FUNCTYPE(LRESULT, w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM)

WM_DESTROY, WM_COMMAND, WM_TIMER, WM_APP = 0x0002, 0x0111, 0x0113, 0x8000
WM_SETTINGCHANGE = 0x001A
WM_LBUTTONUP, WM_RBUTTONUP, WM_NULL = 0x0202, 0x0205, 0x0000
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_WARNING = 0x2
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
ID_OPEN, ID_HIDE, ID_COPY = 1, 2, 3
CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
TIMER_POLL, TIMER_FRAME = 1, 2
CALLBACK = WM_APP + 1


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("hWnd", w.HWND),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", w.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", w.DWORD),
        ("dwStateMask", w.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", w.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", w.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HICON),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD),
        ("biWidth", w.LONG),
        ("biHeight", w.LONG),
        ("biPlanes", w.WORD),
        ("biBitCount", w.WORD),
        ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD),
        ("biXPelsPerMeter", w.LONG),
        ("biYPelsPerMeter", w.LONG),
        ("biClrUsed", w.DWORD),
        ("biClrImportant", w.DWORD),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", w.BOOL),
        ("xHotspot", w.DWORD),
        ("yHotspot", w.DWORD),
        ("hbmMask", w.HBITMAP),
        ("hbmColor", w.HBITMAP),
    ]


def _bind() -> None:
    user32.DefWindowProcW.argtypes = [w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.argtypes = [
        w.DWORD,
        w.LPCWSTR,
        w.LPCWSTR,
        w.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        w.HWND,
        w.HMENU,
        w.HINSTANCE,
        w.LPVOID,
    ]
    user32.CreateWindowExW.restype = w.HWND
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, ctypes.c_uint, w.LPVOID]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [w.HWND, ctypes.c_size_t]
    user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, ctypes.c_uint, ctypes.c_uint]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.CreatePopupMenu.restype = w.HMENU
    user32.AppendMenuW.argtypes = [w.HMENU, ctypes.c_uint, ctypes.c_size_t, w.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [
        w.HMENU,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        w.HWND,
        w.LPVOID,
    ]
    user32.DestroyMenu.argtypes = [w.HMENU]
    user32.SetForegroundWindow.argtypes = [w.HWND]
    user32.PostMessageW.argtypes = [w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
    user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
    user32.CreateIconIndirect.restype = w.HICON
    user32.DestroyIcon.argtypes = [w.HICON]
    user32.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
    user32.DestroyWindow.argtypes = [w.HWND]
    gdi32.CreateDIBSection.argtypes = [
        w.HDC,
        ctypes.POINTER(BITMAPINFOHEADER),
        ctypes.c_uint,
        ctypes.POINTER(w.LPVOID),
        w.HANDLE,
        w.DWORD,
    ]
    gdi32.CreateDIBSection.restype = w.HBITMAP
    gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, w.LPVOID]
    gdi32.CreateBitmap.restype = w.HBITMAP
    gdi32.DeleteObject.argtypes = [w.HANDLE]
    shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.ShellExecuteW.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_int]
    shell32.ShellExecuteW.restype = w.HINSTANCE
    kernel32.GetModuleHandleW.restype = w.HMODULE
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = w.HGLOBAL
    kernel32.GlobalLock.argtypes = [w.HGLOBAL]
    kernel32.GlobalLock.restype = w.LPVOID
    kernel32.GlobalUnlock.argtypes = [w.HGLOBAL]
    kernel32.GlobalFree.argtypes = [w.HGLOBAL]
    user32.OpenClipboard.argtypes = [w.HWND]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, w.HANDLE]
    user32.SetClipboardData.restype = w.HANDLE


def make_icon(size: int, data: bytes) -> int:
    hdr = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = w.LPVOID()
    color = gdi32.CreateDIBSection(None, ctypes.byref(hdr), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, data, len(data))
    mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    info = ICONINFO(True, 0, 0, mask, color)
    icon = user32.CreateIconIndirect(ctypes.byref(info))
    gdi32.DeleteObject(color)
    gdi32.DeleteObject(mask)
    return icon


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("lpReserved", w.LPCWSTR),
        ("lpDesktop", w.LPCWSTR),
        ("lpTitle", w.LPCWSTR),
        ("dwX", w.DWORD),
        ("dwY", w.DWORD),
        ("dwXSize", w.DWORD),
        ("dwYSize", w.DWORD),
        ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD),
        ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD),
        ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD),
        ("lpReserved2", ctypes.c_char_p),
        ("hStdInput", w.HANDLE),
        ("hStdOutput", w.HANDLE),
        ("hStdError", w.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", w.HANDLE),
        ("hThread", w.HANDLE),
        ("dwProcessId", w.DWORD),
        ("dwThreadId", w.DWORD),
    ]


def _find_chrome() -> Optional[str]:
    candidates = [
        shutil.which("chrome"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def _open_url(url: str) -> None:
    """Open a URL in Google Chrome (if found) or default browser on the interactive desktop."""
    chrome = _find_chrome()
    if chrome:
        cmd = f'"{chrome}" "{url}"'
    else:
        cmd = f'rundll32.exe url.dll,FileProtocolHandler "{url}"'

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.lpDesktop = "WinSta0\\Default"
    pi = PROCESS_INFORMATION()
    if kernel32.CreateProcessW(None, cmd, None, None, False, 0, None, None, ctypes.byref(si), ctypes.byref(pi)):
        kernel32.CloseHandle(pi.hProcess)
        kernel32.CloseHandle(pi.hThread)
        return
    try:
        os.startfile(url)
        return
    except (OSError, AttributeError):
        # Best-effort launch: fall back to ShellExecute if os.startfile fails.
        pass
    shell32.ShellExecuteW(None, "open", url, None, None, 1)


class Bee:
    def __init__(self, port: int, token_file: str, hivelane: str, home: str) -> None:
        self.port, self.token_file, self.hivelane, self.home = port, token_file, hivelane, home
        self.view: dict[str, Any] = {
            "state": "off",
            "label": "",
            "tip": "Hivelane is starting",
            "root": None,
            "room": None,
            "url": None,
        }
        self.fails_since: float | None = None
        self.frame = 0
        self.frame_ms = 0
        self.covers: dict[tuple[str, float], list[tuple[int, ...]]] = {}
        self.icons: dict[tuple[str, float], int] = {}
        self.palette = LIGHT if light_taskbar() else DARK
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            user32.SetProcessDPIAware()
        self.size = user32.GetSystemMetrics(49) or 16  # SM_CXSMICON, at this display's scale
        # Set before the window exists: it gets messages while being created.
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._proc = WNDPROC(self._wndproc)  # kept alive: the window calls back into it
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName="HivelaneBee")
        user32.RegisterClassW(ctypes.byref(cls))
        self.hwnd = user32.CreateWindowExW(0, "HivelaneBee", "Hivelane bee", 0, 0, 0, 0, 0, None, None, hinst, None)
        self.nid = NOTIFYICONDATAW()
        self.nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self.nid.hWnd = self.hwnd
        self.nid.uID = 1
        self.nid.uCallbackMessage = CALLBACK
        self._add()
        user32.SetTimer(self.hwnd, TIMER_POLL, POLL_MS, None)
        self._poll()

    # icons

    def _icon(self, state: str, frame: int) -> int:
        # The beat is a cosine, so frames come in mirrored pairs: key by pose.
        key = (state, round(flap(frame), 3))
        if key not in self.icons:
            if key not in self.covers:
                self.covers[key] = coverage(self.size, frame, state)
            self.icons[key] = make_icon(self.size, pixels(self.covers[key], state, self.palette))
        return self.icons[key]

    def _retheme(self) -> None:
        """Redraw in the other palette when the taskbar turns light or dark."""
        palette = LIGHT if light_taskbar() else DARK
        if palette is self.palette:
            return
        self.palette = palette
        old, self.icons = self.icons, {}
        self._show(self.frame if FRAME_MS.get(self.view["state"]) else -1)
        for icon in old.values():
            user32.DestroyIcon(icon)

    def _add(self) -> None:
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self.nid.hIcon = self._icon(self.view["state"], -1)
        self.nid.szTip = "Hivelane"
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid))

    def _show(self, frame: int) -> None:
        self.nid.uFlags = NIF_ICON
        self.nid.hIcon = self._icon(self.view["state"], frame)
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def _animate(self) -> None:
        ms = FRAME_MS.get(self.view["state"], 0)
        if ms != self.frame_ms:
            user32.KillTimer(self.hwnd, TIMER_FRAME)
            if ms:
                user32.SetTimer(self.hwnd, TIMER_FRAME, ms, None)
            else:
                self._show(-1)
            self.frame_ms = ms

    # daemon

    def _fetch(self) -> dict[str, Any]:
        with open(self.token_file, encoding="utf-8") as fh:
            token = fh.read().strip()
        with socket.create_connection(("127.0.0.1", self.port), timeout=1) as sock:
            sock.sendall(
                f"GET /bee HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\nX-Hivelane-Token: {token}\r\n\r\n".encode()
            )
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
        if not head.startswith(b"HTTP/1.") or head.split(b" ", 2)[1] != b"200":
            raise OSError("daemon answered " + head[:40].decode("latin-1"))
        return json.loads(body)

    def _poll(self) -> None:
        try:
            nxt = self._fetch()
            self.fails_since = None
        except (OSError, ValueError, IndexError):
            now = time.monotonic()
            self.fails_since = self.fails_since or now
            if now - self.fails_since > GIVE_UP_SECONDS:
                user32.DestroyWindow(self.hwnd)
                return
            nxt = {"state": "off", "label": "", "tip": "Hivelane is not running", "root": None}
        old = self.view
        if (nxt.get("state"), nxt.get("label"), nxt.get("tip")) == (old["state"], old["label"], old["tip"]):
            self.view.update(root=nxt.get("root"), room=nxt.get("room"), url=nxt.get("url"))
            return
        worse = nxt.get("state") in ("warn", "block") and nxt.get("state") != old["state"]
        self.view = {
            "state": str(nxt.get("state") or "off"),
            "label": str(nxt.get("label") or ""),
            "tip": str(nxt.get("tip") or ""),
            "root": nxt.get("root"),
            "room": nxt.get("room"),
            "url": nxt.get("url"),
        }
        first = self.view["tip"].split("\n")[0]
        text = f"{first} · {self.view['label']}" if self.view["label"] else first
        self.nid.uFlags = NIF_TIP
        self.nid.szTip = text[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))
        self._show(self.frame if FRAME_MS.get(self.view["state"]) else -1)
        self._animate()
        if worse:  # a quiet heads-up when things turn amber or red; nothing for green
            self.nid.uFlags = NIF_INFO
            self.nid.szInfoTitle = "Hivelane"
            self.nid.szInfo = self.view["tip"].replace("\n", " · ")[:255]
            self.nid.dwInfoFlags = NIIF_WARNING
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    # actions

    def _open(self) -> None:
        url = self.view.get("url")
        room = self.view.get("room")
        if not url or url.startswith("http://localhost") or url.startswith("http://127.0.0.1"):
            if room:
                url = f"https://adrianshah.github.io/Hivelane/r/{room}"
            else:
                url = "https://adrianshah.github.io/Hivelane"
        _open_url(url)

    def _copy(self, text: str) -> None:
        """Put text on the clipboard (CF_UNICODETEXT)."""
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, size)
        kernel32.GlobalUnlock(handle)
        if not user32.OpenClipboard(self.hwnd):
            kernel32.GlobalFree(handle)
            return
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)  # the clipboard owns it only on success
        user32.CloseClipboard()

    def _hide(self) -> None:
        with open(os.path.join(self.home, "bee-hidden"), "a", encoding="utf-8"):
            pass
        user32.DestroyWindow(self.hwnd)

    def _menu(self) -> None:
        """The bee's menu, on either click: the room's whole status, then
        what you can do about it."""
        menu = user32.CreatePopupMenu()
        for line in menu_lines(self.view["tip"]):
            user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, line)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        can_open = bool(self.view.get("url") or (self.hivelane and self.view.get("root")))
        user32.AppendMenuW(menu, MF_STRING | (0 if can_open else MF_GRAYED), ID_OPEN, "Open the dashboard")
        room = self.view.get("room")
        copy = f"Copy room code {room}" if room else "Copy room code"
        user32.AppendMenuW(menu, MF_STRING | (0 if room else MF_GRAYED), ID_COPY, copy)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_HIDE, "Hide the bee (hivelane bee on brings it back)")
        pt = w.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)  # so the menu closes on an outside click
        choice = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if choice == ID_OPEN:
            self._open()
        elif choice == ID_COPY and room:
            self._copy(str(room))
        elif choice == ID_HIDE:
            self._hide()

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_TIMER:
            if wparam == TIMER_POLL:
                self._poll()
            elif wparam == TIMER_FRAME:
                self.frame = (self.frame + 1) % FRAMES
                self._show(self.frame)
            return 0
        if msg == CALLBACK:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONUP, WM_RBUTTONUP):
                self._menu()
            return 0
        if msg == self.taskbar_created:  # Explorer restarted: put the icon back
            self._add()
            return 0
        if msg == WM_SETTINGCHANGE:  # light/dark mode may have changed
            self._retheme()
        if msg == WM_DESTROY:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            for icon in self.icons.values():
                user32.DestroyIcon(icon)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> None:
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def _attach_desktop() -> None:
    """If started from a virtual or sandbox desktop (e.g. IDE agents, exebox),
    switch this thread to the interactive 'Default' desktop so the system tray
    icon attaches to explorer.exe's taskbar."""
    try:
        user32.OpenDesktopW.argtypes = [w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD]
        user32.OpenDesktopW.restype = w.HANDLE
        user32.SetThreadDesktop.argtypes = [w.HANDLE]
        user32.SetThreadDesktop.restype = w.BOOL
        hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)  # GENERIC_ALL
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:
        # Best-effort attachment: if the interactive desktop is unreachable, stay on current.
        pass


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    _attach_desktop()
    _bind()
    Bee(int(argv[0]), argv[1], argv[2], argv[3]).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
