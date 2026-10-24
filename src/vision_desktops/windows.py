"""Generic window management. No app assumptions, no layout guessing.

Windows are located exclusively via hwnd / title_contains.
Coordinates always come from the current screenshot of the same window.
"""

import collections
import ctypes
import queue
import subprocess
import threading
import time
from ctypes import wintypes

import logging

log = logging.getLogger(__name__)


# EIGENE WinDLL-Instanzen - Absicht, keine Praezisionerei.
# `ctypes.windll.user32` ist ein prozessweit gecachtes Singleton. Setzt
# dieses Modul darauf argtypes mit SEINER RECT-Klasse (unten), und ein
# anderes Modul (capture.py, harness, Plugin) mit seiner eigenen, gewinnt
# der Zuletzt-Importierte: der andere wirft beim naechsten Aufruf
# `ctypes.ArgumentError` ("expected LP_RECT instance instead of pointer to
# RECT"), den sein eigener try/except schluckt -> still kaputt (Fenster
# verlieren ihr rect, Aufgaben scheinen ohne Grund zu scheitern).
# `ctypes.WinDLL(...)` erzeugt eine frische Instanz mit eigenen _FuncPtr:
# unsere argtypes hier beruehren niemanden sonst mehr.
_user32 = ctypes.WinDLL("user32")
_kernel32 = ctypes.WinDLL("kernel32")
_dwmapi = ctypes.WinDLL("dwmapi")

_user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p), ctypes.c_void_p]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
_user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
_user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
_user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.IsWindow.argtypes = [ctypes.c_void_p]
_user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.SetWinEventHook.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.UINT]
_user32.SetWinEventHook.restype = ctypes.c_void_p
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT]

SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9
EVENT_OBJECT_SHOW = 0x8002
WINEVENT_OUTOFCONTEXT = 0x0000


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


_user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
_user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]


def pid_of(hwnd: int) -> int:
    """Owning PID of a window. Cheap: one GetWindowThreadProcessId call, no
    message round-trip, never blocks. Use this before any description."""
    try:
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def class_of(hwnd: int) -> str:
    """Window class. Cheap: read from the kernel, no message round-trip."""
    try:
        cls_buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(ctypes.c_void_p(int(hwnd)), cls_buf, 256)
        return cls_buf.value or ""
    except Exception:  # noqa: BLE001
        return ""


def get_title(hwnd: int) -> str:
    """Window title. Message round-trip bounded to ~100 ms (ABORTIFHUNG
    returns immediately on hung threads; busy threads cost the bound).
    Fetch only when the title is needed (delta entries, display) - never
    speculatively: on a busy system, per-window title stalls add up to
    seconds of caller-thread stall and starve the re-queue budget."""
    try:
        _user32.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                                wintypes.WPARAM, wintypes.LPWSTR,
                                                wintypes.UINT, wintypes.UINT,
                                                ctypes.POINTER(wintypes.DWORD)]
        _user32.SendMessageTimeoutW.restype = ctypes.c_void_p
        WM_GETTEXT, SMTO_ABORTIFHUNG = 0x000D, 0x0002
        buf = ctypes.create_unicode_buffer(512)
        _user32.SendMessageTimeoutW(ctypes.c_void_p(int(hwnd)), WM_GETTEXT, 512,
                                    buf, SMTO_ABORTIFHUNG, 100, None)
        return buf.value or ""
    except Exception:  # noqa: BLE001
        return ""


def _describe(h: int):
    """Describe a single hwnd (None if gone). Never hangs: the title
    is fetched via SendMessageTimeout (500 ms, ABORTIFHUNG) instead of blocking
    GetWindowText - half-dead windows would otherwise block the loop forever."""
    try:
        if not _user32.IsWindow(ctypes.c_void_p(h)):
            return None
    except Exception:  # noqa: BLE001
        return None
    title = get_title(h)
    cls = class_of(h)
    pid = pid_of(h)
    rc = RECT()
    rect = None
    try:
        if _user32.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(rc)):
            rect = [rc.left, rc.top, rc.right, rc.bottom]
    except Exception:  # noqa: BLE001
        rect = None
    area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1]) if rect else 0
    return {"hwnd": h, "title": title, "class": cls,
            "pid": pid, "rect": rect, "area": area}


def all_windows(include_tool_windows: bool = False, visible_only: bool = True) -> list:
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lp):
        try:
            h = int(ctypes.c_void_p(hwnd).value or 0)
        except Exception:  # noqa: BLE001
            return True
        if not h:
            return True
        if visible_only and not _user32.IsWindowVisible(ctypes.c_void_p(h)):
            return True
        w = _describe(h)
        if w is None:
            return True
        if not include_tool_windows and w["class"] in ("Shell_TrayWnd", "Windows.UI.Core.CoreWindow"):
            return True
        found.append(w)
        return True

    _user32.EnumWindows(cb, None)
    return found


def all_hwnds() -> set:
    """Collect handles only - never touch windows (never blocks,
    even on half-dead windows). For before-snapshots."""
    found = set()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lp):
        try:
            h = int(ctypes.c_void_p(hwnd).value or 0)
        except Exception:  # noqa: BLE001
            return True
        if h:
            found.add(h)
        return True

    _user32.EnumWindows(cb, None)
    return found


def launch_process(cmdline: str, cwd: str) -> int:
    # Start deliberately VISIBLE: SW_HIDE at launch freezes WinUI windows
    # mid-initialization (no client area, no title, no
    # desktop view - measured). v2: hook is report-only, the decider moves on
    # proven ownership within ms (target <50 ms without collision).
    exe = cmdline.strip().strip('"').split('"')[0].split()[0] if cmdline.strip() else cmdline
    try:
        proc = subprocess.Popen(cmdline, cwd=cwd, shell=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        # Diagnose by errno, not blanket: the PATH hint applies only
        # when the file was truly not found (WinError 2). For
        # 740 (elevation required) the path is correct - the
        # PATH hint would lie there. Generic, no app knowledge.
        code = getattr(exc, "winerror", None) or exc.errno
        if code == 2:
            hint = "Server PATH != user PATH - specify the full exe path."
        elif code == 740:
            hint = ("Path is correct, but the program requires elevated "
                    "rights (WinError 740) - UAC prompts are unreachable "
                    "by OS design; start the server elevated or choose an "
                    "exe without an admin manifest.")
        else:
            hint = "See the original error for details."
        raise RuntimeError(
            f"Failed to start '{exe}': {exc}. {hint}"
        ) from exc
    invalidate_tree_cache()  # new pid: tree membership changed.
    return proc.pid


def tree_pids(root_pids: list) -> set:
    import psutil

    out = set(root_pids)  # keep matching dead roots (stub exits after handoff).
    for pid in root_pids:
        try:
            root = psutil.Process(pid)
        except Exception:  # noqa: BLE001
            continue
        try:
            for child in root.children(recursive=True):
                out.add(child.pid)
        except Exception:  # noqa: BLE001
            log.debug("swallowed in tree_pids() (was silent pass)", exc_info=True)
    return out


# Tree cache: one psutil walk serves all candidates of a tick instead of one
# walk per candidate. TTL-bounded; launches invalidate explicitly (job handles
# are already held per launch, only the tree walk is cached).
_TREE_CACHE: dict = {}  # frozenset(roots) -> (monotonic_ts, set)
_TREE_CACHE_LOCK = threading.Lock()
TREE_CACHE_TTL_S = 0.5


def invalidate_tree_cache() -> None:
    with _TREE_CACHE_LOCK:
        _TREE_CACHE.clear()


def cached_tree_pids(root_pids: list, ttl: float = TREE_CACHE_TTL_S) -> set:
    """Tree membership with TTL cache. Same result as tree_pids for settled
    trees; late-spawned children appear after at most ttl seconds."""
    try:
        key = frozenset(int(p) for p in (root_pids or []))
    except Exception:  # noqa: BLE001
        return tree_pids(root_pids)
    now = time.monotonic()
    with _TREE_CACHE_LOCK:
        hit = _TREE_CACHE.get(key)
        if hit is not None and (now - hit[0]) < max(0.05, float(ttl)):
            return set(hit[1])
    fresh = tree_pids(root_pids)
    with _TREE_CACHE_LOCK:
        _TREE_CACHE[key] = (now, set(fresh))
    return fresh


def tree_contains(root_pids: list, pid: int) -> bool:
    """Is pid in the tree? Cached walk first; on a miss, verify via the cheap
    parent chain (one psutil call) so children spawned after the cache fill
    are not misclassified as foreign."""
    try:
        wpid = int(pid)
        roots = {int(p) for p in (root_pids or [])}
    except Exception:  # noqa: BLE001
        return False
    if not wpid or not roots:
        return False
    if wpid in roots:
        return True
    try:
        if wpid in cached_tree_pids(root_pids):
            return True
    except Exception:  # noqa: BLE001
        log.debug("swallowed in tree_contains() (was silent pass)", exc_info=True)
    try:
        import psutil

        for parent in psutil.Process(wpid).parents():
            if int(parent.pid) in roots:
                return True
    except Exception:  # noqa: BLE001
        log.debug("swallowed in tree_contains() (was silent pass)", exc_info=True)
    return False


def kill_tree(pid: int) -> bool:
    import psutil

    try:
        root = psutil.Process(pid)
    except Exception:  # noqa: BLE001
        return False
    try:
        procs = [root] + root.children(recursive=True)
        for p in procs:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                log.debug("swallowed in kill_tree() (was silent pass)", exc_info=True)
        try:
            _, alive = psutil.wait_procs(procs, timeout=5)
        except Exception:  # noqa: BLE001
            alive = procs
        for p in alive:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                log.debug("swallowed in kill_tree() (was silent pass)", exc_info=True)
        return True
    except Exception:  # noqa: BLE001
        return False


# --- PID-Identitaet (Recycling-Schutz) -----------------------------------
# Windows vergibt PIDs wiederverwendet. Eine gespeicherte PID ist nach einem
# Server-Neustart KEINE stabile Identitaet mehr: dieselbe Nummer kann dann
# einem fremden Programm gehoeren. desktop_remove() wuerde sie sonst toeten
# und _windows_of() waere sie "eigen" zaehlen. Anker ist die create_time.
def pid_ctime(pid: int):
    """Erstellzeit (epoch s) eines LEBDEN Prozesses - oder None (tot/fremd)."""
    import psutil

    try:
        return psutil.Process(int(pid)).create_time()
    except Exception:  # noqa: BLE001
        return None


def pid_live_as(pid, ctime) -> bool:
    """True NUR wenn `pid` lebt UND seine create_time noch gleich `ctime`
    ist - also JENES Prozess, den wir gestartet haben, keine recycelte PID.
    ctime None (Legacy-Eintrag ohne Anker) -> False (fail-closed: unge-
    pruefte PID nie toeten/zaehlen)."""
    if not pid or ctime is None:
        return False
    now = pid_ctime(pid)
    if now is None:
        return False
    try:
        return abs(float(now) - float(ctime)) < 0.001
    except Exception:  # noqa: BLE001
        return False


def live_roots(pids) -> list:
    """Nur die gespeicherten Wurzel-PIDs, die noch DERSELBE Prozess sind.
    Verarbeitet altes Format (bare int) und neues Format
    ({"pid":..,"ctime":..}). Legacy-Eintraege ohne Anker werden
    ausgeschlossen (fail-closed)."""
    out = []
    for entry in (pids or []):
        if isinstance(entry, dict):
            pid = int(entry.get("pid", 0) or 0)
            ctime = entry.get("ctime")
        else:
            try:
                pid = int(entry)
            except Exception:  # noqa: BLE001
                continue
            ctime = None
        if pid and pid_live_as(pid, ctime):
            out.append(pid)
    return out


def is_window(hwnd: int) -> bool:
    """Is this hwnd still alive? Cheap, never blocks."""
    try:
        return bool(_user32.IsWindow(ctypes.c_void_p(int(hwnd))))
    except Exception:  # noqa: BLE001
        return False


def close_window(hwnd: int) -> bool:
    """Close a window gently (WM_CLOSE). Generic, affects only this hwnd -
    important for single-instance apps, where the process may belong to the user
    and must NOT be killed."""
    try:
        WM_CLOSE = 0x10
        _user32.PostMessageW(ctypes.c_void_p(hwnd), WM_CLOSE, 0, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def show_window(hwnd: int) -> None:
    """Make visible on the target desktop (after the move). WARNING: this
    call sends messages and BLOCKS (hundreds of ms, measured 658 ms) while
    the target thread is busy initializing. Never call it on the hot
    decision path - use async_show (fire-and-forget) or restore_if_minimized
    instead. Test/measurement code with no latency budget may call it."""
    try:
        if _user32.IsIconic(ctypes.c_void_p(hwnd)):
            _user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
        else:
            _user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOW)
    except Exception:  # noqa: BLE001
        log.debug("swallowed in show_window() (was silent pass)", exc_info=True)


def restore_if_minimized(hwnd: int) -> None:
    """Restore a minimized window, otherwise do nothing. Cheap and
    non-blocking for visible windows (no messages sent). Use this instead
    of show_window wherever only the minimized state matters."""
    try:
        if _user32.IsIconic(ctypes.c_void_p(hwnd)):
            _user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
    except Exception:  # noqa: BLE001
        log.debug("swallowed in restore_if_minimized() (was silent pass)", exc_info=True)


# Async show worker: a blocking ShowWindow (see above) must never stall the
# serialized decider. Fallback shows go through this fire-and-forget queue;
# the move retry then happens via pending re-queue, not by waiting here.
_SHOW_Q: "queue.Queue" = queue.Queue()
_SHOW_ONCE = threading.Lock()
_SHOW_REQUESTED: set = set()
_SHOW_STARTED = [False]
# Experiment 14.10.2026 (solo run-7 forensics): show dedup starves retries.
SHOW_DEDUP = False


def _ensure_show_worker() -> None:
    with _SHOW_ONCE:
        if _SHOW_STARTED[0]:
            return
        _SHOW_STARTED[0] = True

    def _loop():
        while True:
            try:
                hwnd = _SHOW_Q.get()
            except Exception:  # noqa: BLE001
                continue
            try:
                show_window(int(hwnd))
            except Exception:  # noqa: BLE001
                log.debug("swallowed in _loop() (was silent pass)", exc_info=True)

    threading.Thread(target=_loop, daemon=True, name="vision-show").start()


def async_show(hwnd: int) -> None:
    """Request a fallback show without blocking. Experiment 14.10.2026:
    dedup DISABLED (SHOW_DEDUP=False) - the once-per-hwnd dedup meant a
    single lost show (busy target thread) starved all retries: every later
    async_show was a silent no-op while moves kept failing view-not-ready.
    Shows drive DWM view registration; retries without shows are pointless.
    Worker serializes blocks; moves never wait for shows."""
    try:
        _ensure_show_worker()
        if SHOW_DEDUP:
            with _SHOW_ONCE:
                if len(_SHOW_REQUESTED) > 10000:
                    _SHOW_REQUESTED.clear()
                if int(hwnd) in _SHOW_REQUESTED:
                    return
                _SHOW_REQUESTED.add(int(hwnd))
        _SHOW_Q.put(int(hwnd))
    except Exception:  # noqa: BLE001
        log.debug("swallowed in async_show() (was silent pass)", exc_info=True)


def hide_window(hwnd: int) -> None:
    """Hide a window immediately (SW_HIDE, best effort). ONLY via the
    ownership gate (proven job/tree pid): never hide foreign or unverified
    windows. Errors are fail-open (window simply stays visible)."""
    try:
        _user32.ShowWindow(ctypes.c_void_p(int(hwnd)), SW_HIDE)
    except Exception:  # noqa: BLE001
        log.debug("swallowed in hide_window() (was silent pass)", exc_info=True)


# Async hide worker: mirror of the show worker. Hiding itself is one fast
# message, but the worker keeps the hook/decider path free of any surprise
# and deduplicates repeat requests per hwnd.
_HIDE_Q: "queue.Queue" = queue.Queue()
_HIDE_ONCE = threading.Lock()
_HIDE_REQUESTED: set = set()
_HIDE_STARTED = [False]


def _ensure_hide_worker() -> None:
    with _HIDE_ONCE:
        if _HIDE_STARTED[0]:
            return
        _HIDE_STARTED[0] = True

    def _loop():
        while True:
            try:
                hwnd = _HIDE_Q.get()
            except Exception:  # noqa: BLE001
                continue
            try:
                hide_window(int(hwnd))
            except Exception:  # noqa: BLE001
                log.debug("swallowed in _loop() (was silent pass)", exc_info=True)

    threading.Thread(target=_loop, daemon=True, name="vision-hide").start()


def async_hide(hwnd: int) -> None:
    """Request a hide without blocking. Once per hwnd (re-shows by the app
    are not re-hidden here; the next handling pass decides again)."""
    try:
        _ensure_hide_worker()
        with _HIDE_ONCE:
            if len(_HIDE_REQUESTED) > 10000:
                _HIDE_REQUESTED.clear()
            if int(hwnd) in _HIDE_REQUESTED:
                return
            _HIDE_REQUESTED.add(int(hwnd))
        _HIDE_Q.put(int(hwnd))
    except Exception:  # noqa: BLE001
        log.debug("swallowed in async_hide() (was silent pass)", exc_info=True)


def is_window_visible(hwnd: int) -> bool:
    """Is the window currently visible? Cheap, never blocks."""
    try:
        return bool(_user32.IsWindowVisible(ctypes.c_void_p(int(hwnd))))
    except Exception:  # noqa: BLE001
        return False


def has_client(hwnd: int) -> bool:
    """Does the window have an area to see/click? Sinks, message-only
    windows and empty containers have none - the agent can neither see nor
    operate those, so do not even claim them."""
    return client_area(hwnd) > 0


def client_area(hwnd: int) -> int:
    """Client area in px (cheap Win32 call, no COM, no title round-trip).
    Used only for move ordering (largest first) - no app knowledge."""
    try:
        rc = RECT()
        if not _user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(rc)):
            return 0
        w = rc.right - rc.left
        h = rc.bottom - rc.top
        return w * h if w > 0 and h > 0 else 0
    except Exception:  # noqa: BLE001
        return 0


# --- Event hook (report-only, v2) ------------------------------------
# Every new window is created on the VISIBLE desktop (CreateProcess knows
# no virtual-desktop parameter). The hook reports EVENT_OBJECT_SHOW within
# milliseconds; nothing is hidden anymore (v1 hide removed: no
# window stealing, no flicker from hide/show, no view gaps).
# Deliberately SHOW (not CREATE): SHOW means initialized, view registered.
# Ownership is decided by the decider thread (job hit or process tree),
# not by the hook. No app special-casing, one hook.

_HOOK_LOCK = threading.Lock()
_hook_events: "collections.deque" = collections.deque(maxlen=2000)  # (wall_t, tick, hwnd, pid)
_hook_on = False
_hook_cb = None
_hook_handle = None

# Wake-up signal for the watcher: set on every hook event, so the watcher
# sleeps on the event instead of a tick (250 ms quantization disappears).
# Pure transport - no app knowledge. A wait timeout remains as fallback in
# case a signal is ever lost between scan and clear.
_WAKE = threading.Event()


def wait_for_window_event(timeout: float) -> bool:
    """Sleep until the hook reports a window (True) or timeout (False)."""
    return bool(_WAKE.wait(float(timeout)))


def _wake_clear() -> None:
    _WAKE.clear()

_WinEventProc = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                  ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD)


def _ensure_hook() -> None:
    global _hook_on, _hook_cb
    with _HOOK_LOCK:
        if _hook_on:
            return
        _hook_on = True

    def _on_event(hhook, event, hwnd, id_obj, id_child, tid, ms):
        try:
            if not hwnd or int(id_obj) != 0 or int(id_child) != 0:
                return
            h = int(ctypes.c_void_p(hwnd).value or 0)
            if not h:
                return
            pid = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(pid))
            with _HOOK_LOCK:
                _hook_events.append((time.time(), _tick_now(), h, pid.value))
            _WAKE.set()  # never blocks; hook thread must never die.
        except Exception:  # noqa: BLE001
            # Hook thread must never die.
            log.debug("swallowed in hook _on_event() (hook thread guard)",
                      exc_info=True)

    _hook_cb = _WinEventProc(_on_event)  # Reference must stay alive.

    def _pump():
        hh = _user32.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW,
                                     None, _hook_cb, 0, 0, WINEVENT_OUTOFCONTEXT)
        with _HOOK_LOCK:
            global _hook_handle
            _hook_handle = hh
        if not hh:
            return
        msg = wintypes.MSG()
        while True:  # Message pump: out-of-context hooks would not fire otherwise.
            r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break

    threading.Thread(target=_pump, daemon=True, name="vision-hook").start()


def _tick_now() -> int:
    """ms since boot (GetTickCount64, monotonic). Used by the interaction guard."""
    try:
        _kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return int(_kernel32.GetTickCount64())
    except Exception:  # noqa: BLE001
        return int(time.monotonic() * 1000)


def last_input_tick() -> int:
    """Time of the last user input (tick). Own SendInputs fall inside
    hop-mutex intervals and are factored out by the interaction guard."""

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    try:
        info = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO), 0)
        if _user32.GetLastInputInfo(ctypes.byref(info)):
            return int(info.dwTime & 0xFFFFFFFF)
    except Exception:  # noqa: BLE001
        log.debug("swallowed in last_input_tick() (was silent pass)", exc_info=True)
    return 0


def foreground_hwnd() -> int:
    try:
        _user32.GetForegroundWindow.restype = ctypes.c_void_p
        return int(ctypes.c_void_p(_user32.GetForegroundWindow()).value or 0)
    except Exception:  # noqa: BLE001
        return 0


def hook_events_since(wall_since: float) -> list:
    """Read report-only: [(wall_t, tick, hwnd, pid)]."""
    with _HOOK_LOCK:
        return [e for e in list(_hook_events) if e[0] >= wall_since]


GW_OWNER = 4


def window_owner(hwnd: int) -> int:
    try:
        _user32.GetWindow.argtypes = [ctypes.c_void_p, wintypes.UINT]
        _user32.GetWindow.restype = ctypes.c_void_p
        o = _user32.GetWindow(ctypes.c_void_p(int(hwnd)), GW_OWNER)
        return int(ctypes.c_void_p(o).value or 0)
    except Exception:  # noqa: BLE001
        return 0


def owned_hwnds(owner_hwnd: int) -> list:
    """Owned top-levels without describing (fast, no text round-trip).
    owned_windows() below additionally describes them (for listings)."""
    out = []
    try:
        for h in all_hwnds():
            try:
                if window_owner(h) == int(owner_hwnd):
                    out.append(h)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        log.debug("swallowed in owned_hwnds() (was silent pass)", exc_info=True)
    return out


def owned_windows(owner_hwnd: int) -> list:
    """Top-level windows owned by owner_hwnd (owner co-move)."""
    out = []
    try:
        for w in all_windows(include_tool_windows=True):
            try:
                if window_owner(w["hwnd"]) == int(owner_hwnd):
                    out.append(w["hwnd"])
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        log.debug("swallowed in owned_windows() (was silent pass)", exc_info=True)
    return out


def frame_child_pids(hwnd: int) -> set:
    """Frame-host child mapping (generic, no app name): for frame windows
    (e.g. ApplicationFrameWindow) the PIDs of visible children also count as
    ownership candidates. Normal windows: empty set."""
    try:
        w = _describe(int(hwnd))
        if not w or w.get("class") != "ApplicationFrameWindow":
            return set()
    except Exception:  # noqa: BLE001
        return set()
    pids: set = set()
    try:
        _user32.EnumChildWindows.argtypes = [ctypes.c_void_p, ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p), ctypes.c_void_p]
        cands: list = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _cb(ch, _lp):
            try:
                cands.append(int(ctypes.c_void_p(ch).value or 0))
            except Exception:  # noqa: BLE001
                log.debug("swallowed in _cb() (was silent pass)", exc_info=True)
            return True

        _cb_ref = _cb  # Keep the reference alive during enum.
        _user32.EnumChildWindows(ctypes.c_void_p(int(hwnd)), _cb, None)
        for ch in cands:
            try:
                pid = wintypes.DWORD(0)
                _user32.GetWindowThreadProcessId(ctypes.c_void_p(ch), ctypes.byref(pid))
                if pid.value:
                    pids.add(int(pid.value))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        log.debug("swallowed in frame_child_pids() (was silent pass)", exc_info=True)
    return pids


# --- Suspend-start with job object (v2, with tree_pids fallback) -------------
# Proven ownership means: job hit OR process tree. The job covers direct
# children; broker/store/explorer launches and nested-job environments fall
# back to tree_pids (no app special-casing).

_JOB_HANDLES: dict = {}  # job_key -> handle (kernel handle, process lifetime)
_JOB_LOCK = threading.Lock()


def job_create(key: str = ""):
    """Create a job object (one handle per launch group). Nested jobs: if
    create/assign fails later -> fallback to tree_pids, no error."""
    try:
        k32 = _kernel32
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        h = k32.CreateJobObjectW(None, None)
        if not h:
            return None
        if key:
            with _JOB_LOCK:
                _JOB_HANDLES[key] = h
        return h
    except Exception:  # noqa: BLE001
        return None


def job_assign(job, pid: int) -> bool:
    """Attach a process to a job. Requires PROCESS_SET_QUOTA|TERMINATE."""
    try:
        k32 = _kernel32
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        hproc = k32.OpenProcess(0x0100 | 0x0001, False, int(pid))
        if not hproc:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(hproc)))
        finally:
            try:
                k32.CloseHandle(ctypes.c_void_p(hproc))
            except Exception:  # noqa: BLE001
                log.debug("swallowed in job_assign() (was silent pass)", exc_info=True)
    except Exception:  # noqa: BLE001
        return False


def job_contains(job, pid: int) -> bool:
    """Job hit? Foreign-host candidates never hit (no window stealing)."""
    if not job:
        return False
    try:
        k32 = _kernel32
        k32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.BOOL)]
        k32.IsProcessInJob.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = ctypes.c_void_p
        hproc = k32.OpenProcess(0x0400, False, int(pid))  # QUERY_INFORMATION
        if not hproc:
            return False
        try:
            inside = wintypes.BOOL(False)
            ok = k32.IsProcessInJob(ctypes.c_void_p(hproc), ctypes.c_void_p(job),
                                    ctypes.byref(inside))
            return bool(ok and inside.value)
        finally:
            try:
                k32.CloseHandle(ctypes.c_void_p(hproc))
            except Exception:  # noqa: BLE001
                log.debug("swallowed in job_contains() (was silent pass)", exc_info=True)
    except Exception:  # noqa: BLE001
        return False


def launch_in_job(cmdline: str, cwd: str, job_key: str = "") -> dict:
    """Suspend-start with job object, tree_pids fallback.

    Attempts: start CREATE_SUSPENDED -> assign job -> resume. Broker,
    store, and explorer launches as well as nested-job environments fail at assign
    -> fallback: normal start, ownership via tree_pids only.
    Returns {"pid": int, "job": handle|None, "used_job": bool}."""
    job = job_create(job_key)
    if job:
        try:
            import subprocess as _sp  # only the fallback path uses Popen
            _ = _sp
        except Exception:  # noqa: BLE001
            log.debug("swallowed in launch_in_job() (was silent pass)", exc_info=True)
        try:
            k32 = _kernel32
            CREATE_SUSPENDED = 0x00000004
            CREATE_NO_WINDOW = 0x08000000

            class STARTUPINFOW(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                            ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                            ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p),
                            ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p)]

            class PROCESS_INFORMATION(ctypes.Structure):
                _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                            ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

            k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                           ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.LPCWSTR,
                                           ctypes.POINTER(STARTUPINFOW),
                                           ctypes.POINTER(PROCESS_INFORMATION)]
            k32.CreateProcessW.restype = wintypes.BOOL
            k32.ResumeThread.argtypes = [ctypes.c_void_p]
            k32.ResumeThread.restype = wintypes.DWORD
            si = STARTUPINFOW()
            si.cb = ctypes.sizeof(STARTUPINFOW)
            pi = PROCESS_INFORMATION()
            # CreateProcessW expects args as mutable LPWSTR; cmdline as exe+args.
            buf = ctypes.create_unicode_buffer(cmdline)
            ok = k32.CreateProcessW(None, buf, None, None, False,
                                    CREATE_SUSPENDED | CREATE_NO_WINDOW,
                                    None, cwd, ctypes.byref(si), ctypes.byref(pi))
            if ok and pi.dwProcessId:
                assigned = job_assign(int(job), int(pi.dwProcessId))
                try:
                    k32.ResumeThread(ctypes.c_void_p(pi.hThread))
                except Exception:  # noqa: BLE001
                    log.debug("swallowed in launch_in_job() (was silent pass)", exc_info=True)
                try:
                    k32.CloseHandle(ctypes.c_void_p(pi.hThread))
                    k32.CloseHandle(ctypes.c_void_p(pi.hProcess))
                except Exception:  # noqa: BLE001
                    log.debug("swallowed in launch_in_job() (was silent pass)", exc_info=True)
                invalidate_tree_cache()  # new pid: tree membership changed.
                return {"pid": int(pi.dwProcessId), "job": job if assigned else None,
                        "used_job": bool(assigned)}
        except Exception:  # noqa: BLE001
            log.debug("swallowed in launch_in_job() (was silent pass)", exc_info=True)
    # Fallback: normal visible start (broker/store/explorer/nested-job).
    pid = launch_process(cmdline, cwd=cwd)
    invalidate_tree_cache()  # new pid: tree membership changed.
    return {"pid": pid, "job": job, "used_job": False}


LAUNCH_WINDOW_S = 5.0  # Upper bound (v2 spec)
LAUNCH_REST_S = 0.5  # Quiet period: no new hook window after first find


def wait_new_windows(before: set, root_pids: list, timeout: float,
                     job=None) -> dict:
    """Collect new windows side-effect-free (measurement/test helper, not prod).

    Like the prod path (report-only hook, 5-s upper bound + quiet period), but WITHOUT hide,
    show, or move - only observe + determine provenance. Returns
    {"windows": [...], "handoff": bool, "provenance": {hwnd: "job"|"tree"|"foreign"}}.
    Proven = job OR tree; remainder = foreign (handoff).
    """
    _ensure_hook()
    entry = time.time()
    cap = min(max(2.0, float(timeout)), LAUNCH_WINDOW_S)
    deadline = entry + cap
    tree_hits: list = []
    held: list = []
    retry: dict = {}
    first_found = None
    last_new = entry
    provenance: dict = {}
    seeded = False
    while time.time() < deadline:
        now = time.time()
        pids = tree_pids(root_pids)
        known = before | {w["hwnd"] for w in tree_hits} | {w["hwnd"] for w in held}
        cands: list = []
        if not seeded:
            seeded = True
            for h in all_hwnds() - before:
                if h not in known:
                    known.add(h)
                    cands.append(h)
        for (wt, _tick, h, _pid) in hook_events_since(entry - 3.0):
            if h not in known:
                known.add(h)
                cands.append(h)
        for h in cands:
            w = _describe(h)
            if w is None:
                if h not in retry:
                    retry[h] = now
                continue
            retry.pop(h, None)
            if (w.get("class") or "") == "IME":
                continue  # System sink like tray (not a user window).
            # Provenance: job hit OR tree = proven; remainder = foreign.
            prov = "foreign"
            try:
                if job is not None and job_contains(job, w["pid"]):
                    prov = "job"
                elif w["pid"] in pids:
                    prov = "tree"
                else:
                    # Frame-host mapping: child PIDs count too (generic).
                    try:
                        kids = frame_child_pids(h)
                        if kids and (kids & pids):
                            prov = "tree"
                    except Exception:  # noqa: BLE001
                        log.debug("swallowed in wait_new_windows() (was silent pass)", exc_info=True)
            except Exception:  # noqa: BLE001
                prov = "foreign"
            provenance[h] = prov
            if prov in ("job", "tree"):
                tree_hits.append(w)
                if first_found is None:
                    first_found = now
                last_new = now
            elif not any(x["hwnd"] == h for x in held):
                held.append(w)
                last_new = now
        for w in held:
            if w["pid"] in pids and not any(x["hwnd"] == w["hwnd"] for x in tree_hits):
                tree_hits.append(w)
                provenance[w["hwnd"]] = "tree"
                if first_found is None:
                    first_found = time.time()
        held = [w for w in held if not any(x["hwnd"] == w["hwnd"] for x in tree_hits)]
        for h, t0 in list(retry.items()):
            if now - t0 > 5.0:
                del retry[h]
                continue
            w = _describe(h)
            if w is None:
                continue
            del retry[h]
            prov = "job" if (job is not None and job_contains(job, w["pid"])) \
                else ("tree" if w["pid"] in tree_pids(root_pids) else "foreign")
            provenance[h] = prov
            if prov in ("job", "tree"):
                tree_hits.append(w)
                if first_found is None:
                    first_found = now
                last_new = now
            elif not any(x["hwnd"] == h for x in held):
                held.append(w)
                last_new = now
        if first_found is not None and (now - last_new) >= LAUNCH_REST_S:
            break
        time.sleep(0.05)
    if tree_hits:
        # Foreign windows are NOT shown/hidden (report-only) - they stay on Main.
        return {"windows": tree_hits, "handoff": False, "provenance": provenance}
    fresh = []
    for w in held:
        d = _describe(w["hwnd"])
        if d and d["title"].strip():
            fresh.append(w)
    return {"windows": fresh, "handoff": len(fresh) > 0, "provenance": provenance}


# --- v2 move chain helpers (ported 1:1 from the tested chain, vision-TEST) ---
DWMWA_CLOAKED = 14  # 1 app / 2 shell / 4 inherited

_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = ctypes.c_void_p
_user32.GetAncestor.argtypes = [ctypes.c_void_p, wintypes.UINT]
_user32.GetAncestor.restype = ctypes.c_void_p
_dwmapi.DwmGetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                          ctypes.c_void_p, ctypes.c_uint]
_dwmapi.DwmGetWindowAttribute.restype = ctypes.c_int


def dwm_cloaked(hwnd: int) -> int:
    """DWMWA_CLOAKED bitmask, -1 on error. Bit 2 (shell) = the window is not
    rendered on the current desktop - the tested chain's visibility oracle."""
    v = wintypes.DWORD(0)
    try:
        hr = _dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(int(hwnd)),
                                           DWMWA_CLOAKED, ctypes.byref(v),
                                           ctypes.sizeof(v))
    except Exception:  # noqa: BLE001
        return -1
    return int(v.value) if hr == 0 else -1


def dwm_hidden(hwnd: int) -> bool:
    """True when the window is not rendered here (shell-cloaked: on another
    desktop, or cloaked by the chain) - same rule as the tested `hidden()`:
    DWMWA_CLOAKED & 2."""
    v = dwm_cloaked(hwnd)
    return v >= 0 and bool(v & 2)


def win_rect(hwnd: int):
    """[l, t, r, b] or None - cheap GetWindowRect with the shared RECT."""
    rc = RECT()
    try:
        if _user32.GetWindowRect(ctypes.c_void_p(int(hwnd)), ctypes.byref(rc)):
            return [rc.left, rc.top, rc.right, rc.bottom]
    except Exception:  # noqa: BLE001
        return None
    return None


def point_is_ours(hwnd: int, x: int, y: int) -> bool:
    """Topmost window at (x, y) is hwnd or its root child - the tested
    chain's occlusion probe for the baseline shot (GA_ROOT compare)."""
    try:
        w = _user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not w:
            return False
        return int(_user32.GetAncestor(ctypes.c_void_p(w), 2) or 0) == int(hwnd)
    except Exception:  # noqa: BLE001
        return False
