"""Event-driven window retrieval (v2).

Four threads: the hook only reports (windows.py), the decider (here, one
thread) for fast move decisions (ms), the watcher (here, one thread) for
late windows "any time" (updaters, login after an action), the COM thread
executes (vdesk.py, hop before move). Auto-move only with proven ownership
(job hit OR process tree). Foreign-host candidates always go to delta, even
with only one candidate. Proven windows are hidden at discovery (ownership
gate, fail-open); every non-move classification restores them (restore
guarantee, measured by hide_balance).
"""

import queue
import threading
import time

from . import hop, messages, state, vdesk, windows

import logging

log = logging.getLogger(__name__)


# Open points (measure, don't guess): queue cap + usage window.
COLLISION_QUEUE_MAX_S = 2.0
QUOTA_PER_HWND = 3
# Measured handling client->move WITH the v2 move chain: 3-64 ms
# (retrieval_measure_test, 19.10.2026; our share, goal <250 ms) - the
# uncloak cadence + commit re-move + desk verify all ride inside that
# budget (parked-window pull: 57 ms median). Historic naked-move phases
# (exp23, 11.10.2026): AppView construct ~0-1 ms, move ~7 ms, confirm
# ~1 ms, post-move show ~0 ms.
# SHOW->move ~0.9-3.3 s is cold app-init (view floor), not retrieval.
# Transients (4/5 hwnds) never register a view and fail loudly - never
# hammer them at full rate (backoff below); the main window must never
# queue behind doomed transients (area-descending order at call sites).
LATENCY_GOAL_MS = 250.0
LAUNCH_WINDOW_S = 5.0
LAUNCH_REST_S = 0.5

_MOVE_COUNT: dict = {}
_MOVE_LOCK = threading.Lock()
_STATS: "object" = __import__("collections").deque(maxlen=200)

_DECIDER_Q: "queue.Queue" = queue.Queue()
_DECIDER_ONCE = threading.Lock()
_DECIDER_STARTED = [False]

# Set by the server: rest() = hop mutex free AND no open verification step.
_REST_FN = [lambda: not hop.hop_busy()]


def set_rest_fn(fn) -> None:
    _REST_FN[0] = fn


def is_rest() -> bool:
    try:
        return bool(_REST_FN[0]())
    except Exception:  # noqa: BLE001
        return not hop.hop_busy()


def _ensure_decider() -> None:
    with _DECIDER_ONCE:
        if _DECIDER_STARTED[0]:
            return
        _DECIDER_STARTED[0] = True

    def _loop():
        while True:
            fn, result_q = _DECIDER_Q.get()
            try:
                result_q.put(("ok", fn()))
            except BaseException as exc:  # noqa: BLE001
                result_q.put(("err", exc))

    threading.Thread(target=_loop, daemon=True, name="vision-decider").start()


def _decider_call(fn, timeout: float = 30.0):
    """Only for fast move decisions (ms). Launch loops run in the caller
    thread so parallel launches do not serialize for up to 45 s."""
    _ensure_decider()
    result_q: "queue.Queue" = queue.Queue()
    _DECIDER_Q.put((fn, result_q))
    status, value = result_q.get(timeout=timeout)
    if status == "err":
        raise value if isinstance(value, BaseException) else RuntimeError(str(value))
    return value


def _tick_after(a: int, b: int) -> bool:
    try:
        diff = (int(a) - int(b)) & 0xFFFFFFFF
        return 0 < diff < 0x7FFFFFFF
    except Exception:  # noqa: BLE001
        return False


def user_touched(hwnd: int, appear_tick: int) -> bool:
    """Interaction guard: taking the foreground at appear time does NOT count.
    Evidence = window is foreground AND last input is after appearance.
    Own SendInputs (hop intervals) are subtracted."""
    try:
        if windows.foreground_hwnd() != int(hwnd):
            return False
        li = windows.last_input_tick()
        if not li:
            return False
        if hop.is_own_input_tick(li):
            return False
        return _tick_after(li, appear_tick)
    except Exception:  # noqa: BLE001
        return False


def _quota_hit(hwnd: int) -> bool:
    with _MOVE_LOCK:
        return _MOVE_COUNT.get(int(hwnd), 0) >= QUOTA_PER_HWND


def _quota_add(hwnd: int) -> None:
    with _MOVE_LOCK:
        _MOVE_COUNT[int(hwnd)] = _MOVE_COUNT.get(int(hwnd), 0) + 1


# Two separate budgets (13.10.2026; re-acceptance RED on runs 5+7: backoff
# cut attempts-per-timeout from ~100 to ~5 while the timeout stayed fixed,
# so a view registering after ~1.5 s was missed and the main restored).
# Budget 1 - view ready: max time from appearance (SHOW) waiting for DWM
# view registration. Generous on purpose: backoff spaces drip attempts
# (~10 over 8 s), retries never block the decider, so this costs attempts,
# not stalls. Transients (never register) still die bounded.
VIEW_READY_MAX_S = 8.0
# Budget 2 - actual move: max executed view-not-ready attempts per hwnd
# (each _notready_arms is one executed move that found no view).
NOTREADY_MAX_N = 10
# Hard backstop: restore at the latest this long after appearance, even if
# the attempt budget is unspent. Attempts need rest(); if rest sticks False
# (stuck hop/verification), attempts never execute, N never grows, and the
# two-budget timeout alone would hold hidden windows forever - silently
# (no delta, no restore, no evidence). The backstop guarantees termination
# regardless of rest; show_window needs no rest (direct Win32).
VIEW_HARD_MAX_S = 20.0
# Handover floor: leftovers are handed to the watcher only when a
# substantial view budget remains. Nearly-spent entries (hopeless
# transients) terminate in the tail - handing them over would only delay
# their restore and keep them hidden with zero benefit.
HANDOVER_MIN_REMAINING_S = 2.0

# Viewless-transient backoff (generic per-hwnd counter, no app knowledge):
# each consecutive "view-not-ready" doubles the wait before the next drip
# attempt (0.1 -> 0.2 -> 0.4 -> 0.8 -> cap 1.0 s). Exp23: 20/25 transient
# attempts failed loudly at full rate - that burn helped nobody. Any
# terminal classification (moved/already/other delta/gone) resets.
_NOTREADY_N: dict = {}
_NOTREADY_WAIT: dict = {}
_NOTREADY_LOCK = threading.Lock()
_NOTREADY_BASE_S = 0.1
_NOTREADY_CAP_S = 1.0


def _notready_arms(hwnd: int) -> float:
    """Record one view-not-ready; returns next eligible timestamp."""
    h = int(hwnd)
    with _NOTREADY_LOCK:
        n = _NOTREADY_N.get(h, 0) + 1
        _NOTREADY_N[h] = n
        wait = min(_NOTREADY_BASE_S * (2 ** (n - 1)), _NOTREADY_CAP_S)
        ready = time.time() + wait
        _NOTREADY_WAIT[h] = ready
        return ready


def _notready_eligible(hwnd: int) -> bool:
    with _NOTREADY_LOCK:
        return time.time() >= _NOTREADY_WAIT.get(int(hwnd), 0.0)


def _notready_reset(hwnd: int) -> None:
    with _NOTREADY_LOCK:
        _NOTREADY_N.pop(int(hwnd), None)
        _NOTREADY_WAIT.pop(int(hwnd), None)


def _notready_spent(hwnd: int) -> bool:
    """Move-attempt budget (budget 2) spent? Counts executed attempts."""
    with _NOTREADY_LOCK:
        return _NOTREADY_N.get(int(hwnd), 0) >= NOTREADY_MAX_N


def _budgets_open(hwnd: int, deadline: float) -> bool:
    """True while at least one budget remains: view time (budget 1) OR move
    attempts (budget 2). Restore fires only when both are spent."""
    try:
        if time.time() < float(deadline):
            return True
    except Exception:  # noqa: BLE001
        return True
    return not _notready_spent(hwnd)


def _order_largest_first(hwnds: list) -> list:
    """Move order: largest client area first (generic, no app knowledge).
    The main window must never queue behind doomed viewless transients
    (exp23b: reversed order wasted 3 calls before main, area-desc 0)."""
    try:
        return sorted(set(int(h) for h in hwnds),
                      key=lambda h: -windows.client_area(h))
    except Exception:  # noqa: BLE001
        return list(hwnds)


# Hidden-window ledger (restore guarantee): every hwnd hidden via the
# ownership gate must end visible somewhere (box via move, Main via
# restore) or dead. hidden == moved + restored + gone + restore_failed.
# A window hidden and never resolved is theft by invisibility - worse than
# a flash. Foreign windows are never hidden (tripwire counter below).
_HIDE_STATS = {"hidden": 0, "moved": 0, "restored": 0, "gone": 0,
               "restore_failed": 0, "foreign_hide_attempts": 0}
_HIDE_LOCK = threading.Lock()
_HIDDEN_PENDING: set = set()  # hidden, not yet resolved


def _note_hidden(hwnd: int, desktop_id: str, prov: str) -> bool:
    """Register a hide. Only job/tree provenance may hide; anything else
    increments the tripwire and hides nothing. Returns True if registered."""
    try:
        h = int(hwnd)
    except Exception:  # noqa: BLE001
        return False
    with _HIDE_LOCK:
        if prov not in ("job", "tree"):
            _HIDE_STATS["foreign_hide_attempts"] += 1
            return False
        if h in _HIDDEN_PENDING:
            return True
        _HIDDEN_PENDING.add(h)
        _HIDE_STATS["hidden"] += 1
    # HART UEBERLEBEND: Eintrag in state.json, BEVOR das Fenster versteckt
    # wird. Bei Kill/Absturz laeuft atexit nicht - dieser Eintrag ist dann
    # die einzige Spur, und der naechste Server-Start holt das Fenster
    # zurueck (restore_hidden_at_start). Reihenfolge: erst der Eintrag,
    # dann verstecken - nie umgekehrt (Luecke = fuer immer unsichtbar).
    try:
        state.add_hidden(h, {"desktop_id": str(desktop_id),
                             "since": time.time()})
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _note_hidden() (state.add_hidden failed, "
                  "hide proceeds - hard-kill recovery would be blind)",
                  exc_info=True)
    _ensure_hidden_janitor()
    return True


def _resolve_hidden(hwnd: int, show: bool) -> str | None:
    """Resolve one hidden hwnd. show=True (terminal, must end visible):
    show_window + visibility check -> restored / gone / restore_failed.
    show=False (moved to box): fire-and-forget target show, count moved.
    Returns the class or None when the hwnd was never registered hidden."""
    try:
        h = int(hwnd)
    except Exception:  # noqa: BLE001
        return None
    with _HIDE_LOCK:
        if h not in _HIDDEN_PENDING:
            return None
        _HIDDEN_PENDING.discard(h)
    if not show:
        with _HIDE_LOCK:
            _HIDE_STATS["moved"] += 1
        try:
            windows.async_show(h)  # visible on target, never blocks decider
        except Exception:  # noqa: BLE001
            log.debug("swallowed in _resolve_hidden() (was silent pass)", exc_info=True)
        # state-Eintrag bleibt stehen, bis das Fenster sichtbar ist - der
        # Janitor wirft ihn weg, sobald is_window_visible True meldet
        # (Reihenfolge egal: sichtbar => nichts mehr zu retten).
        return "moved"
    try:
        alive = windows.is_window(h)
    except Exception:  # noqa: BLE001
        alive = False
    if not alive:
        with _HIDE_LOCK:
            _HIDE_STATS["gone"] += 1
        _drop_hidden_entry(h)  # tot -> nichts mehr zu retten
        return "gone"
    try:
        windows.show_window(h)  # caller/watcher thread, never the decider
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _resolve_hidden() (was silent pass)", exc_info=True)
    try:
        if windows.is_window_visible(h):
            with _HIDE_LOCK:
                _HIDE_STATS["restored"] += 1
            _drop_hidden_entry(h)  # sichtbar -> aufgeloest
            return "restored"
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _resolve_hidden() (was silent pass)", exc_info=True)
    try:
        if not windows.is_window(h):
            with _HIDE_LOCK:
                _HIDE_STATS["gone"] += 1
            _drop_hidden_entry(h)
            return "gone"
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _resolve_hidden() (was silent pass)", exc_info=True)
    with _HIDE_LOCK:
        _HIDE_STATS["restore_failed"] += 1
    # Eintrag BEWUSST stehen lassen: das Fenster ist immer noch unsichtbar,
    # der naechste Start versucht die Rueckholung erneut.
    return "restore_failed"


def hide_balance() -> dict:
    """Measured restore guarantee: hidden == moved+restored+gone+restore_failed.
    balanced must be True; restore_failed and foreign_hide_attempts must be 0."""
    with _HIDE_LOCK:
        b = dict(_HIDE_STATS)
        b["pending_hidden"] = len(_HIDDEN_PENDING)
        accounted = b["moved"] + b["restored"] + b["gone"] + b["restore_failed"]
        b["balanced"] = (b["hidden"] == accounted)
        return b


def _restore_all_hidden_at_exit() -> None:
    """Safety net: no hidden window may outlive the resolver process.
    Hidden-pending hwnds are shown again (or counted gone). Without this,
    a process exit with unspent budgets orphans invisible windows -
    theft by invisibility with no process left to resolve them."""
    try:
        with _HIDE_LOCK:
            pending = sorted(_HIDDEN_PENDING)
    except Exception:  # noqa: BLE001
        return
    for h in pending:
        try:
            _resolve_hidden(h, show=True)
        except Exception:  # noqa: BLE001
            log.debug("swallowed in _restore_all_hidden_at_exit() (was silent pass)", exc_info=True)


def _drop_hidden_entry(h: int) -> None:
    """state.json-Eintrag entfernen - darf den Aufrufer nie kosten."""
    try:
        state.drop_hidden(h)
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _drop_hidden_entry() (was silent pass)", exc_info=True)


HIDDEN_JANITOR_S = 2.0     # Aufraum-Takt
HIDDEN_GRACE_S = 15.0      # Schonfrist: Eintrag entsteht VOR dem async_hide
_JANITOR_LOCK = threading.Lock()
_JANITOR_STARTED = [False]


def _hidden_janitor_loop() -> None:
    """Entfernt Eintraege, die nichts mehr zu retten haben: Fenster sichtbar
    (Rueckholung ueberfluessig) oder tot. NIE selbst anzeigen - laufendes
    Verstecken ist Absicht (Bewegung in die Box), zurueckgeholt wird nur
    beim Start. Die Schutzfrist verhindert, dass der frische Eintrag vor
    dem greifenden async_hide weggewischt wird."""
    while True:
        time.sleep(HIDDEN_JANITOR_S)
        try:
            now = time.time()
            for h_str, info in state.hidden_entries().items():
                try:
                    h = int(h_str)
                    if float(info.get("since", 0) or 0) + HIDDEN_GRACE_S > now:
                        continue
                    if (not windows.is_window(h)) or windows.is_window_visible(h):
                        state.drop_hidden(h)
                except Exception:  # noqa: BLE001
                    log.debug("swallowed in _hidden_janitor_loop() (entry) (was silent pass)",
                              exc_info=True)
        except Exception:  # noqa: BLE001
            log.debug("swallowed in _hidden_janitor_loop() (was silent pass)",
                      exc_info=True)


def _ensure_hidden_janitor() -> None:
    with _JANITOR_LOCK:
        if _JANITOR_STARTED[0]:
            return
        _JANITOR_STARTED[0] = True
    threading.Thread(target=_hidden_janitor_loop, daemon=True,
                     name="vision-hidden-janitor").start()


def restore_hidden_at_start() -> dict:
    """Harter Kill (Task-Manager, Absturz): atexit-Recovery lief NICHT.
    Alle offenen Hide-Eintraege aus state.json hier zurueckholen - der
    Server-Start ruft das VOR dem Warten auf Anfragen auf.

    Pro Eintrag: tot -> weg; sichtbar -> weg (nur Buchhaltung war offen);
    versteckt -> show_window + Sichtpruefung. Bleibt es unsichtbar, bleibt
    der Eintrag stehen (naechster Start versucht es erneut) - nie blind
    als erledigt buchen."""
    stats = {"entries": 0, "gone": 0, "already_visible": 0, "shown": 0,
             "still_hidden": 0}
    try:
        entries = state.hidden_entries()
        _ensure_hidden_janitor()
    except Exception:  # noqa: BLE001
        log.debug("swallowed in restore_hidden_at_start() (was silent pass)",
                  exc_info=True)
        return stats
    for h_str in list(entries):
        stats["entries"] += 1
        try:
            h = int(h_str)
            if not windows.is_window(h):
                _drop_hidden_entry(h)
                stats["gone"] += 1
                continue
            if windows.is_window_visible(h):
                _drop_hidden_entry(h)
                stats["already_visible"] += 1
                continue
            windows.show_window(h)
            if windows.is_window_visible(h):
                _drop_hidden_entry(h)
                stats["shown"] += 1
            else:
                stats["still_hidden"] += 1  # Eintrag bleibt (Erneutversuch)
        except Exception:  # noqa: BLE001
            log.debug("swallowed in restore_hidden_at_start() (entry) "
                      "(was silent pass)", exc_info=True)
            stats["still_hidden"] += 1
    return stats


try:
    import atexit as _atexit
    _atexit.register(_restore_all_hidden_at_exit)
except Exception:  # noqa: BLE001
    log.debug("swallowed in <modul>() (was silent pass)", exc_info=True)


def _appear_tick(hwnd: int, since_wall: float) -> int:
    try:
        for (_wt, tick, h, _pid) in windows.hook_events_since(since_wall):
            if int(h) == int(hwnd):
                return int(tick)
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _appear_tick() (was silent pass)", exc_info=True)
    try:
        return int(windows._tick_now())
    except Exception:  # noqa: BLE001
        return 0


def _appear_wall(hwnd: int, since_wall: float, fallback: float) -> float:
    """Real SHOW timestamp from hook events (honest show->move latency).
    Falls back to discovery time when no hook event exists."""
    try:
        for (wt, _tick, h, _pid) in windows.hook_events_since(since_wall):
            if int(h) == int(hwnd):
                return float(wt)
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _appear_wall() (was silent pass)", exc_info=True)
    return float(fallback)


# Parallel launches: foreign trees in flight may belong to the neighbor
# launch - neither move them nor report them as foreign into delta (no
# delta spam, no timer reset). The owner (or the watcher) handles them.
_INFLIGHT: set = set()  # frozenset(root_pids) per active launch
_INFLIGHT_LOCK = threading.Lock()


def _inflight_add(root_pids: list) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.add(frozenset(int(p) for p in root_pids))


def _inflight_drop(root_pids: list) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.discard(frozenset(int(p) for p in root_pids))


def _in_other_inflight(wpid: int, own_roots: list) -> bool:
    try:
        wpid = int(wpid)
        own = set(int(p) for p in own_roots)
        with _INFLIGHT_LOCK:
            others = [s for s in _INFLIGHT if s != frozenset(own)]
        for s in others:
            try:
                if windows.tree_contains(list(s), wpid):
                    return True
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _in_other_inflight() (was silent pass)", exc_info=True)
    return False


def _title_of(hwnd: int) -> str:
    """Title on demand (message round-trip). Kept by name for callers."""
    try:
        return windows.get_title(int(hwnd))
    except Exception:  # noqa: BLE001
        return ""


def _pid_of(hwnd: int) -> int:
    """Owning PID, cheap single call. Kept by name for callers."""
    try:
        return int(windows.pid_of(int(hwnd)))
    except Exception:  # noqa: BLE001
        return 0


def _try_move(hwnd: int, desktop_id: str, appear_wall: float,
              appear_tick: int) -> dict:
    """One move with all guards. Returns {"moved": bool, "reason": str}.

    Strictly non-blocking: exactly one move attempt (plus one immediate
    retry after the fallback show), no sleeps. Waiting for a late DWM view
    happens via pending re-queue in the caller, never inside the serialized
    decider - one viewless transient must not stall the main window behind
    it (head-of-line blocking). "view-not-ready" means: retry later."""
    hwnd = int(hwnd)
    if _quota_hit(hwnd):
        return {"moved": False, "reason": "quota"}
    if user_touched(hwnd, appear_tick):
        return {"moved": False, "reason": "user-touched"}
    fg_before = windows.foreground_hwnd()
    t0 = time.time()
    # Move first; show only as fallback. A blind pre-show costs one visible
    # flash per window, so it runs solely when the view is missing: a never-
    # shown view is unknown to MoveWindowToDesktop ("element not found").
    outcome = vdesk.move_if_unpinned(hwnd, desktop_id)
    if outcome != "moved" and outcome != "already" and outcome != "pin":
        # Fallback show goes async (fire-and-forget): a blocking ShowWindow
        # would stall the serialized decider for ~650 ms on a busy target
        # thread (measured). The move itself is retried via pending re-queue.
        windows.async_show(hwnd)
        _notready_arms(hwnd)
        return {"moved": False, "reason": "view-not-ready"}
    if outcome == "pin":
        _notready_reset(hwnd)
        return {"moved": False, "reason": "pin"}
    if outcome != "moved" and outcome != "already":
        _notready_reset(hwnd)
        return {"moved": False, "reason": f"move-failed: {outcome}"[:80]}
    already = (outcome == "already")
    # The window arrived hidden (ownership hide): request target visibility
    # fire-and-forget. Resolution ("moved") happens at the call sites.
    try:
        windows.async_show(hwnd)
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _try_move() (was silent pass)", exc_info=True)
    # Owner co-move (generic): take owned popups of the same owner along.
    # Fast path without describe (no text round-trip per window).
    try:
        for owned in windows.owned_hwnds(hwnd):
            try:
                if not windows.has_client(owned):
                    continue
                if vdesk.move_if_unpinned(owned, desktop_id) != "moved":
                    continue
                try:
                    windows.async_show(owned)
                except Exception:  # noqa: BLE001
                    log.debug("swallowed in _try_move() (was silent pass)", exc_info=True)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _try_move() (was silent pass)", exc_info=True)
    # Focus guard: only on actually lost foreground (before/after).
    try:
        fg_after = windows.foreground_hwnd()
        if fg_before and fg_after != fg_before and fg_after == hwnd:
            try:
                import ctypes as _ct

                _ct.windll.user32.SetForegroundWindow(_ct.c_void_p(fg_before))
            except Exception:  # noqa: BLE001
                log.debug("swallowed in _try_move() (was silent pass)", exc_info=True)
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _try_move() (was silent pass)", exc_info=True)
    # "already" burned no move: no quota, but same placement accounting.
    if not already:
        _quota_add(hwnd)
    _notready_reset(hwnd)
    latency_ms = (time.time() - appear_wall) * 1000.0
    try:
        _STATS.append({"hwnd": hwnd, "latency_ms": latency_ms, "t": time.time()})
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _try_move() (was silent pass)", exc_info=True)
    return {"moved": True,
            "reason": "already-on-target" if already else "box",
            "latency_ms": latency_ms, "queued_s": max(0.0, t0 - appear_wall)}


def _decide_move(hwnd: int, desktop_id: str, appear_wall: float,
                 appear_tick: int) -> dict:
    """Move decision serialized (ms) - parallel launches share only this
    fast path, no 5-s loop. handling_ms = decision+move from client
    (without app init); latency_ms = from hook SHOW (flash, incl. init)."""
    t0 = time.time()
    res = _decider_call(lambda: _try_move(hwnd, desktop_id, appear_wall, appear_tick),
                        timeout=15.0)
    try:
        res["handling_ms"] = (time.time() - t0) * 1000.0
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _decide_move() (was silent pass)", exc_info=True)
    return res


def _provenance_of(job, root_pids: list, wpid: int, hwnd: int) -> str:
    """Provenance before description: pid (one cheap call at the caller) ->
    job/tree hit -> title only on delta demand. Proven moves never pay for
    a title on the hot path. The frame mapping covers hosted windows."""
    try:
        if job is not None and windows.job_contains(job, int(wpid)):
            return "job"
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _provenance_of() (was silent pass)", exc_info=True)
    try:
        if windows.tree_contains(root_pids, int(wpid)):
            return "tree"
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _provenance_of() (was silent pass)", exc_info=True)
    try:
        kids = windows.frame_child_pids(int(hwnd))
        if kids and (kids & windows.cached_tree_pids(root_pids)):
            return "tree"
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _provenance_of() (was silent pass)", exc_info=True)
    return "foreign"


def _delta_add(hwnd: int, pid: int, title: str, reason: str,
               detail: str = "") -> dict | None:
    try:
        return state.add_delta({"hwnd": int(hwnd), "pid": int(pid),
                                "title": str(title or ""), "reason": reason,
                                "detail": detail} if detail else
                               {"hwnd": int(hwnd), "pid": int(pid),
                                "title": str(title or ""), "reason": reason})
    except Exception:  # noqa: BLE001
        return None


def _handle_candidate(desktop_id: str, h: int, wpid: int, appear_wall: float,
                      appear_tick: int, job, root_pids: list,
                      moved: list, moved_hwnds: list, delta: list,
                      latencies: list, pending: list,
                      handling: list | None = None) -> str:
    """One candidate, provenance before description: pid -> job/tree hit ->
    guards/move without any title. The title (message round-trip) is fetched
    solely for delta entries and the moved display list.
    Returns provenance (rest timer ticks only for own trees, not noise)."""
    h = int(h)
    try:
        wpid = int(wpid)
    except Exception:  # noqa: BLE001
        wpid = 0
    if _in_other_inflight(wpid, root_pids):
        return "inflight"
    prov = _provenance_of(job, root_pids, wpid, h)
    if prov == "foreign":
        e = _delta_add(h, wpid, windows.get_title(h), "foreign-host")
        if e:
            delta.append(e)
        return prov
    # Pin is checked by _try_move in the same COM round as the move (1 round-trip).
    if user_touched(h, appear_tick):
        # Never hide a window the user is using - they keep it visible.
        e = _delta_add(h, wpid, windows.get_title(h), "user-touched",
                       messages.USER_TOUCHED_DETAIL)
        if e:
            delta.append(e)
        return prov
    # Ownership-gated hide (proven job/tree only): the flash window closes
    # here. Fail-open: hide errors leave the window visible. Every later
    # terminal branch resolves the ledger (moved/restored/gone).
    if _note_hidden(h, desktop_id, prov):
        try:
            windows.async_hide(h)
        except Exception:  # noqa: BLE001
            log.debug("swallowed in _handle_candidate() (was silent pass)", exc_info=True)
    if not is_rest():
        now_q = time.time()
        pending.append((h, appear_wall, appear_tick, now_q, "collision",
                        now_q + COLLISION_QUEUE_MAX_S))
        return prov
    res = _decide_move(h, desktop_id, appear_wall, appear_tick)
    if res.get("moved"):
        _resolve_hidden(h, show=False)
        moved.append(windows.get_title(h) or str(h))
        moved_hwnds.append(h)
        latencies.append(float(res.get("latency_ms", 0.0)))
        if handling is not None:
            handling.append(float(res.get("handling_ms", 0.0)))
    elif res.get("reason") == "view-not-ready":
        now_q = time.time()
        pending.append((h, appear_wall, appear_tick, now_q, "notready",
                        appear_wall + VIEW_READY_MAX_S))
    else:
        _resolve_hidden(h, show=True)
        _notready_reset(h)
        e = _delta_add(h, wpid, windows.get_title(h),
                       res.get("reason", "move-failed"))
        if e:
            delta.append(e)
    return prov


def _drain_pending(desktop_id: str, pending: list, moved: list,
                   moved_hwnds: list, delta: list, latencies: list,
                   handling: list | None = None) -> list:
    """Drain queue: rest -> move, user -> delta, timeout -> delta.
    Entries are (h, awall, atick, enq, kind, deadline); kind is "collision"
    (hop session running) or "notready" (DWM view missing, re-queued
    without blocking the decider). Notready deadline is budget 1 (view time,
    anchored at appearance); restore fires only when budget 1 AND budget 2
    (move attempts) are spent (_budgets_open)."""
    now = time.time()
    try:
        pending = sorted(pending,
                         key=lambda t: -windows.client_area(int(t[0])))
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _drain_pending() (was silent pass)", exc_info=True)
    still: list = []
    for (h, awall, atick, enq, kind, deadline) in pending:
        if not windows.is_window(h):
            _resolve_hidden(h, show=True)  # counts gone for registered hwnds
            _notready_reset(h)
            continue
        if user_touched(h, atick):
            _resolve_hidden(h, show=True)  # user gets a visible window back
            _notready_reset(h)
            e = _delta_add(h, _pid_of(h), _title_of(h), "user-touched",
                           messages.USER_TOUCHED_DETAIL)
            if e:
                delta.append(e)
            continue
        if kind == "notready" and (
                not _budgets_open(h, deadline)
                or now >= awall + VIEW_HARD_MAX_S):
            _resolve_hidden(h, show=True)
            _notready_reset(h)
            e = _delta_add(h, _pid_of(h), _title_of(h),
                           "move-failed: view-not-ready")
            if e:
                delta.append(e)
            continue
        if kind == "collision" and now >= deadline:
            _resolve_hidden(h, show=True)
            e = _delta_add(h, _pid_of(h), _title_of(h), "queue-overflow")
            if e:
                delta.append(e)
            continue
        if kind == "notready" and not _notready_eligible(h):
            # Backoff: viewless transient waits its drip turn instead of
            # burning a decider round every 20 ms.
            still.append((h, awall, atick, enq, kind, deadline))
            continue
        if not is_rest():
            still.append((h, awall, atick, enq, kind, deadline))
            continue
        res = _decide_move(h, desktop_id, awall, atick)
        if res.get("moved"):
            _resolve_hidden(h, show=False)
            try:
                w2t = _title_of(h)
            except Exception:  # noqa: BLE001
                w2t = str(h)
            moved.append(w2t or str(h))
            moved_hwnds.append(h)
            latencies.append(float(res.get("latency_ms", 0.0)))
            if handling is not None:
                handling.append(float(res.get("handling_ms", 0.0)))
        elif res.get("reason") == "view-not-ready":
            still.append((h, awall, atick, enq, kind, deadline))
        else:
            _resolve_hidden(h, show=True)
            _notready_reset(h)
            e = _delta_add(h, _pid_of(h), _title_of(h), res.get("reason", "move-failed"))
            if e:
                delta.append(e)
    return still


def launch_and_retrieve(desktop_id: str, exe: str, args: str,
                        wait_seconds: float, cwd: str,
                        desktop_name: str = "") -> dict:
    """Suspended start + event-driven retrieval (caller thread).

    Flow: suspended start with job (tree fallback) -> collect hook events +
    diffs (5-s cap + 0.5-s rest) -> per candidate immediately via
    _handle_candidate (pin/provenance/interaction/collision), moves fast
    serialized. Proven windows are hidden at discovery (ownership gate,
    fail-open) and every non-move classification restores them via
    show_window - measured by hide_balance (hidden == moved+restored+gone+
    restore_failed). Afterwards watch registration: late windows (updaters,
    login after an action) are picked up by the watcher "any time", not just
    in the launch window. wait_seconds > 5 is explicitly reported as
    launch_window_s=5 + wait_capped=True (no silent capping). Latency honestly
    from hook SHOW (flash=cost), rest timer only for own trees (no extension
    by noise). Returns {"pid":..., "moved_windows":[...], "handoff":bool,
    "delta":[...], "latencies_ms":[...], "used_job":bool,
    "launch_window_s":float, "wait_capped":bool, "hide_balance":dict}."""
    cmd = f'"{exe}" {args}'.strip()
    windows._ensure_hook()
    before = windows.all_hwnds()
    started = windows.launch_in_job(cmd, cwd=cwd, job_key=f"{desktop_id}-{time.time()}")
    pid = int(started["pid"])
    job = started.get("job")
    used_job = bool(started.get("used_job"))
    root_pids = [pid]
    entry_wall = time.time()
    want = max(2.0, float(wait_seconds))
    cap = min(want, LAUNCH_WINDOW_S)
    wait_capped = want > LAUNCH_WINDOW_S
    deadline = entry_wall + cap
    known = set(before)
    moved: list = []
    moved_hwnds: list = []
    delta: list = []
    latencies: list = []
    handling_ms: list = []
    pending: list = []
    retry: dict = {}
    first_found = None
    last_new = entry_wall
    _inflight_add(root_pids)
    try:
        while time.time() < deadline:
            now = time.time()
            cands: list = []
            try:
                for h in windows.all_hwnds() - known:
                    known.add(h)
                    cands.append(h)
            except Exception:  # noqa: BLE001
                log.debug("swallowed in launch_and_retrieve() (was silent pass)", exc_info=True)
            try:
                for (_wt, _tick, h, _p) in windows.hook_events_since(entry_wall - 3.0):
                    if h not in known:
                        known.add(h)
                        cands.append(h)
            except Exception:  # noqa: BLE001
                log.debug("swallowed in launch_and_retrieve() (was silent pass)", exc_info=True)
            for h in _order_largest_first(cands):
                h = int(h)
                # Provenance before description: the pid is one cheap call,
                # the title (message round-trip) is paid only on delta/display.
                if not windows.is_window(h):
                    if h not in retry:
                        retry[h] = now
                        known.discard(h)
                    elif (now - retry[h]) <= 5.0:
                        known.discard(h)
                    else:
                        retry.pop(h, None)
                    continue
                retry.pop(h, None)
                wpid = windows.pid_of(h)
                if (windows.class_of(h) or "") == "IME":
                    continue
                try:
                    if not windows.has_client(h):
                        continue
                except Exception:  # noqa: BLE001
                    continue
                atick = _appear_tick(h, entry_wall - 3.0)
                awall = _appear_wall(h, entry_wall - 3.0, now)
                prov = _handle_candidate(desktop_id, h, wpid, awall, atick,
                                         job, root_pids, moved, moved_hwnds,
                                         delta, latencies, pending, handling_ms)
                if prov not in ("foreign", "inflight"):
                    if first_found is None:
                        first_found = now
                    last_new = now
            pending = _drain_pending(desktop_id, pending, moved, moved_hwnds,
                                     delta, latencies, handling_ms)
            if first_found is not None and (now - last_new) >= LAUNCH_REST_S and not pending:
                break
            time.sleep(0.02)
        # Tail drain after the deadline: bounded extra time, same rules.
        tail_end = time.time() + 2.0
        while pending and time.time() < tail_end:
            pending = _drain_pending(desktop_id, pending, moved, moved_hwnds,
                                     delta, latencies, handling_ms)
            if pending:
                time.sleep(0.05)
        try:
            watch_register(desktop_name or desktop_id, desktop_id, job, root_pids)
        except Exception:  # noqa: BLE001
            log.debug("swallowed in launch_and_retrieve() (was silent pass)", exc_info=True)
        for (h, _awall, _atick, _enq, kind, _deadline) in list(pending):
            try:
                if not windows.is_window(h):
                    _resolve_hidden(h, show=True)  # gone
                    _notready_reset(h)
                    continue
                if user_touched(h, _atick):
                    _resolve_hidden(h, show=True)  # user gets it back now
                    _notready_reset(h)
                    e = _delta_add(h, _pid_of(h), _title_of(h), "user-touched")
                    if e:
                        delta.append(e)
                    continue
                if kind == "notready" and _budgets_open(h, _deadline):
                    # Budgets unspent: hand over to the watcher instead of a
                    # terminal restore - the 2-s launch tail must not cut the
                    # generous view budget short. Ledger stays hidden-pending.
                    # Only when substantial view budget remains; nearly-spent
                    # entries terminate here (hopeless transients gain nothing
                    # from handover, and launch returns settled).
                    try:
                        if _deadline - time.time() > HANDOVER_MIN_REMAINING_S:
                            with _WATCH_LOCK:
                                w = _WATCHES.get(str(desktop_id))
                                if w is not None:
                                    w["pending"].append(
                                        (h, _awall, _atick, _enq, kind, _deadline))
                                    continue
                    except Exception:  # noqa: BLE001
                        log.debug("swallowed in launch_and_retrieve() (was silent pass)", exc_info=True)
                reason = ("queue-overflow" if kind == "collision" else "move-failed: view-not-ready")
                _resolve_hidden(h, show=True)  # visible again on Main
                _notready_reset(h)
                e = _delta_add(h, _pid_of(h), _title_of(h), reason)
                if e:
                    delta.append(e)
            except Exception:  # noqa: BLE001
                log.debug("swallowed in launch_and_retrieve() (was silent pass)", exc_info=True)
        handoff = (len(moved_hwnds) == 0 and len(delta) > 0)
        time.sleep(0.3)
        return {"pid": pid, "moved_windows": moved, "moved_hwnds": moved_hwnds,
                "handoff": handoff, "foreign": bool(handoff),
                "delta": delta, "latencies_ms": latencies, "used_job": used_job,
                "handling_ms": handling_ms, "hide_balance": hide_balance(),
                "launch_window_s": cap, "wait_capped": wait_capped}
    finally:
        _inflight_drop(root_pids)



def wintypes_pid(hwnd: int) -> int:
    import ctypes
    from ctypes import wintypes

    try:
        pid = wintypes.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(
            ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def stats_last(n: int = 20) -> list:
    try:
        return list(_STATS)[-max(1, n):]
    except Exception:  # noqa: BLE001
        return []


# --- Permanent retrieval "any time" (watcher) -------------------------------
# The launch window (5 s + rest) covers only startup. Late windows
# (updaters, login after an action, minutes later) are never seen by a launch
# thread. The watcher runs permanently, knows job/tree per box and retrieves
# later - with the same guards (pin, user, collision/queue, quota, focus).

_WATCHES: dict = {}  # desktop_id -> {name, job, root_pids, known:set, pending:list}
_WATCH_LOCK = threading.Lock()
_WATCH_ONCE = threading.Lock()
_WATCH_STARTED = [False]
# Fallback timeout only: the loop below sleeps on the hook's wake-up signal
# and re-scans on every event (millisecond granularity). The timeout covers
# a signal lost between scan and clear - nothing else.
WATCH_FALLBACK_S = 5.0


def watch_register(name: str, desktop_id: str, job, root_pids: list) -> None:
    try:
        seen = set(windows.all_hwnds())
    except Exception:  # noqa: BLE001
        seen = set()
    with _WATCH_LOCK:
        prev = _WATCHES.get(str(desktop_id))
        pending = list(prev.get("pending", [])) if prev else []
        _WATCHES[str(desktop_id)] = {"name": str(name), "job": job,
                                     "root_pids": list(root_pids or []),
                                     "known": seen, "pending": pending}
    _ensure_watcher()


def watch_unregister(desktop_id: str) -> None:
    with _WATCH_LOCK:
        watch = _WATCHES.pop(str(desktop_id), None)
    # Restore guarantee: hidden hwnds still queued for a removed box must
    # not stay invisible on Main - resolve them all here.
    if watch:
        for (h, _awall, _atick, _enq, _kind, _deadline) in list(watch.get("pending", [])):
            try:
                _resolve_hidden(h, show=True)
            except Exception:  # noqa: BLE001
                log.debug("swallowed in watch_unregister() (was silent pass)", exc_info=True)


def drop_watch_pending(desktop_id: str, hwnd: int) -> None:
    """Remove one hwnd from a watch queue (e.g. explicitly adopted elsewhere,
    so the watcher does not move it a second time)."""
    try:
        did, h = str(desktop_id), int(hwnd)
    except Exception:  # noqa: BLE001
        return
    try:
        with _WATCH_LOCK:
            watch = _WATCHES.get(did)
            if not watch:
                return
            watch["pending"] = [t for t in watch.get("pending", [])
                                if int(t[0]) != h]
    except Exception:  # noqa: BLE001
        log.debug("swallowed in drop_watch_pending() (was silent pass)", exc_info=True)


def watch_list() -> list:
    with _WATCH_LOCK:
        return [{"desktop_id": k, "name": v.get("name"),
                 "pending": len(v.get("pending", []))} for k, v in _WATCHES.items()]


def _watch_add_to_rec(desktop_id: str, hwnd: int) -> None:
    try:
        from . import state as _state

        did = str(desktop_id)
        with _WATCH_LOCK:
            name = (_WATCHES.get(did) or {}).get("name", "")
        if not name or name == did:
            # Name unknown: search recs by desktop_id.
            st = _state.load_state().get("desktops", {})
            for recname, rec in st.items():
                if rec.get("desktop_id") == did:
                    name = recname
                    break
        if not name:
            return
        try:
            rec = _state.require_desktop(name)
        except Exception:  # noqa: BLE001
            return
        if int(hwnd) not in rec.get("hwnds", []):
            rec.setdefault("hwnds", []).append(int(hwnd))
            _state.put_desktop(rec)
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _watch_add_to_rec() (was silent pass)", exc_info=True)


# Fast tick while work exists (timeline probe 14.10.2026): pending
# attempts ran every ~2-5 s (hook-event/5-s-fallback cadence), not at
# backoff spacing (0.1-1 s) - the ticks set the pace, so the view budget
# bought only ~2-4 attempts instead of ~10. While any watch has pending,
# tick every 0.5 s; backoff still paces actual attempts, drains skip cheaply
# when nothing is eligible. Idle machines keep the 5-s/event cadence.
WATCH_BUSY_S = 0.5


def _any_pending() -> bool:
    try:
        with _WATCH_LOCK:
            return any(bool(w.get("pending")) for w in _WATCHES.values())
    except Exception:  # noqa: BLE001
        return False


def _ensure_watcher() -> None:
    windows._ensure_hook()
    with _WATCH_ONCE:
        if _WATCH_STARTED[0]:
            return
        _WATCH_STARTED[0] = True

    def _loop():
        while True:
            try:
                windows.wait_for_window_event(
                    WATCH_BUSY_S if _any_pending() else WATCH_FALLBACK_S)
            except Exception:  # noqa: BLE001
                log.debug("swallowed in _loop() (was silent pass)", exc_info=True)
            try:
                _watch_tick()
            except Exception:  # noqa: BLE001
                log.debug("swallowed in _loop() (was silent pass)", exc_info=True)
            try:
                windows._wake_clear()
            except Exception:  # noqa: BLE001
                log.debug("swallowed in _loop() (was silent pass)", exc_info=True)

    threading.Thread(target=_loop, daemon=True, name="vision-watcher").start()


def _watch_tick() -> None:
    with _WATCH_LOCK:
        items = list(_WATCHES.items())
    if not items:
        return
    try:
        cur = set(windows.all_hwnds())
    except Exception:  # noqa: BLE001
        return
    try:
        events = windows.hook_events_since(time.time() - 5.0)
        event_hwnds = {int(h) for (_wt, _tick, h, _p) in events}
    except Exception:  # noqa: BLE001
        event_hwnds = set()
    for desktop_id, watch in items:
        job = watch.get("job")
        root_pids = watch.get("root_pids", [])
        known = watch.get("known", set())
        fresh = [h for h in (cur - known) | (event_hwnds - known)]
        if fresh:
            with _WATCH_LOCK:
                watch["known"] = set(known) | set(cur) | set(event_hwnds)
        moved: list = []
        moved_hwnds: list = []
        delta: list = []
        latencies: list = []
        pending = watch.get("pending", [])
        now = time.time()
        for h in _order_largest_first(fresh):
            h = int(h)
            if not windows.is_window(h):
                continue
            wpid = windows.pid_of(h)
            # Only own trees interest the watcher; it ignores foreign ones
            # (no delta spam: the launch path already reports handoff windows).
            # No title is fetched here at all - provenance first.
            if _provenance_of(job, root_pids, wpid, h) == "foreign":
                continue
            if (windows.class_of(h) or "") == "IME":
                continue  # System sink like tray (no user window).
            try:
                if not windows.has_client(h):
                    continue
            except Exception:  # noqa: BLE001
                continue
            atick = _appear_tick(h, now - 5.0)
            _handle_candidate(desktop_id, h, wpid, now, atick, job,
                              root_pids, moved, moved_hwnds, delta,
                              latencies, pending)
        pending = _drain_pending(desktop_id, pending, moved, moved_hwnds,
                                 delta, latencies)
        with _WATCH_LOCK:
            if desktop_id in _WATCHES:
                _WATCHES[desktop_id]["pending"] = pending
        for h in moved_hwnds:
            _watch_add_to_rec(desktop_id, h)
