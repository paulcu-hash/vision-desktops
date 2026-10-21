"""Windows virtual desktops via pyvda (COM apartment thread).

IMPORTANT - do not confuse:
- We use the modern virtual-desktop system (Win+Tab level),
  addressed via COM: IVirtualDesktopManagerInternal.
  Encapsulated in code via the Python library `pyvda`.
  These desktops are permanently rendered by the DWM -> screenshots
  also work while invisible (PrintWindow).
- This does NOT mean the Win32 API `CreateDesktop`.
  That creates a raw desktop object not rendered by the DWM
  (no thumbnail, no PrintWindow, separate logon-session nuances).
  Unusable for our purpose.

All pyvda calls run on exactly one COM apartment thread,
because COM otherwise fails with RPC_E_WRONG_THREAD.
"""

import ctypes
import queue
import threading
import time

from . import capture, windows

COINIT_APARTMENTTHREADED = 0x2


class _ComWorker:
    """COM apartment thread with priority (v2): hop/switch (prio 0) before moves
    (prio 1). Three threads: hook only reports, decider queues moves, COM
    executes - hop calls always take precedence over moves."""

    def __init__(self):
        self._queue: "queue.PriorityQueue" = queue.PriorityQueue()
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._thread = None
        self._lock = threading.Lock()

    def _run(self):
        try:
            ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        except OSError:
            pass
        import pyvda  # noqa: F401 (load inside the COM apartment)

        while True:
            _prio, _seq, fn, result_q = self._queue.get()
            try:
                result_q.put(("ok", fn()))
            except BaseException as exc:  # noqa: BLE001
                result_q.put(("err", exc))

    def call(self, fn, timeout: float = 60.0, priority: int = 0):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name="vision-desktops-com"
                )
                self._thread.start()
        result_q: "queue.Queue" = queue.Queue()
        with self._counter_lock:
            self._counter += 1
            seq = self._counter
        self._queue.put((int(priority), seq, fn, result_q))
        status, value = result_q.get(timeout=timeout)
        if status == "err":
            raise RuntimeError(f"{type(value).__name__}: {value}") from value
        return value


_COM = _ComWorker()


def call(fn, priority: int = 0):
    """Run a function on the COM apartment thread (sole access to pyvda).
    priority 0 = hop/switch (precedence), 1 = auto-move."""
    return _COM.call(fn, priority=priority)


def norm_guid(value) -> str:
    return str(value).strip("{}").lower()


def list_desktops() -> list:
    def run():
        import pyvda

        out = []
        for d in pyvda.get_virtual_desktops():
            try:
                entry = {"number": d.number, "id": norm_guid(d.id)}
            except Exception:
                continue  # deleted concurrently - skip.
            try:
                entry["name"] = d.name
            except Exception:
                entry["name"] = ""
            out.append(entry)
        return out

    return _COM.call(run)


def create_desktop(label: str) -> dict:
    def run():
        import pyvda

        vd = pyvda.VirtualDesktop.create()
        try:
            vd.rename(f"vision-{label}")
        except Exception:  # noqa: BLE001
            pass
        return {"id": norm_guid(vd.id), "number": vd.number}

    return _COM.call(run)


def _resolved(desktop_id: str):
    import pyvda

    target = norm_guid(desktop_id)
    for d in pyvda.get_virtual_desktops():
        if norm_guid(d.id) == target:
            return d
    raise LookupError(f"Virtual desktop {desktop_id} no longer exists")


# ------------------------------------------------------------------ v2 chain
# Ported 1:1 from the tested chain (vision-TEST exp37/38/39b, quotes:
# 50/50 runs exp39c + 5/5 native app exp43, remmoves=1 everywhere):
#   cloak(1,1) -> move -> SetCloak(1,0)+(2,0) -> IMMEDIATE re-move (commit
#   trigger) -> vision leak check (baseline vs poll shots) -> desktop
#   verification (up to 3 re-move retries).
# Safety rule from the tests, refined by the port probe (exp46): the
# emergency uncloak (_cloak_off) is mandatory up to AND including the
# clears step, and FORBIDDEN afterwards - the window can already be parked
# on the target there, and SetCloak(*,0) would strip the DWM's own shell
# cloak and re-expose it on Main (probe: attr 2 -> 0 while parked). The
# guarded phase lives inside _chain(); callers never touch the cloak.
CLOAK_POLL_BUDGET = 1.2      # tested: 240 x 5 ms + one nudge (unc/cloak)
CLOAK_POLL_DT = 0.005
CLOAK_POLL_FAST = 0.25       # no-pixel path: short cap, fail-open to naked
PRE_CLEAR_S = 0.05           # tested cadence before the uncloak
BASELINE_S = 0.25            # settle before the baseline shot (B)
LEAK_POLL_S = 0.2            # pause after each commit re-move
LEAK_RETRY_S = 0.15
DESK_RETRY_S = 0.3
LEAK_MAX_ROUNDS = 3
DESK_MAX_TRIES = 3
LEAK_SAME_T = 20000          # tested leak threshold (changed channel bytes)

# Evidence of the LAST chain run (cloak/leak/desk outcome) - diagnostics
# for callers and tests only, never a decision input itself.
LAST_MOVE_INFO: dict = {}


def _raw_view(av):
    """pyvda 0.6.0 has no cloak API; the raw IApplicationView pointer does
    (SetCloak - validated empirically, same interface the tests drove via
    GetViewForHwnd). None when unavailable -> chain runs fail-open."""
    return getattr(av, "_view", None)


def _poll_hidden(hwnd: int, budget: float) -> bool:
    """DWM visibility oracle, same rule as the tested hidden(): attr & 2."""
    t0 = time.time()
    while time.time() - t0 < budget:
        if windows.dwm_hidden(hwnd):
            return True
        time.sleep(CLOAK_POLL_DT)
    return windows.dwm_hidden(hwnd)


def _cloak_on(av, hwnd: int, budget: float) -> bool:
    """Tested cloak: SetCloak(1,1) + attr poll + one nudge (uncloak 0.1 s,
    cloak again). Observed in the port probe (exp46): via the pyvda view
    pointer the call can RAISE although the cloak applied - the POLL decides,
    never the exception. Fail-open: False means the chain continues
    uncloaked (today's behavior) - never a stuck-invisible window."""
    raw = _raw_view(av)
    if raw is None:
        return False
    try:
        raw.SetCloak(1, 1)
    except Exception:  # noqa: BLE001 (may still have applied - poll decides)
        pass
    if _poll_hidden(hwnd, budget):
        return True
    try:
        raw.SetCloak(1, 0)
        time.sleep(0.1)
        raw.SetCloak(1, 1)
    except Exception:  # noqa: BLE001
        pass
    return _poll_hidden(hwnd, budget)


def _cloak_off(av) -> None:
    """clears() from the tests: SetCloak(1,0) + SetCloak(2,0).

    Two call sites only, both inside the guarded phase of _chain(): the
    tested clears step (immediately before the commit re-move) and the
    emergency path while our cloak may still be live (pre-parking). NEVER
    after the window reached the target: clearing then strips the DWM's
    own shell cloak of a parked window and re-exposes it on Main."""
    raw = _raw_view(av)
    if raw is None:
        return
    for bit in (1, 2):
        try:
            raw.SetCloak(bit, 0)
        except Exception:  # noqa: BLE001
            pass


def _note_view_error(hwnd: int, exc: BaseException) -> None:
    """Wrapper-level failure (no AppView yet): keep LAST_MOVE_INFO
    truthful instead of leaving the previous run's numbers in place."""
    LAST_MOVE_INFO.clear()
    LAST_MOVE_INFO.update(
        {"hwnd": int(hwnd), "cloak": None, "pixel": "skipped",
         "leak_px": None, "rounds": 0, "desk": None,
         "err": f"view:{type(exc).__name__}"[:120], "outcome": "error"})


def _top(hwnd: int, on: bool) -> None:
    """Occlusion probe from the tests: briefly topmost, so a leak behind
    other windows would show up in the poll shot (then back)."""
    try:
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(int(hwnd)),
            ctypes.c_void_p(-1 if on else -2),
            0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    except Exception:  # noqa: BLE001
        pass


def _shot(region):
    """(data, w, h) of the screen region - None on failure (honest skip,
    the chain then makes no pixel claim)."""
    try:
        return capture.grab_region(*region)
    except Exception:  # noqa: BLE001
        return None


def _diff(a, b, w, h):
    """Tested leak metric (changed channel bytes) or None."""
    try:
        return capture.pixel_diff(a, b, w, h)
    except Exception:  # noqa: BLE001
        return None


def _chain(av, hwnd: int, desktop_id: str, check_pin: bool, pixel: bool) -> str:
    """The tested v2 sequence. Returns the historic string contract:
    "moved" | "already" | "pin" | "error:<why>". Runs ON the COM thread
    (raw view pointer), pixel steps are GDI - same apartment, no re-entry
    into _COM.call (that would deadlock the single worker)."""
    hwnd = int(hwnd)
    info = {"hwnd": hwnd, "cloak": None, "pixel": "skipped", "leak_px": None,
            "rounds": 0, "desk": None, "err": None}

    def done(outcome: str) -> str:
        LAST_MOVE_INFO.clear()
        LAST_MOVE_INFO.update(info)
        LAST_MOVE_INFO["outcome"] = outcome
        return outcome

    LAST_MOVE_INFO.clear()
    LAST_MOVE_INFO.update(info)

    if check_pin:
        try:
            if av.is_pinned() or av.is_app_pinned():
                return done("pin")
        except Exception:  # noqa: BLE001
            pass
    target = norm_guid(desktop_id)
    try:
        on_target = norm_guid(av.desktop_id) == target
    except Exception:  # noqa: BLE001
        on_target = False
    if on_target:
        # Genuinely done = invisible here (or minimized in place). The
        # opposite (COM says target, window still visible on Main) is the
        # tested silent-move lie and falls through to the commit repair.
        try:
            parked = ((not windows.is_window_visible(hwnd))
                      or windows.dwm_hidden(hwnd)
                      or bool(ctypes.windll.user32.IsIconic(
                          ctypes.c_void_p(hwnd))))
        except Exception:  # noqa: BLE001
            parked = True
        if parked:
            info["desk"] = True
            return done("already")
    try:
        vd = _resolved(desktop_id)
    except Exception as exc:  # noqa: BLE001
        info["err"] = type(exc).__name__
        return done(f"error:{type(exc).__name__}")

    # --- baseline region: only a window visible on Main can leak on Main ---
    region = None
    a_data = None
    occl_wfp = False
    if pixel:
        try:
            import pyvda

            dlist = pyvda.get_virtual_desktops()
            main = norm_guid(min(dlist, key=lambda d: d.number).id)
            on_main = norm_guid(pyvda.VirtualDesktop.current().id) == main
        except Exception:  # noqa: BLE001
            on_main = False
        if (on_main and windows.is_window_visible(hwnd)
                and not windows.dwm_hidden(hwnd)):
            try:
                region = capture.clip_region(windows.win_rect(hwnd))
            except Exception:  # noqa: BLE001
                region = None
        info["pixel"] = "pending" if region else "skipped"
    if region:
        shot = _shot(region)
        a_data = shot[0] if shot else None
        x, y, w, h = region
        occl_wfp = not windows.point_is_ours(hwnd, x + w // 2, y + h // 2)

    # --- cloak .. clears: guarded phase (safety invariant) ---
    # Until the clears step our SetCloak(1,1) may still be live: every exit
    # here (move error, unexpected exception) MUST restore visibility.
    # After the clears the window may already be PARKED on the target -
    # SetCloak(*,0) then strips the DWM's own shell cloak and re-exposes the
    # window on Main (exp46 probe: attr went 2 -> 0 while parked) - so from
    # that point on the chain never touches the cloak again.
    cloak_live = [False]
    try:
        try:
            need_cloak = (windows.is_window_visible(hwnd)
                          and not windows.dwm_hidden(hwnd))
        except Exception:  # noqa: BLE001
            need_cloak = True
        cloaked = False
        if need_cloak:
            cloak_live[0] = True  # attempt made - it may have applied
            cloaked = _cloak_on(av, hwnd,
                                CLOAK_POLL_BUDGET if region
                                else CLOAK_POLL_FAST)
        info["cloak"] = cloaked
        if region and need_cloak and not cloaked:
            # Fail-open cloak: the window is still visible, so a baseline shot
            # would show the window itself and every poll would scream "leak"
            # (false verdict). Honest: no pixel claim, commit + desk active.
            info["pixel"] = "cloak-failed"
            region = None

        b_data = None
        occluded = False
        if region:
            time.sleep(BASELINE_S)
            shot = _shot(region)
            b_data = shot[0] if shot else None
            if b_data and a_data:
                d_a = _diff(a_data, b_data, shot[1], shot[2])
                occluded = occl_wfp or (d_a is not None and d_a < LEAK_SAME_T)
            else:
                occluded = occl_wfp

        # --- move #1 (tested: while cloaked) ---
        try:
            av.move(vd)
        except Exception as exc:  # noqa: BLE001
            info["err"] = type(exc).__name__
            return done(f"error:{type(exc).__name__}")
        time.sleep(PRE_CLEAR_S)
        _cloak_off(av)  # clears(): uncloak BEFORE the commit re-move
        cloak_live[0] = False  # never touch the cloak again from here
    finally:
        if cloak_live[0]:
            _cloak_off(av)  # restore pre-move visibility (pre-parking only)

    # --- commit + vision leak check (tested rounds) / plain commit ---
    cleared = None  # None = no pixel claim (mechanical path / shot trouble)
    rounds_run = False
    if region and b_data is not None:
        cleared = False
        rounds_run = True
        for rnd in range(LEAK_MAX_ROUNDS):
            try:
                av.move(vd)  # commit trigger (round 0) / repair re-move
            except Exception as exc:  # noqa: BLE001
                info["err"] = f"commit:{type(exc).__name__}"
            time.sleep(LEAK_POLL_S)
            f_shot = _shot(region)
            if not f_shot:
                cleared = None
                info["pixel"] = "shot-failed"
                break
            f_data, fw, fh = f_shot
            leak = _diff(f_data, b_data, fw, fh)
            info["rounds"] = rnd + 1
            info["leak_px"] = leak
            if leak is not None and leak < LEAK_SAME_T:
                if occluded:
                    _top(hwnd, True)
                    time.sleep(0.25)
                    f2_shot = _shot(region)
                    _top(hwnd, False)
                    if not f2_shot:
                        cleared = None
                        info["pixel"] = "shot-failed"
                        break
                    leak2 = _diff(f2_shot[0], b_data, f2_shot[1], f2_shot[2])
                    info["leak_px"] = leak2
                    if leak2 is not None and leak2 < LEAK_SAME_T:
                        cleared = True
                        break
                else:
                    cleared = True
                    break
            if rnd < LEAK_MAX_ROUNDS - 1:
                time.sleep(LEAK_RETRY_S)
    if not rounds_run:
        # No pixel claim (mechanical path, or baseline shot failed) - the
        # COMMIT re-move still runs unconditionally: it is the mechanism.
        if region:
            info["pixel"] = "shot-failed"
        try:
            av.move(vd)  # commit trigger, tested cadence (immediately after)
        except Exception as exc:  # noqa: BLE001
            info["err"] = f"commit:{type(exc).__name__}"

    # --- desktop verification (tested: up to 3, re-move per retry) ---
    desk_ok = False
    for _ in range(DESK_MAX_TRIES):
        try:
            desk_ok = norm_guid(av.desktop_id) == target
        except Exception:  # noqa: BLE001
            desk_ok = False
        if desk_ok:
            break
        try:
            av.move(vd)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(DESK_RETRY_S)
    info["desk"] = desk_ok
    if region:
        info["pixel"] = "ok" if cleared else (
            "leak" if cleared is False else info["pixel"])
    if not desk_ok:
        info["err"] = info["err"] or "desk-mismatch"
        return done("error:desk")
    if cleared is False:
        info["err"] = "leak-persisted"
        return done("error:leak")
    return done("moved")


def move_window(hwnd: int, desktop_id: str, priority: int = 1,
                pixel: bool = True) -> str:
    """Move a window to the target desktop via the tested v2 chain
    (cloak -> move -> uncloak + immediate commit re-move -> vision leak
    check -> desktop verification). NO pin guard - like the old
    move_window this parks explicitly approved windows (graveyard/tests);
    pins are only guarded in move_if_unpinned. priority 1 = auto-move (hop
    takes precedence). Raises on failure (historic contract)."""
    def run():
        import pyvda

        try:
            av = pyvda.AppView(hwnd=int(hwnd))
        except Exception as exc:  # noqa: BLE001
            _note_view_error(hwnd, exc)
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
        # The chain owns the safety invariant (guarded cloak phase), so no
        # extra cloak handling here - parked windows stay shell-cloaked.
        outcome = _chain(av, hwnd, desktop_id, check_pin=False,
                         pixel=pixel)
        if outcome not in ("moved", "already"):
            raise RuntimeError(outcome)
        return outcome

    return _COM.call(run, priority=priority)


def is_view_pinned(hwnd: int) -> bool:
    """Pin check: pinned windows are never moved (v2 acceptance)."""
    def run():
        import pyvda

        try:
            return bool(pyvda.AppView(hwnd=int(hwnd)).is_pinned())
        except Exception:  # noqa: BLE001
            return False

    try:
        return bool(_COM.call(run, priority=0))
    except Exception:  # noqa: BLE001
        return False


def is_app_pinned(hwnd: int) -> bool:
    """Pin check: pinned apps are never moved (v2 acceptance)."""
    def run():
        import pyvda

        try:
            return bool(pyvda.AppView(hwnd=int(hwnd)).is_app_pinned())
        except Exception:  # noqa: BLE001
            return False

    try:
        return bool(_COM.call(run, priority=0))
    except Exception:  # noqa: BLE001
        return False


def pin_status(hwnd: int) -> tuple:
    """Both pin flags in ONE COM round-trip (latency: pyvda calls cost
    ~100-300 ms each here, two separate checks would double the price)."""
    def run():
        import pyvda

        try:
            view = pyvda.AppView(hwnd=int(hwnd))
            return (bool(view.is_pinned()), bool(view.is_app_pinned()))
        except Exception:  # noqa: BLE001
            return (False, False)

    try:
        return tuple(_COM.call(run, priority=0))
    except Exception:  # noqa: BLE001
        return (False, False)


def move_if_unpinned(hwnd: int, desktop_id: str, priority: int = 1,
                     pixel: bool = False) -> str:
    """Pin check + tested v2 move chain in ONE COM call. Historic contract:
    "moved" | "already" (parked at target, no move burned) | "pin" |
    "error:..." - callers keep working unchanged.

    Chain: cloak (fail-open) -> move -> uncloak + immediate commit re-move
    -> desktop verification; pixel=True additionally runs the tested vision
    leak check (baseline after cloak vs poll shots, occlusion probe) - only
    when Main is current and the window is visible here, otherwise the
    pixel step is skipped honestly (reported in LAST_MOVE_INFO). priority 0
    = explicit (adoption), 1 = auto-move (hop takes precedence)."""
    def run():
        import pyvda

        try:
            av = pyvda.AppView(hwnd=int(hwnd))
        except Exception as exc:  # noqa: BLE001
            _note_view_error(hwnd, exc)
            return f"error:{type(exc).__name__}"
        # The chain owns the safety invariant (guarded cloak phase).
        return _chain(av, hwnd, desktop_id, check_pin=True, pixel=pixel)

    try:
        return str(_COM.call(run, priority=int(priority)))
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}"


def switch_to(desktop_id: str) -> None:
    def run():
        vd = _resolved(desktop_id)
        vd.go(allow_set_foreground=True)

    _COM.call(run)


def current_id() -> str:
    def run():
        import pyvda

        return norm_guid(pyvda.VirtualDesktop.current().id)

    return _COM.call(run)


def main_id() -> str:
    """ID of the Main desktop (lowest number, usually desktop #1)."""
    desks = list_desktops()
    if not desks:
        raise RuntimeError("No desktops found")
    return min(desks, key=lambda d: d["number"])["id"]


def show_main() -> None:
    """Visibly switch back to the Main desktop (best effort, never raises)."""
    try:
        switch_to(main_id())
    except Exception:  # noqa: BLE001
        pass


def remove_desktop(desktop_id: str) -> None:
    def run():
        vd = _resolved(desktop_id)
        first = None
        for d in _all():
            if norm_guid(d.id) != norm_guid(desktop_id):
                first = d
                break
        if first is None:
            raise RuntimeError("Cannot remove the last desktop")
        vd.remove(fallback=first)

    _COM.call(run)


def _all():
    import pyvda

    return pyvda.get_virtual_desktops()
