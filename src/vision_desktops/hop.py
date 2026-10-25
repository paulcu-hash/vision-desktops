"""Hop input: the compromise from the research plan.

Problem: True parallelism with 2..100 screens is easy (the DWM renders
all desktops, screenshots run in parallel). Truly parallel INPUT is
impossible - there is only one mouse and one keyboard.

Compromise (exactly as requested):
  remember main -> jump to target desktop -> click/type there at leisure
  -> back to main -> next desktop -> back to main -> ...

Everything 100% vision: coordinates (x, y) come from the screenshot
(see `see`). Here they are only converted client->screen and
delivered via real SendInput events. This makes all apps work
(Win32, Electron, Chromium, WinUI) - no PostMessage special path, no
app special cases, no DOM/UIA shortcuts.

Serialization: one global lock so that parallel agent loops do not
get in each other's way while hopping.
"""

import contextlib
import ctypes
import random
import time
from ctypes import wintypes

from . import capture, vdesk

import logging

log = logging.getLogger(__name__)


# EIGENE WinDLL-Instanzen - Absicht, keine Praezisionerei.
# `ctypes.windll.*` ist prozessweit gecachtes Singleton: unsere argtypes
# (unten ~30 Stellen) gaelten sonst auch fuer JEDE andere Codebasis im
# selben Prozess. Kollidiert deren RECT/Struktur mit unseren, wirft der
# andere ctypes.ArgumentError - meist in schluckendem try/except -> still
# kaputt (der argtypes-Clobbering-Bug, in vision-TEST reproduziert).
# WinDLL() = eigene Instanz mit eigenen _FuncPtr: unsere argtypes bleiben
# unsers.
_user32 = ctypes.WinDLL("user32")
_kernel32 = ctypes.WinDLL("kernel32")

# Hop occupancy for the collision clause + interaction guard (v2).
# _HOP_ACTIVE: in-process counter; mutex covers cross-process.
# _HOP_INTERVALS: (tick_start, tick_end) per hop, max 200 - own SendInputs
# are factored out of the LastInput comparison.
_HOP_ACTIVE = [0]
_HOP_INTERVALS: "collections.deque" = __import__("collections").deque(maxlen=200)


def _tick_now() -> int:
    try:
        _kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return int(_kernel32.GetTickCount64())
    except Exception:  # noqa: BLE001
        return int(time.monotonic() * 1000)


def hop_busy() -> bool:
    """Idle? False = hop mutex free (the collision clause additionally needs
    'no open verification step' - tracked by the server)."""
    if _HOP_ACTIVE[0] > 0:
        return True
    try:
        _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        _kernel32.CreateMutexW.restype = ctypes.c_void_p
        _kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        _kernel32.WaitForSingleObject.restype = wintypes.DWORD
        _kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        WAIT_OBJECT_0, WAIT_ABANDONED = 0x0, 0x80
        h = _kernel32.CreateMutexW(None, False, "Local\\VisionDesktopsHop")
        if not h:
            return True
        try:
            r = _kernel32.WaitForSingleObject(h, 0)
            # WICHTIG: WAIT_OBJECT_0 / WAIT_ABANDONED heisst, wir haben den
            # Mutex JETZT ERWORBEN (entweder war er frei, oder der vorherige
            # Owner ist abgestuerzt - beide faellen Ownership an uns).
            # Ein reiner "ist er frei?"-Check darf den Hop-Lock NIEMALS
            # halten: CloseHandle laesst Ownership stehen, bis dieser Thread
            # endet. Ohne ReleaseMutex blockiert dann der naechste echte
            # Hop (_system_lock, 120 s Timeout) bis dahin. Also sofort frei.
            if r in (WAIT_OBJECT_0, WAIT_ABANDONED):
                if not _kernel32.ReleaseMutex(h):
                    log.debug("hop_busy(): ReleaseMutex failed - mutex would "
                              "stay owned by this thread (blocked hop risk)")
            return r not in (WAIT_OBJECT_0, WAIT_ABANDONED)
        finally:
            try:
                _kernel32.CloseHandle(h)
            except Exception:  # noqa: BLE001
                log.debug("swallowed in hop_busy() (was silent pass)", exc_info=True)
    except Exception:  # noqa: BLE001
        return _HOP_ACTIVE[0] > 0


def is_own_input_tick(tick: int) -> bool:
    """Was the LastInput tick inside a hop interval? Then it was the agent."""
    try:
        t = int(tick) & 0xFFFFFFFF
        for a, b in list(_HOP_INTERVALS):
            if (int(a) & 0xFFFFFFFF) <= t <= (int(b) & 0xFFFFFFFF) + 500:
                return True
    except Exception:  # noqa: BLE001
        log.debug("swallowed in is_own_input_tick() (was silent pass)", exc_info=True)
    return False


@contextlib.contextmanager
def _system_lock():
    """System-wide hop lock as a named Win32 mutex (session-local).

    threading.Lock only applies per process - with 3 processes the
    desktop switches steal from each other. The kernel mutex applies session-wide to
    all processes (and threads). If the owner dies, it becomes 'abandoned'
    and the next waiter still gets it - no deadlock from crashes.
    Handle is opened/closed per acquisition: no leak, no stale state.
    """
    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = ctypes.c_void_p
    _kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    WAIT_OBJECT_0, WAIT_ABANDONED = 0x0, 0x80
    h = _kernel32.CreateMutexW(None, False, "Local\\VisionDesktopsHop")
    if not h:
        raise RuntimeError("CreateMutexW failed - no system-wide hop lock.")
    try:
        r = _kernel32.WaitForSingleObject(h, 120_000)
        if r not in (WAIT_OBJECT_0, WAIT_ABANDONED):
            raise RuntimeError(
                "Hop lock blocked for 120s - is a foreign process stuck in a hop?")
        try:
            yield
        finally:
            _kernel32.ReleaseMutex(h)
    finally:
        _kernel32.CloseHandle(h)

_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.POINT)]
_user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(capture.RECT)]
_user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
_user32.SetFocus.argtypes = [ctypes.c_void_p]
_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
_user32.IsIconic.argtypes = [ctypes.c_void_p]
_user32.GetAncestor.argtypes = [ctypes.c_void_p, wintypes.UINT]
_user32.GetAncestor.restype = ctypes.c_void_p
_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.c_void_p]
_user32.GetGUIThreadInfo.restype = wintypes.BOOL

GA_ROOT = 2


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", ctypes.c_void_p),
        ("hwndFocus", ctypes.c_void_p),
        ("hwndCapture", ctypes.c_void_p),
        ("hwndMenuOwner", ctypes.c_void_p),
        ("hwndMoveSize", ctypes.c_void_p),
        ("hwndCaretMove", ctypes.c_void_p),
        ("rcCaret", capture.RECT),
    ]

SW_RESTORE = 9
HWND_TOP = 0
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
_user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _IU(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _IU)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.OpenClipboard.argtypes = [ctypes.c_void_p]
_user32.CloseClipboard.argtypes = []
_user32.EmptyClipboard.argtypes = []
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
_user32.SetClipboardData.restype = ctypes.c_void_p
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Short = real typing, long = clipboard+paste. Observed: <=11 chars OK,
# 39+ chars garble WinUI Notepad (async TSF layout, queue fills up,
# show_main steals focus while events are still pending). Paste is one
# event instead of thousands -> one layout pass, nothing hangs.
_CHAR_MODE_MAX = 20

_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "meta": 0x5B,
}
for _i in range(1, 25):
    _VK[f"f{_i}"] = 0x6F + _i


def client_to_screen(hwnd: int, px: int, py: int):
    """Screenshot pixels (see `see`) -> absolute screen coordinates."""
    rc = capture.RECT()
    if not _user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(rc)):
        raise RuntimeError(f"GetClientRect failed for hwnd {hwnd}")
    w, h = rc.right - rc.left, rc.bottom - rc.top
    scale = capture.dpi_scale(hwnd)
    cx = max(0, min(w - 1, int(round(px * scale))))
    cy = max(0, min(h - 1, int(round(py * scale))))
    pt = wintypes.POINT(cx, cy)
    if not _user32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(pt)):
        raise RuntimeError("ClientToScreen failed")
    return pt.x, pt.y


def _fg() -> int:
    try:
        return int(ctypes.c_void_p(_user32.GetForegroundWindow()).value or 0)
    except Exception:  # noqa: BLE001
        return 0


def _foreground(hwnd: int) -> None:
    """Bring the target window to the foreground AND verify.

    Focus guard (v2): only intervene when the foreground was actually lost
    (compare before/after). If the target is already in front,
    no intervention. Otherwise raise Z-order, then foreground via
    thread attach, up to 3 attempts. If everything fails -> error instead
    of blind-clicking (protection for user windows).
    """
    if _user32.IsIconic(ctypes.c_void_p(hwnd)):
        _user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
        time.sleep(0.3)
    if _fg() == hwnd:
        return
    me = _kernel32.GetCurrentThreadId()
    last_fg = 0
    for _ in range(3):
        try:
            _user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOP),
                                 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        except Exception:  # noqa: BLE001
            log.debug("swallowed in _foreground() (was silent pass)", exc_info=True)
        time.sleep(0.2)
        fg = _fg()
        last_fg = fg
        if fg == hwnd:
            return
        tgt_thread = _user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), None)
        fg_thread = _user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None) if fg else 0
        attached = []
        try:
            if fg_thread and fg_thread != me and _user32.AttachThreadInput(me, fg_thread, True):
                attached.append((me, fg_thread))
            if tgt_thread and tgt_thread != me and _user32.AttachThreadInput(me, tgt_thread, True):
                attached.append((me, tgt_thread))
            _user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
            _user32.SetFocus(ctypes.c_void_p(hwnd))
            time.sleep(0.3)
        finally:
            for a, b in attached:
                _user32.AttachThreadInput(a, b, False)
        if _fg() == hwnd:
            return
    raise RuntimeError(
        f"Window {hwnd} did not come to the foreground (current: {last_fg}, "
        f"visible desktop: {vdesk.current_id()[:8]}) - aborted to protect foreign "
        "windows. Try again later."
    )


def _await_focus(hwnd: int, timeout: float = 2.0) -> None:
    """Caret rule, server side: only type once keyboard focus has arrived
    in the window.

    Foreground window != focus-in-field: after launch/focus change the
    app needs a moment to place focus into the input field. Typing early
    loses the first characters (classic: first character missing).
    The focus handle of the window thread is checked (a child of the window
    counts - the editor is a child). Timeout -> error instead of blind typing,
    same philosophy as _foreground.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            tid = _user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), None)
            if tid:
                gui = GUITHREADINFO()
                gui.cbSize = ctypes.sizeof(GUITHREADINFO)
                if _user32.GetGUIThreadInfo(tid, ctypes.byref(gui)) and gui.hwndFocus:
                    focus = gui.hwndFocus or 0
                    root = _user32.GetAncestor(ctypes.c_void_p(focus), GA_ROOT)
                    if focus == hwnd or (root is not None and int(root) == hwnd):
                        time.sleep(0.2)  # one beat of quiet after focus arrival
                        return
        except Exception:  # noqa: BLE001
            log.debug("swallowed in _await_focus() (was silent pass)", exc_info=True)
        time.sleep(0.1)
    raise RuntimeError(
        f"No keyboard focus in window {hwnd} within {timeout:.0f}s "
        f"(caret rule) - did not type, to avoid losing characters."
    )


def _send(inp: INPUT) -> None:
    arr = (INPUT * 1)(inp)
    if _user32.SendInput(1, arr, ctypes.sizeof(INPUT)) != 1:
        raise RuntimeError("SendInput failed")


def _mouse_click(sx: int, sy: int, button: str, double: bool) -> None:
    _user32.SetCursorPos(sx, sy)
    time.sleep(0.08)
    down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    if button == "right":
        down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    clicks = 2 if double else 1
    for _ in range(clicks):
        _send(INPUT(type=INPUT_MOUSE, u=_IU(mi=MOUSEINPUT(0, 0, 0, down, 0, None))))
        time.sleep(0.03)
        _send(INPUT(type=INPUT_MOUSE, u=_IU(mi=MOUSEINPUT(0, 0, 0, up, 0, None))))
        time.sleep(0.05)


def _mouse_wheel(lines: float) -> None:
    delta = int(lines * 120)
    mi = MOUSEINPUT(0, 0, delta & 0xFFFFFFFF, 0x0800, 0, None)
    _send(INPUT(type=INPUT_MOUSE, u=_IU(mi=mi)))


def _type_unicode(text: str) -> None:
    for ch in text:
        if ch == "\n":
            _vk_press(0x0D)
            continue
        if ch == "\t":
            _vk_press(0x09)
            continue
        if ord(ch) < 32:
            continue
        code = ord(ch)
        codes = [code] if code <= 0xFFFF else [
            0xD800 + ((code - 0x10000) >> 10),
            0xDC00 + ((code - 0x10000) & 0x3FF),
        ]
        for s in codes:
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                _send(INPUT(type=INPUT_KEYBOARD, u=_IU(ki=KEYBDINPUT(0, s, flags, 0, None))))
                # Deliberately calm (~25ms effective): fresh windows / TSF pipelines
                # swallow/discard millisecond bursts (hit 3).
                time.sleep(0.02)


def _open_clipboard() -> None:
    for _ in range(5):
        if _user32.OpenClipboard(None):
            return
        time.sleep(0.1)
    raise RuntimeError("Clipboard busy (another program is holding it)")


def clipboard_get() -> str:
    """Read Unicode text from the clipboard ("" when empty)."""
    _open_clipboard()
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = _kernel32.GlobalLock(ctypes.c_void_p(handle))
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(ctypes.c_void_p(handle))
    finally:
        _user32.CloseClipboard()


def clipboard_set(text: str) -> None:
    """Put Unicode text on the clipboard."""
    data = (text + "\x00").encode("utf-16-le")
    _open_clipboard()
    try:
        _user32.EmptyClipboard()
        mem = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not mem:
            raise RuntimeError("GlobalAlloc failed")
        ptr = _kernel32.GlobalLock(ctypes.c_void_p(mem))
        ctypes.memmove(ptr, data, len(data))
        _kernel32.GlobalUnlock(ctypes.c_void_p(mem))
        if not _user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(mem)):
            _kernel32.GlobalFree(ctypes.c_void_p(mem))
            raise RuntimeError("SetClipboardData failed")
        # mem now belongs to the system - do not free it.
    finally:
        _user32.CloseClipboard()


def _type_via_clipboard(text: str) -> None:
    """Long text: clipboard + ctrl+v, then restore the clipboard.

    One paste = one event instead of thousands of single events: the app sees one
    atomic insertion (one layout pass), nothing is left hanging in the queue
    when show_main restores focus. Deciding what goes where stays
    100% vision (coordinates/focus from `see`) - only the transport changes.
    """
    payload = text.replace("\r\n", "\n").replace("\n", "\r\n")
    try:
        previous = clipboard_get()
        had_previous = True
    except Exception:  # noqa: BLE001
        previous, had_previous = "", False
    clipboard_set(payload)
    try:
        mods, main = parse_combo("ctrl+v")
        _combo(mods, main)
        time.sleep(0.5)  # Paste is 1 event - 0.5s drain is generous.
    finally:
        try:
            if had_previous:
                clipboard_set(previous)
            else:
                _open_clipboard()
                try:
                    _user32.EmptyClipboard()
                finally:
                    _user32.CloseClipboard()
        except Exception:  # noqa: BLE001
            # Restore is a courtesy, no reason to fail.
            log.debug("swallowed in _type_via_clipboard() (clipboard restore)",
                      exc_info=True)


def _vk_press(vk: int) -> None:
    scan = _user32.MapVirtualKeyW(vk, 0)
    _send(INPUT(type=INPUT_KEYBOARD, u=_IU(ki=KEYBDINPUT(vk, scan, 0, 0, None))))
    time.sleep(0.01)
    _send(INPUT(type=INPUT_KEYBOARD, u=_IU(ki=KEYBDINPUT(vk, scan, KEYEVENTF_KEYUP, 0, None))))
    time.sleep(0.01)


def parse_combo(combo: str) -> tuple:
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("Empty key combo")
    vks = []
    for part in parts:
        if part in _VK:
            vks.append(_VK[part])
        elif len(part) == 1:
            vks.append(ord(part.upper()))
        else:
            raise ValueError(f"Unknown key '{part}'")
    return vks[:-1], vks[-1]


def _combo(mods: list, main: int) -> None:
    held = []
    for vk in mods:
        scan = _user32.MapVirtualKeyW(vk, 0)
        _send(INPUT(type=INPUT_KEYBOARD, u=_IU(ki=KEYBDINPUT(vk, scan, 0, 0, None))))
        held.append((vk, scan))
        time.sleep(0.02)
    _vk_press(main)
    for vk, scan in reversed(held):
        _send(INPUT(type=INPUT_KEYBOARD, u=_IU(ki=KEYBDINPUT(vk, scan, KEYEVENTF_KEYUP, 0, None))))
        time.sleep(0.01)


def _settle_after_type(payload: dict) -> None:
    """Give the queue time to drain after typing, BEFORE switching back to
    Main. SendInput is fire-and-forget: on laggy systems late characters
    are still in the queue and would otherwise land in the Main window
    (tail loss) or nowhere. Scales with burst length."""
    time.sleep(max(0.25, 0.03 * len(payload.get("text", ""))))


def do_hop(desktop_id: str, hwnd: int, action: str, payload: dict) -> dict:
    """One hop: remember main -> target -> real input -> back to main.

    Window level: needs a tracked app window + foreground proof.
    For system UI (Start/Run/Tray) there is do_hop_desktop (desktop level).
    The global lock lets any number of desktops/loops work
    interleaved without stealing each other's mouse/keyboard.
    """
    # Antwortform kuerz (Punkt 15): nur {"ok": detail}. "hopped"/"home"
    # waren reine Diagnosefelder (GUID-Abgleich) - kein Konsument liest sie,
    # und jede Hop-Antwort soll unter ca. 100 Zeichen bleiben.
    with desktop_session(desktop_id):
        _foreground(hwnd)
        if action == "type":
            _await_focus(hwnd)  # Caret rule: no typing without focus in the field.
        detail = _perform(hwnd, action, payload, client_coords=True)
        if action == "type":
            _settle_after_type(payload)
        else:
            time.sleep(0.25)
        return {"ok": detail}


def do_hop_desktop(desktop_id: str, action: str, payload: dict) -> dict:
    """Desktop level: like do_hop, but without a window reference - for system UI
    (Start, Run dialog, tray) that is not a tracked app window.

    The guard here is "right desktop + verified arrival" instead of
    "right window in the foreground". Coordinates still come from
    screenshot pixels (desktop_see), inputs run through the same
    real-mode path (_mouse_click/_type_unicode/_combo) - no third path.
    The Main/host desktop is hard-refused (assertion in _enter_desktop).
    """
    # Wie do_hop: Antwort nur {"ok": detail} (Punkt 15, unter ca. 100 Zeichen).
    with desktop_session(desktop_id):
        detail = _perform(0, action, payload, client_coords=False)
        if action == "type":
            _settle_after_type(payload)
        else:
            time.sleep(0.25)
        return {"ok": detail}


@contextlib.contextmanager
def desktop_session(desktop_id: str):
    """Lock + Main guard + switch with arrival poll; finally Main + cursor.

    The only session primitive: do_hop, do_hop_desktop, and desktop_see
    all run through here. Raises BEFORE anything is switched when
    the target is the Main/host desktop. Records hop intervals (ticks)
    so the interaction guard can factor out own SendInputs.
    """
    with _system_lock():
        tick_start = _tick_now()
        _HOP_ACTIVE[0] += 1
        saved_cursor = wintypes.POINT()
        try:
            _user32.GetCursorPos(ctypes.byref(saved_cursor))
            has_cursor = True
        except Exception:  # noqa: BLE001
            has_cursor = False
        home = _enter_desktop(desktop_id)
        try:
            yield home
        finally:
            # Rule: ALWAYS show Main at the end - no matter where we started
            # and no matter what failed. (Best effort, swallows no errors
            # of the actual action.)
            try:
                vdesk.show_main()
                time.sleep(0.12)
            except Exception:  # noqa: BLE001
                log.debug("swallowed in desktop_session() (was silent pass)", exc_info=True)
            if has_cursor:
                try:
                    _user32.SetCursorPos(saved_cursor.x, saved_cursor.y)
                except Exception:  # noqa: BLE001
                    log.debug("swallowed in desktop_session() (was silent pass)", exc_info=True)
            _HOP_ACTIVE[0] = max(0, _HOP_ACTIVE[0] - 1)
            try:
                _HOP_INTERVALS.append((tick_start, _tick_now()))
            except Exception:  # noqa: BLE001
                log.debug("swallowed in desktop_session() (was silent pass)", exc_info=True)


def _enter_desktop(desktop_id: str) -> str:
    """Hard guard + verified switch. Call only under _system_lock.

    1. Assertion: the target must NEVER be the Main/host desktop (lowest
       number). Aborts BEFORE switching - protects the user's visible
       screen.
    2. Arrival poll instead of one-shot check: switch_to may return
       before the DWM has taken over. current_id()==target, every 5ms,
       hard timeout 2s. Up to 3 attempts with jitter: if something
       foreign interferes (finger on Win+Tab, another tool), we do not
       march against it in lockstep but retry with an offset.
    Returns home (starting desktop).
    """
    target = vdesk.norm_guid(desktop_id)
    if target == vdesk.norm_guid(vdesk.main_id()):
        raise RuntimeError(
            "Refused: target is the Main/host desktop. Desktop hops run "
            "only on worker desktops (#2-N) - the visible screen is never touched."
        )
    home = vdesk.current_id()
    if home != target:
        arrived = False
        for _ in range(3):
            vdesk.switch_to(desktop_id)
            for _ in range(400):
                if vdesk.current_id() == target:
                    arrived = True
                    break
                time.sleep(0.005)
            if arrived:
                break
            time.sleep(0.1 + random.random() * 0.3)
        if not arrived:
            raise RuntimeError("Desktop switch did not arrive - hop aborted.")
        time.sleep(0.3)  # let DWM + windows settle
    return home


def _perform(hwnd: int, action: str, payload: dict, client_coords: bool) -> str:
    """Real input via exactly ONE real-mode path.

    client_coords=True: (x, y) are window screenshot pixels -> conversion.
    client_coords=False: (x, y) are desktop screenshot pixels (= screen minus
    virtual origin from grab_screen).
    """
    if action == "click":
        if client_coords:
            sx, sy = client_to_screen(hwnd, payload["x"], payload["y"])
        else:
            ox, oy = payload.get("origin") or capture.screen_origin()
            sx, sy = int(ox) + int(payload["x"]), int(oy) + int(payload["y"])
        _mouse_click(sx, sy, payload.get("button", "left"), payload.get("double", False))
        where = f"{payload['x']},{payload['y']}->screen {sx},{sy}"
        return f"hop-click {where} on hwnd {hwnd}" if hwnd else f"desktop-click {where}"
    elif action == "type":
        txt = payload["text"]
        if len(txt) <= _CHAR_MODE_MAX:
            _type_unicode(txt)
            detail = f"hop-type {len(txt)} chars into hwnd {hwnd}" if hwnd else f"desktop-type {len(txt)} chars"
        else:
            _type_via_clipboard(txt)
            detail = (f"hop-paste {len(txt)} chars via clipboard+ctrl+v into hwnd {hwnd}"
                      if hwnd else f"desktop-paste {len(txt)} chars via clipboard+ctrl+v")
        return detail
    elif action == "key":
        mods, main = parse_combo(payload["keys"])
        _combo(mods, main)
        return f"hop-key '{payload['keys']}' to hwnd {hwnd}" if hwnd else f"desktop-key '{payload['keys']}'"
    elif action == "scroll":
        _mouse_wheel(payload.get("lines", 3))
        return (f"hop-scroll {payload.get('lines', 3):+g} on hwnd {hwnd}" if hwnd
                else f"desktop-scroll {payload.get('lines', 3):+g}")
    else:
        raise ValueError(f"Unknown hop action '{action}'")
