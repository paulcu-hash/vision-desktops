"""Screenshots - 100% vision, no APIs, no DOM, no accessibility tree.

Every agent action is derived from a screenshot.
Screenshots use PrintWindow with PW_RENDERFULLCONTENT: the DWM also renders
invisible virtual desktops, so this works in the background.
Coordinates from screenshots are used 1:1 as click/type positions.
"""

import ctypes
import io
from ctypes import wintypes

# EIGENE WinDLL-Instanzen - Absicht, keine Praezisionerei.
# `ctypes.windll.user32` ist prozessweit gecachtes Singleton: argtypes,
# die wir hier setzen, gelten DANACH fuer JEDE andere Codebasis im selben
# Prozess (andere Module, hinzugeladene Plugins, Test-Harnesses). Deren
# Aufrufe wirfen dann ctypes.ArgumentError ("expected LP_RECT instance
# instead of pointer to RECT"), meist in einem schluckenden try/except ->
# still kaputt statt sichtbar kaputt. Eigene Instanz = eigene argtypes.
_user32 = ctypes.WinDLL("user32")
_gdi32 = ctypes.WinDLL("gdi32")

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002


from .windows import RECT  # noqa: E402  (one RECT type for the whole package!)


_user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
_user32.GetDC.argtypes = [ctypes.c_void_p]
_user32.GetDC.restype = ctypes.c_void_p
_user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
_user32.PrintWindow.restype = wintypes.BOOL
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi32.SelectObject.restype = ctypes.c_void_p
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
_gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                          wintypes.DWORD]
_gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
                                    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                    wintypes.DWORD]
_gdi32.CreateDIBSection.restype = ctypes.c_void_p


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def grab_client(hwnd: int):
    """Render a window's client area. Raises on black capture."""
    hwnd_c = ctypes.c_void_p(hwnd)
    rc = RECT()
    if not _user32.GetClientRect(hwnd_c, ctypes.byref(rc)):
        raise RuntimeError(f"GetClientRect failed for hwnd {hwnd}")
    width, height = rc.right - rc.left, rc.bottom - rc.top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Window {hwnd} has no client area (minimized?)")

    hdc = _user32.GetDC(hwnd_c)
    mem_dc = _gdi32.CreateCompatibleDC(ctypes.c_void_p(hdc))
    bi = _BITMAPINFOHEADER(
        ctypes.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0
    )
    bits = ctypes.c_void_p()
    bmp = _gdi32.CreateDIBSection(
        ctypes.c_void_p(mem_dc), ctypes.byref(bi), 0, ctypes.byref(bits), None, 0
    )
    if not bmp or not bits.value:
        raise RuntimeError("CreateDIBSection failed")
    old = _gdi32.SelectObject(ctypes.c_void_p(mem_dc), ctypes.c_void_p(bmp))
    ok = _user32.PrintWindow(
        hwnd_c, ctypes.c_void_p(mem_dc), PW_CLIENTONLY | PW_RENDERFULLCONTENT
    )
    data = ctypes.string_at(bits.value, width * height * 4) if ok else b""
    _gdi32.SelectObject(ctypes.c_void_p(mem_dc), ctypes.c_void_p(old))
    _gdi32.DeleteObject(ctypes.c_void_p(bmp))
    _gdi32.DeleteDC(ctypes.c_void_p(mem_dc))
    _user32.ReleaseDC(hwnd_c, ctypes.c_void_p(hdc))

    if not ok:
        raise RuntimeError(f"PrintWindow failed for hwnd {hwnd}")
    if data and data.count(0) == len(data):
        raise RuntimeError(
            "Capture completely black - app does not render in the background. "
            "Choose a different program."
        )
    return data, width, height


def to_png(data: bytes, width: int, height: int) -> bytes:
    from PIL import Image

    img = Image.frombuffer("RGBA", (width, height), data, "raw", "BGRA").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def fit_width(width: int, height: int, max_width: int) -> tuple:
    """Target size of the embedded JPEG (aspect ratio preserved)."""
    max_width = max(320, max_width)
    if width > max_width:
        ratio = max_width / float(width)
        return max_width, max(1, int(height * ratio))
    return width, height


def to_jpeg(data: bytes, width: int, height: int, max_width: int = 1280) -> bytes:
    from PIL import Image

    img = Image.frombuffer("RGBA", (width, height), data, "raw", "BGRA").convert("RGB")
    jw, jh = fit_width(width, height, max_width)
    if (jw, jh) != (img.width, img.height):
        img = img.resize((jw, jh))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def crop_bgra(data: bytes, width: int, height: int, region) -> tuple:
    """Client-Aufnahme auf region (x, y, w, h) zuschneiden -> (data, w, h).

    Sparsamkeitsgrund: das Bild wird NUR fuer den Ausschnitt kodiert -
    ein kleiner Ausschnitt kostet deutlich weniger Encode und weniger
    Antwort-Bytes. Rueckgabe weiterhin als BGRA-Bytes, damit alle
    bestehenden Verbraucher (to_jpeg, pixel_diff) unveraendert bleiben."""
    from PIL import Image

    x, y, w, h = (int(v) for v in region)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(int(width), x0 + max(0, w)), min(int(height), y0 + max(0, h))
    if x1 <= x0 or y1 <= y0:
        raise RuntimeError("region lies outside the window")
    img = Image.frombuffer("RGBA", (width, height), data, "raw", "BGRA")
    img = img.crop((x0, y0, x1, y1))
    # PIL speichert RGBA; zurueck in die BGRA-Bytefolge der Verbraucher.
    r, g, b, a = img.split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes(), img.width, img.height


def screen_origin() -> tuple:
    """Virtual screen origin (ox, oy). Fast, no pixels involved."""
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    return (_user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            _user32.GetSystemMetrics(SM_YVIRTUALSCREEN))


def grab_screen():
    """Fullscreen of the VISIBLE desktop via BitBlt (+ virtual origin).

    Generic for any desktop: the caller (desktop hop) first switches
    to the target desktop, THEN reads. Returns (data, w, h, ox, oy):
    screenshot pixel (px, py) = screen coordinate (ox+px, oy+py). The origin
    only matters for multi-monitor setups (negative offset possible).
    """
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    x, y = screen_origin()
    w = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        raise RuntimeError("virtual screen metrics failed")

    screen_dc = _user32.GetDC(None)
    mem_dc = _gdi32.CreateCompatibleDC(ctypes.c_void_p(screen_dc))
    bi = _BITMAPINFOHEADER(
        ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0
    )
    bits = ctypes.c_void_p()
    bmp = _gdi32.CreateDIBSection(
        ctypes.c_void_p(mem_dc), ctypes.byref(bi), 0, ctypes.byref(bits), None, 0
    )
    if not bmp or not bits.value:
        raise RuntimeError("CreateDIBSection failed")
    old = _gdi32.SelectObject(ctypes.c_void_p(mem_dc), ctypes.c_void_p(bmp))
    _gdi32.BitBlt(ctypes.c_void_p(mem_dc), 0, 0, w, h,
                  ctypes.c_void_p(screen_dc), x, y, 0x00CC0020)
    data = ctypes.string_at(bits.value, w * h * 4)
    _gdi32.SelectObject(ctypes.c_void_p(mem_dc), ctypes.c_void_p(old))
    _gdi32.DeleteObject(ctypes.c_void_p(bmp))
    _gdi32.DeleteDC(ctypes.c_void_p(mem_dc))
    _user32.ReleaseDC(None, ctypes.c_void_p(screen_dc))
    if data.count(0) == len(data):
        raise RuntimeError("Screen capture completely black.")
    return data, w, h, x, y


def dpi_scale(hwnd: int) -> float:
    """Screenshot pixels -> client pixels when DPI scaling != 100%."""
    try:
        get_ctx = getattr(_user32, "GetWindowDpiAwarenessContext", None)
        get_aw = getattr(_user32, "GetAwarenessFromDpiAwarenessContext", None)
        get_dpi_win = getattr(_user32, "GetDpiForWindow", None)
        get_dpi_sys = getattr(_user32, "GetDpiForSystem", None)
        if not (get_ctx and get_aw and get_dpi_win and get_dpi_sys):
            return 1.0
        ctx = get_ctx(ctypes.c_void_p(hwnd))
        awareness = get_aw(ctypes.c_void_p(ctx))
        win_dpi = get_dpi_win(ctypes.c_void_p(hwnd))
        if not win_dpi:
            return 1.0
        if awareness == 0:
            return 96.0 / win_dpi
        if awareness == 1:
            sys_dpi = get_dpi_sys()
            return (float(sys_dpi) / win_dpi) if sys_dpi else 1.0
        return 1.0
    except Exception:  # noqa: BLE001
        return 1.0


# --- v2 move chain (ported from the tested chain in vision-TEST, exp37-43) ---
# The leak check is pure vision: two region shots (baseline after cloak,
# poll after the commit re-move) compared per channel byte - same rule as
# the tested numpy diff (|a-b| > 30, count of changed BGRA channel bytes).
SRCCOPY_CAPTUREBLT = 0x40CC0020
LEAK_CHANNEL_DIFF = 30


def grab_region(x: int, y: int, w: int, h: int):
    """Screen region via BitBlt (+ CAPTUREBLT for layered windows).
    Returns (data, w, h) as top-down BGRA like grab_screen."""
    w, h = int(w), int(h)
    if w <= 0 or h <= 0:
        raise RuntimeError("grab_region: empty region")
    screen_dc = _user32.GetDC(None)
    mem_dc = _gdi32.CreateCompatibleDC(ctypes.c_void_p(screen_dc))
    bi = _BITMAPINFOHEADER(
        ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0
    )
    bits = ctypes.c_void_p()
    bmp = _gdi32.CreateDIBSection(
        ctypes.c_void_p(mem_dc), ctypes.byref(bi), 0, ctypes.byref(bits), None, 0
    )
    if not bmp or not bits.value:
        raise RuntimeError("CreateDIBSection failed")
    old = _gdi32.SelectObject(ctypes.c_void_p(mem_dc), ctypes.c_void_p(bmp))
    ok = _gdi32.BitBlt(ctypes.c_void_p(mem_dc), 0, 0, w, h,
                       ctypes.c_void_p(screen_dc), int(x), int(y),
                       SRCCOPY_CAPTUREBLT)
    data = ctypes.string_at(bits.value, w * h * 4) if ok else b""
    _gdi32.SelectObject(ctypes.c_void_p(mem_dc), ctypes.c_void_p(old))
    _gdi32.DeleteObject(ctypes.c_void_p(bmp))
    _gdi32.DeleteDC(ctypes.c_void_p(mem_dc))
    _user32.ReleaseDC(None, ctypes.c_void_p(screen_dc))
    if not ok:
        raise RuntimeError("grab_region BitBlt failed")
    return data, w, h


def clip_region(rc) -> tuple:
    """Window rect (l, t, r, b) clipped to the VIRTUAL screen -> (x, y, w, h).
    None below 50 px (a too-small strip proves nothing - honest skip)."""
    if not rc:
        return None
    ox, oy = screen_origin()
    vw = _user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    vh = _user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
    x0, y0 = max(ox, int(rc[0])), max(oy, int(rc[1]))
    x1 = min(ox + vw, int(rc[2]))
    y1 = min(oy + vh, int(rc[3]))
    if x1 - x0 < 50 or y1 - y0 < 50:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def pixel_diff(a: bytes, b: bytes, width: int, height: int):
    """Changed BGRA channel bytes above LEAK_CHANNEL_DIFF - identical
    semantics to the tested diff in vision-TEST (numpy |a-b| > 30 sum).
    None when the buffers are missing or sizes differ (never a guess)."""
    if not a or not b or len(a) != len(b) or len(a) != width * height * 4:
        return None
    from PIL import Image, ImageChops

    ia = Image.frombuffer("RGBA", (width, height), a, "raw", "BGRA")
    ib = Image.frombuffer("RGBA", (width, height), b, "raw", "BGRA")
    total = 0
    for band in ("R", "G", "B", "A"):
        d = ImageChops.difference(ia.getchannel(band), ib.getchannel(band))
        mask = d.point(lambda p: 255 if p > LEAK_CHANNEL_DIFF else 0)
        total += mask.histogram()[255]
    return int(total)
