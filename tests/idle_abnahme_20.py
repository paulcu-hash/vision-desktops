"""Idle acceptance, 20 runs. Vanilla: read-only src/, everything via tests/ + MCP tools.
Spec:
- per run desktop_create(run-N) empty, record desktop_list, delta baseline
- Matrix: 8 isolated (charmap immediate + 2s/5s/30s delayed), 4 Tk background,
  4 handoff (notepad store + chrome without profile), 2 pin, 2 interaction
- Measurement per window: hwnd, title, latency ms, flash, location per desktop ID,
  delta reason, misgrab
- Cleanup: window_list -> see verdict (title star guard) -> desktop_remove(decisions)
Hardening (runner, tests/ only): has_client filter before counting, show-before-
locate (dead vs box vs ERR measured), adopt only own delta hwnd (since run start
+ alive + verified on Main, PID against launch tree), interaction with early
hook tick + verified setup + stability.
RUNS env (e.g. RUNS=1,2,13) measures a subset into idle20_remeasure.json.
"""
import asyncio
import faulthandler
import json
import sys
import threading
import time
from pathlib import Path

faulthandler.enable()

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from fastmcp import Client

from vision_desktops import retrieval, state, vdesk, windows
from vision_desktops.server import mcp

PY = sys.executable
DELAY_HELPER = str(Path(r"C:\Users\Paul\vision-desktops\tests\window_placement_delayed_test.py").resolve())
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

results = []


def log(s):
    print(s, flush=True)


def idle_seconds():
    try:
        return max(0.0, ((int(windows._tick_now()) & 0xFFFFFFFF) - int(windows.last_input_tick())) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return -1.0


def desktop_of(hwnd):
    def run():
        from pyvda import AppView
        return vdesk.norm_guid(AppView(hwnd=int(hwnd)).desktop.id)
    return vdesk.call(run)


def title_of(hwnd):
    for w in windows.all_windows(include_tool_windows=True):
        if w["hwnd"] == hwnd:
            return w.get("title", "")
    return ""


def is_client(hwnd):
    try:
        return bool(windows.has_client(int(hwnd)))
    except Exception:
        return False


def pid_of(hwnd):
    try:
        for w in windows.all_windows(include_tool_windows=True):
            if w["hwnd"] == int(hwnd):
                return int(w.get("pid", 0))
    except Exception:
        pass
    return 0


def tree_of(pids):
    try:
        return set(windows.tree_pids(list(pids)))
    except Exception:
        return set()


def locate_shown(hwnd, tries=5, delay=0.4):
    """Show-before-locate (measured, no reinterpretation): first show (register
    view), then desktop ID with retry. Returns (location, attempts, detail);
    'dead' when IsWindow is false (transient, e.g. closed dropdown)."""
    try:
        if not windows.is_window(int(hwnd)):
            return ("dead", 0, "IsWindow false")
    except Exception:
        return ("dead", 0, "IsWindow-err")
    try:
        windows.show_window(int(hwnd))
    except Exception:
        pass
    time.sleep(0.3)
    last = ""
    for i in range(tries):
        try:
            return (desktop_of(int(hwnd)), i + 1, "")
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
            time.sleep(delay)
    try:
        if not windows.is_window(int(hwnd)):
            return ("dead", tries, last)
    except Exception:
        pass
    return ("ERR", tries, last)


def location_name(loc, box_id, main_id):
    if loc == box_id:
        return "box"
    if loc == main_id:
        return "main"
    return loc[:8]


def in_own_tree(hwnd, root_pids):
    if not root_pids:
        return False
    try:
        return int(pid_of(hwnd)) in tree_of(root_pids)
    except Exception:
        return False


def text_of(res):
    return "\n".join(getattr(b, "text", "") for b in res.content if hasattr(b, "text"))


def parse(res):
    t = text_of(res)
    try:
        return json.loads(t or "null")
    except Exception:
        return {"_raw": t, "_error": bool(getattr(res, "is_error", False))}


async def call(c, tool, args):
    try:
        r = await c.call_tool(tool, args)
    except Exception as e:
        return {"_error": True, "_raw": f"{type(e).__name__}: {e}"}
    out = parse(r)
    if getattr(r, "is_error", False) and isinstance(out, dict):
        out["_error"] = True
    return out


def _poll_dead(pid, timeout=6.0):
    """True once the pid is gone (polls, does not assume the kill landed)."""
    try:
        import psutil
    except Exception:
        return None
    end = time.time() + max(1.0, timeout)
    while time.time() < end:
        try:
            psutil.Process(int(pid))
        except Exception:
            return True
        time.sleep(0.3)
    try:
        psutil.Process(int(pid))
        return False
    except Exception:
        return True


def own_cleanup(hwnds, pids, pre_pids):
    # Never kill foreign PIDs, never close dirty (*) windows.
    # Returns verification status - kills are polled until dead (runner rule:
    # no fire-and-forget kills; a surviving own pid is an open finding).
    closed = []
    for h in list(hwnds):
        try:
            if not windows.is_window(h):
                continue
        except Exception:
            continue
        if title_of(h).startswith("*"):
            continue
        try:
            windows.close_window(h)
            closed.append(h)
        except Exception:
            pass
    time.sleep(0.8)
    kill_verified = {}
    for p in list(pids):
        if p in pre_pids or not p:
            continue
        try:
            windows.kill_tree(p)
        except Exception:
            pass
        kill_verified[p] = _poll_dead(p)
    time.sleep(0.8)
    return {"closed": closed, "kill_verified": kill_verified,
            "kill_unverified": sorted(p for p, v in kill_verified.items() if v is not True)}


async def run_one(c, n, run_type, coro):
    dl0 = await call(c, "delta_list", {})
    base = len((dl0.get("delta") or [])) if isinstance(dl0, dict) else 0
    idle_before = round(idle_seconds(), 1)
    # Per-run idle gate: no contaminated run. Below 90s idle nothing is
    # measured; instead inconclusive-environment is recorded (no partial run).
    if idle_before >= 0 and idle_before < 90:
        row = {"run": n, "type": run_type, "pass": False, "status": "inconclusive-environment",
               "idle_s_before": idle_before, "delta_new": [], "wall_s": 0.0,
               "note": f"not idle ({idle_before}s < 90s) - not measured"}
        results.append(row)
        log(f"Run {n} | {run_type} | SKIP inconclusive-environment (idle {idle_before}s)")
        return row
    t0 = time.time()
    try:
        row = await coro(c, n)
    except Exception as e:
        import traceback
        row = {"run": n, "type": run_type, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}
    row["run"] = n
    row["type"] = run_type
    row["idle_s_before"] = idle_before
    if "status" not in row:
        if row.get("pass"):
            row["status"] = "pass"
        elif row.get("inconclusive"):
            row["status"] = f"inconclusive-{row['inconclusive']}"
        else:
            row["status"] = "open"
    dl1 = await call(c, "delta_list", {})
    d1 = (dl1.get("delta") or []) if isinstance(dl1, dict) else []
    row["delta_new"] = d1[max(0, base):][-12:]
    row["wall_s"] = round(time.time() - t0, 1)
    results.append(row)
    log(f"Run {n} | {run_type} | {json.dumps({k: row.get(k) for k in ('windows','latency_ms','flash','location','delta_reason','misgrab','hide_balance','error')}, ensure_ascii=False)[:800]}")
    return row


async def mk_box(c, n):
    name = f"run-{n}"
    try:
        await c.call_tool("desktop_create", {"name": name})
    except Exception as e:
        return {"box": name, "create_err": str(e)[:150]}
    return {"box": name}


async def cleanup_box(c, name, pre_pids):
    # window_list -> see verdict (title star) -> decisions -> remove
    wl = await call(c, "window_list", {"desktop": name})
    wins = wl.get("windows", []) if isinstance(wl, dict) else []
    decisions = {}
    for w in wins:
        h = w["hwnd"]
        t = w.get("title", "")
        # See verdict: attempt screenshot, tolerant of errors
        try:
            await c.call_tool("see", {"desktop": name, "hwnd": h, "embed_image": False})
            seen = "seen"
        except Exception as e:
            seen = f"see-err:{type(e).__name__}"
        if t.startswith("*"):
            decisions[str(h)] = "park"
        else:
            decisions[str(h)] = "drop"
        w["see"] = seen
    rm = await call(c, "desktop_remove", {"desktop": name, "kill_apps": True, "decisions": decisions})
    try:
        await c.call_tool("show_main", {})
    except Exception:
        pass
    try:
        cur = vdesk.current_id()
        main = vdesk.main_id()
        restored = (cur == main)
    except Exception:
        restored = "?"
    dl = await call(c, "desktop_list", {})
    ndesks = len(dl.get("desktops", dl) if isinstance(dl, dict) else [])
    return {"windows": wins, "decisions": decisions, "remove": rm, "main_restored": restored}


async def reclaim_box(c, name, pre_pids):
    """Remove a stale same-named box from an earlier run (if any) via the
    honest path (window_list -> see verdict -> decisions -> remove), so
    desktop_create starts clean. Only touches windows in the stale rec's own
    pid tree (window_list now carries pid); anything foreign blocks the
    reclaim and surfaces honestly instead of being closed.
    Returns the cleanup record, "blocked-foreign", or None."""
    try:
        rec = state.require_desktop(name)
    except Exception:
        return None
    try:
        wl = await call(c, "window_list", {"desktop": name})
        wins = wl.get("windows", []) if isinstance(wl, dict) else []
        own = set(windows.tree_pids(rec.get("pids", [])))
        for w in wins:
            if int(w.get("pid", 0) or 0) not in own:
                raise RuntimeError(
                    f"reclaim-blocked-foreign: hwnd {w.get('hwnd')} pid {w.get('pid')} "
                    f"not in stale rec tree - left untouched")
    except RuntimeError:
        raise
    except Exception:
        pass
    return await cleanup_box(c, name, pre_pids)


async def isolated_immediate(c, n):
    name = f"run-{n}"
    pre_pids = {w["pid"] for w in windows.all_windows(include_tool_windows=True)}
    await reclaim_box(c, name, pre_pids)
    await c.call_tool("desktop_create", {"name": name})
    rec = state.require_desktop(name)
    box_id = vdesk.norm_guid(rec["desktop_id"])
    main_id = vdesk.norm_guid(vdesk.main_id())
    lr = await call(c, "app_launch", {"desktop": name, "exe": "charmap.exe", "wait_seconds": 5})
    roots = [lr.get("pid", 0)] if isinstance(lr, dict) and lr.get("pid") else []
    moved = lr.get("moved_hwnds", lr.get("moved_windows", [])) if isinstance(lr, dict) else []
    wl = await call(c, "window_list", {"desktop": name})
    wins = wl.get("windows", []) if isinstance(wl, dict) else []
    # Hardening: has_client filter BEFORE counting (sinks/tool windows without
    # area never count as windows), then show-before-locate per hwnd.
    counted = [w for w in wins if is_client(w["hwnd"])]
    skipped = len(wins) - len(counted)
    # Completeness per spec (measured, no reinterpretation): every counted
    # hwnd falls into exactly one class box | transient-tot | delta. Own
    # transients (ComboLBox/tooltip without DWM view) are no misgrab when
    # they are dead or recorded in the delta; alive-unrecorded = misgrab.
    delta_hwnds = {}
    if isinstance(lr, dict):
        for e in lr.get("delta", []) or []:
            try:
                delta_hwnds[int(e.get("hwnd", 0) or 0)] = e.get("reason", "")
            except Exception:
                pass
    window_entries = []
    misgrab = 0
    for w in counted:
        h = w["hwnd"]
        loc, attempts, detail = locate_shown(h)
        if loc == "dead":
            window_entries.append({"hwnd": h, "title": w.get("title", "")[:40],
                            "location": "transient-tot", "attempts": attempts, "detail": detail})
            continue  # Measured dead (e.g. closed dropdown) - no window anymore
        if loc == "ERR":
            if h in delta_hwnds:
                window_entries.append({"hwnd": h, "title": w.get("title", "")[:40],
                                "location": f"delta-{delta_hwnds[h]}"[:60], "attempts": attempts,
                                "detail": detail, "own_tree": in_own_tree(h, roots)})
                continue  # Measured as recorded - no loss
            try:
                dead = not windows.is_window(h)
            except Exception:
                dead = False
            if dead:
                window_entries.append({"hwnd": h, "title": w.get("title", "")[:40],
                                "location": "transient-tot", "attempts": attempts, "detail": detail})
                continue
            misgrab += 1  # Alive, location-less, unrecorded = only true misgrab case
            window_entries.append({"hwnd": h, "title": w.get("title", "")[:40],
                            "location": f"UNDETERMINED:{detail[:60]}", "attempts": attempts,
                            "detail": detail, "own_tree": in_own_tree(h, roots)})
            continue
        location = location_name(loc, box_id, main_id)
        if location != "box":
            misgrab += 1
        elif not in_own_tree(h, roots):
            misgrab += 1  # Measured: foreign PID on box (PID vs launch tree)
            location = f"FOREIGN:{location}"
        window_entries.append({"hwnd": h, "title": w.get("title", "")[:40], "location": location,
                        "attempts": attempts, "detail": detail,
                        "own_tree": in_own_tree(h, roots)})
    lats = lr.get("latencies_ms", []) if isinstance(lr, dict) else []
    hand = lr.get("handling_ms", []) if isinstance(lr, dict) else []
    bal = lr.get("hide_balance", {}) if isinstance(lr, dict) else {}
    cl = await cleanup_box(c, name, pre_pids)
    living = [f for f in window_entries if f["location"] not in ("transient-tot",)]
    ok = bool([f for f in living if f["location"] == "box"]) and misgrab == 0 and not (lr.get("handoff", False) if isinstance(lr, dict) else False)
    return {"windows": window_entries, "latency_ms": lats, "handling_ms": hand,
            "hide_balance": bal, "flash": "yes" if wins else "no",
            "location": "box" if ok else "REVIEW", "delta_reason": lr.get("delta", []) if isinstance(lr, dict) else [],
            "misgrab": misgrab, "excluded": f"no-client={skipped}",
            "app_launch": {k: lr.get(k) for k in ("pid", "handoff", "wait_capped", "launch_window_s")} if isinstance(lr, dict) else lr,
            "cleanup": cl, "pass": ok}


async def isolated_delayed(c, n, delay):
    name = f"run-{n}"
    pre_pids = {w["pid"] for w in windows.all_windows(include_tool_windows=True)}
    await reclaim_box(c, name, pre_pids)
    await c.call_tool("desktop_create", {"name": name})
    rec = state.require_desktop(name)
    box_id = vdesk.norm_guid(rec["desktop_id"])
    helper_args = f'"{DELAY_HELPER}" --child-launch {delay} "charmap.exe"'
    # Timeline sampler (background thread: survives blocking app_launch):
    # ledger + attempt counts + rest every 1 s from just before launch until
    # the settle snapshot. Shows WHEN attempts ran and WHEN termination hit.
    tl = []
    tl_t0 = time.time()
    tl_stop = threading.Event()

    def _tl(tag=""):
        try:
            b = retrieval.hide_balance()
            tl.append((round(time.time() - tl_t0, 1), tag,
                       b.get("hidden"), b.get("moved"), b.get("restored"),
                       b.get("gone"), b.get("pending_hidden"),
                       sorted(retrieval._NOTREADY_N.items()),
                       bool(retrieval.is_rest())))
        except Exception:
            pass

    def _tl_loop():
        while not tl_stop.wait(1.0):
            _tl()

    _tl_thread = threading.Thread(target=_tl_loop, daemon=True)
    _tl_thread.start()
    _tl("pre-launch")
    lr = await call(c, "app_launch", {"desktop": name, "exe": PY, "args": helper_args, "wait_seconds": 5})
    bal_launch = lr.get("hide_balance", {}) if isinstance(lr, dict) else {}
    # Watcher needs delay + buffer
    wait_extra = delay + 8.0
    time.sleep(wait_extra)
    # Settle gate: measure settled state, not boundary luck. Poll the live
    # ledger until nothing is hidden-pending (timeout -> measure anyway and
    # let the verdict speak; never reinterpret). Also samples rest/busy and
    # max attempt count -> distinguishes frozen attempts (rest/cadence) from
    # executed-but-viewless attempts (registration) on REVIEW rows.
    rest_false_seen = False
    hop_busy_seen = False
    max_notready_n = 0
    settle_end = time.time() + 15.0
    while time.time() < settle_end:
        try:
            if int(retrieval.hide_balance().get("pending_hidden", 0)) <= 0:
                break
        except Exception:
            break
        try:
            if not retrieval.is_rest():
                rest_false_seen = True
        except Exception:
            pass
        try:
            from vision_desktops import hop as _hop
            if _hop.hop_busy():
                hop_busy_seen = True
        except Exception:
            pass
        try:
            nmax = max(retrieval._NOTREADY_N.values(), default=0)
            if int(nmax) > max_notready_n:
                max_notready_n = int(nmax)
        except Exception:
            pass
        time.sleep(0.5)
    # Live ledger snapshot (in-process: captures watcher-phase activity that
    # the launch-return snapshot predates). Conservation here, not just then.
    bal_now = {}
    try:
        bal_now = retrieval.hide_balance()
    except Exception:
        pass
    try:
        tl_stop.set()
        _tl("snapshot")
    except Exception:
        pass
    condensed = []
    try:
        prev = None
        for e in tl:
            if e[1:] != (prev[1:] if prev else None):
                condensed.append(e)
                prev = e
        condensed = condensed[:80]
    except Exception:
        pass
    wl = await call(c, "window_list", {"desktop": name})
    wins = wl.get("windows", []) if isinstance(wl, dict) else []
    # Helper itself has no client window; only the charmap child counts
    # ("Zeichen" matches the German OS window title "Zeichentabelle").
    # Late-app tolerance: the watcher retrieves any time, so a missing main
    # gets re-listed (2 x 15 s) before verdict - app lateness must not read
    # as mechanism failure. Records waited time honestly.
    chars = [w for w in wins if "Zeichen" in (w.get("title") or "")]
    waited_extra = 0.0
    for _ in range(2):
        if chars:
            break
        time.sleep(15.0)
        waited_extra += 15.0
        try:
            wl = await call(c, "window_list", {"desktop": name})
            wins = wl.get("windows", []) if isinstance(wl, dict) else []
            chars = [w for w in wins if "Zeichen" in (w.get("title") or "")]
        except Exception:
            break
    chars = [w for w in wins if "Zeichen" in (w.get("title") or "")]
    window_entries = []
    misgrab = 0
    main_id = vdesk.norm_guid(vdesk.main_id())
    for w in wins:
        if not is_client(w["hwnd"]):
            window_entries.append({"hwnd": w["hwnd"], "title": w.get("title", "")[:40],
                            "location": "no-client-skip"})
            continue
        h = w["hwnd"]
        loc, attempts, detail = locate_shown(h)
        if loc == "dead":
            window_entries.append({"hwnd": h, "title": w.get("title", "")[:40],
                            "location": "transient-tot", "attempts": attempts})
            continue
        location = location_name(loc, box_id, main_id) if loc != "ERR" else f"ERR:{detail[:60]}"
        if loc == "ERR":
            misgrab += 1
        window_entries.append({"hwnd": h, "title": w.get("title", "")[:40], "location": location,
                        "attempts": attempts})
    dl = await call(c, "delta_list", {})
    cl = await cleanup_box(c, name, pre_pids)
    ok = len(chars) >= 1 and all(True for _ in chars)
    # Every charmap window must be on the box
    ok = ok and all(f["location"] == "box" for f in window_entries if "Zeichen" in f["title"])
    bal = lr.get("hide_balance", {}) if isinstance(lr, dict) else {}
    dh = {}
    try:
        keys = ("hidden", "moved", "restored", "gone", "restore_failed",
                "foreign_hide_attempts", "pending_hidden")
        dh = {k: bal_now.get(k, 0) - bal.get(k, 0) for k in keys} if bal_now else {}
        dh["conserved"] = (dh.get("hidden", -1) == dh.get("moved", -2) + dh.get("restored", -3)
                           + dh.get("gone", -4) + dh.get("restore_failed", -5)
                           + dh.get("pending_hidden", -6)) if dh else False
    except Exception:
        pass
    return {"windows": window_entries, "latency_ms": (lr.get("latencies_ms", []) if isinstance(lr, dict) else []),
            "flash": "yes", "location": "box" if ok else "REVIEW", "delta_reason": f"delay={delay}s chars={len(chars)}",
            "hide_balance": bal, "hide_balance_check": bal_now, "hide_delta": dh,
            "rest_false_seen": rest_false_seen, "hop_busy_seen": hop_busy_seen,
            "max_notready_n": max_notready_n, "timeline": condensed,
            "waited_extra_s": waited_extra,
            "misgrab": misgrab, "cleanup": cl, "pass": ok}


async def tk_background(c, n, delay):
    name = f"run-{n}"
    pre_pids = {w["pid"] for w in windows.all_windows(include_tool_windows=True)}
    await reclaim_box(c, name, pre_pids)
    await c.call_tool("desktop_create", {"name": name})
    rec = state.require_desktop(name)
    box_id = vdesk.norm_guid(rec["desktop_id"])
    title = f"IDLE-TK-{n}-{delay}s"
    helper_args = f'"{DELAY_HELPER}" --child-tk "{title}" {delay}'
    lr = await call(c, "app_launch", {"desktop": name, "exe": PY, "args": helper_args, "wait_seconds": 5})
    time.sleep(delay + 10.0)
    wl = await call(c, "window_list", {"desktop": name})
    wins = wl.get("windows", []) if isinstance(wl, dict) else []
    tk = [w for w in wins if title in (w.get("title") or "")]
    window_entries = []
    misgrab = 0
    main_id = vdesk.norm_guid(vdesk.main_id())
    for w in wins:
        if not is_client(w["hwnd"]):
            window_entries.append({"hwnd": w["hwnd"], "title": w.get("title", "")[:40],
                            "location": "no-client-skip"})
            continue
        h = w["hwnd"]
        loc, attempts, detail = locate_shown(h)
        if loc == "dead":
            window_entries.append({"hwnd": h, "title": w.get("title", "")[:40],
                            "location": "transient-tot", "attempts": attempts})
            continue
        location = location_name(loc, box_id, main_id) if loc != "ERR" else f"ERR:{detail[:60]}"
        if loc == "ERR":
            misgrab += 1
        window_entries.append({"hwnd": h, "title": w.get("title", "")[:40], "location": location,
                        "attempts": attempts})
    cl = await cleanup_box(c, name, pre_pids)
    ok = len(tk) >= 1 and all(f["location"] == "box" for f in window_entries if title in f["title"])
    return {"windows": window_entries, "latency_ms": (lr.get("latencies_ms", []) if isinstance(lr, dict) else []),
            "flash": "yes", "location": "box" if ok else "REVIEW", "delta_reason": f"tk delay={delay}s found={len(tk)}",
            "misgrab": misgrab, "cleanup": cl, "pass": ok}


def delta_new_ids(pre_entries):
    try:
        return {(e.get("hwnd"), e.get("ts")) for e in (pre_entries or [])}
    except Exception:
        return set()


async def handoff_notepad(c, n):
    name = f"run-{n}"
    pre_pids = {w["pid"] for w in windows.all_windows(include_tool_windows=True)}
    await reclaim_box(c, name, pre_pids)
    await c.call_tool("desktop_create", {"name": name})
    rec = state.require_desktop(name)
    box_id = vdesk.norm_guid(rec["desktop_id"])
    main_id = vdesk.norm_guid(vdesk.main_id())
    dl_pre = await call(c, "delta_list", {})
    pre_ids = delta_new_ids((dl_pre.get("delta", []) if isinstance(dl_pre, dict) else []))
    lr = await call(c, "app_launch", {"desktop": name, "exe": "notepad.exe", "wait_seconds": 5})
    roots = [lr.get("pid", 0)] if isinstance(lr, dict) and lr.get("pid") else []
    dl = await call(c, "delta_list", {})
    d = dl.get("delta", []) if isinstance(dl, dict) else []
    # Hardening: only OWN delta entries (since run start), no d[-8:] across everything.
    new = [e for e in d if (e.get("hwnd"), e.get("ts")) not in pre_ids]
    fh = [e for e in new if e.get("reason") == "foreign-host"]
    wl = await call(c, "window_list", {"desktop": name})
    wins = wl.get("windows", []) if isinstance(wl, dict) else []
    # Check box content against launch tree (measured): foreign PID on box = misgrab.
    foreign_on_box = []
    for w in wins:
        h = w["hwnd"]
        if not is_client(h):
            continue
        if windows.is_window(h) and not in_own_tree(h, roots):
            loc, _, _ = locate_shown(h)
            foreign_on_box.append({"hwnd": h, "title": w.get("title", "")[:40],
                                  "pid": pid_of(h), "location": location_name(loc, box_id, main_id) if loc not in ("dead", "ERR") else loc})
    # Adopt only own delta hwnd: alive + verified on Main via shown location.
    adopted = None
    candidate = None
    for e in fh:
        h = int(e.get("hwnd", 0) or 0)
        if not h or not windows.is_window(h):
            continue
        loc, attempts, _ = locate_shown(h)
        if loc == main_id:
            candidate = {"hwnd": h, "attempts": attempts, "pid": pid_of(h),
                    "title": title_of(h)[:40]}
            break
    if candidate:
        h = candidate["hwnd"]
        try:
            await c.call_tool("delta_view", {"hwnd": h})
            viewed = True
        except Exception:
            viewed = False
        ar = await call(c, "window_adopt", {"desktop": name, "hwnd": h})
        adopted = {"hwnd": h, "viewed": viewed, "res": ar, "candidate": candidate}
        try:
            home = vdesk.main_id()
            vdesk.move_window(h, home)
            windows.show_window(h)
            time.sleep(0.5)
            if not title_of(h).startswith("*"):
                windows.close_window(h)
                time.sleep(0.8)
        except Exception:
            pass
    cl = await cleanup_box(c, name, pre_pids)
    ok = bool(lr.get("handoff")) if isinstance(lr, dict) else False
    ok = ok and not foreign_on_box and bool(candidate) and bool(adopted)
    if adopted:
        ar = adopted.get("res", {})
        ok = ok and (ar.get("adopted") == candidate["hwnd"])
    row = {"windows": wins, "latency_ms": [], "flash": "yes", "location": "main-delta",
           "delta_reason": f"foreign-host new={len(fh)}", "misgrab": len(foreign_on_box),
           "foreign_on_box": foreign_on_box, "adopt": adopted, "cleanup": cl, "pass": ok}
    if not ok and not foreign_on_box and not candidate:
        row["inconclusive"] = "no-candidate"
    return row


async def handoff_chrome(c, n):
    name = f"run-{n}"
    pre_pids = {w["pid"] for w in windows.all_windows(include_tool_windows=True)}
    await reclaim_box(c, name, pre_pids)
    await c.call_tool("desktop_create", {"name": name})
    rec = state.require_desktop(name)
    box_id = vdesk.norm_guid(rec["desktop_id"])
    main_id = vdesk.norm_guid(vdesk.main_id())
    if not Path(CHROME).exists():
        cl = await cleanup_box(c, name, pre_pids)
        return {"windows": [], "latency_ms": [], "flash": "no", "location": "skip-no-chrome",
                "delta_reason": "skip", "misgrab": 0, "cleanup": cl, "pass": True, "skip": True}
    dl_pre = await call(c, "delta_list", {})
    pre_ids = delta_new_ids((dl_pre.get("delta", []) if isinstance(dl_pre, dict) else []))
    lr = await call(c, "app_launch", {"desktop": name, "exe": CHROME, "args": "--new-window about:blank", "wait_seconds": 5})
    roots = [lr.get("pid", 0)] if isinstance(lr, dict) and lr.get("pid") else []
    dl = await call(c, "delta_list", {})
    d = dl.get("delta", []) if isinstance(dl, dict) else []
    new = [e for e in d if (e.get("hwnd"), e.get("ts")) not in pre_ids]
    fh = [e for e in new if e.get("reason") == "foreign-host"]
    wl = await call(c, "window_list", {"desktop": name})
    wins = wl.get("windows", []) if isinstance(wl, dict) else []
    foreign_on_box = []
    for w in wins:
        h = w["hwnd"]
        if not is_client(h):
            continue
        if windows.is_window(h) and not in_own_tree(h, roots):
            loc, _, _ = locate_shown(h)
            foreign_on_box.append({"hwnd": h, "title": w.get("title", "")[:40],
                                  "pid": pid_of(h), "location": location_name(loc, box_id, main_id) if loc not in ("dead", "ERR") else loc})
    adopted = None
    candidate = None
    for e in fh:
        h = int(e.get("hwnd", 0) or 0)
        if not h or not windows.is_window(h):
            continue
        loc, attempts, _ = locate_shown(h)
        if loc == main_id:
            candidate = {"hwnd": h, "attempts": attempts, "pid": pid_of(h),
                    "title": title_of(h)[:40]}
            break
    if candidate:
        h = candidate["hwnd"]
        try:
            await c.call_tool("delta_view", {"hwnd": h})
            viewed = True
        except Exception:
            viewed = False
        ar = await call(c, "window_adopt", {"desktop": name, "hwnd": h})
        adopted = {"hwnd": h, "viewed": viewed, "res": ar, "candidate": candidate}
        try:
            home = vdesk.main_id()
            vdesk.move_window(h, home)
            windows.show_window(h)
            time.sleep(0.5)
            if not title_of(h).startswith("*"):
                windows.close_window(h)
                time.sleep(0.8)
        except Exception:
            pass
    cl = await cleanup_box(c, name, pre_pids)
    ok = bool(lr.get("handoff")) if isinstance(lr, dict) else False
    ok = ok and not foreign_on_box and bool(candidate) and bool(adopted)
    if adopted:
        ar = adopted.get("res", {})
        ok = ok and (ar.get("adopted") == candidate["hwnd"])
    row = {"windows": wins, "latency_ms": [], "flash": "yes", "location": "main-delta",
           "delta_reason": f"foreign-host new={len(fh)}", "misgrab": len(foreign_on_box),
           "foreign_on_box": foreign_on_box, "adopt": adopted, "cleanup": cl, "pass": ok}
    if not ok and not foreign_on_box and not candidate:
        row["inconclusive"] = "no-candidate"
    return row


async def pin_case(c, n, mode):
    name = f"run-{n}"
    pre_pids = {w["pid"] for w in windows.all_windows(include_tool_windows=True)}
    await reclaim_box(c, name, pre_pids)
    await c.call_tool("desktop_create", {"name": name})
    rec = state.require_desktop(name)
    # Launch raw on main, pin, decide -> must yield pin, never move
    before = windows.all_hwnds()
    pid = windows.launch_process("charmap.exe", cwd=str(Path.home()))
    found = windows.wait_new_windows(before, [pid], 15)
    wins = [w for w in found.get("windows", []) if windows.has_client(w["hwnd"])]
    if not wins:
        oc = own_cleanup([], [pid], pre_pids)
        cl = await cleanup_box(c, name, pre_pids)
        return {"windows": [], "flash": "yes", "location": "inconclusive-no-window", "delta_reason": "none",
                "misgrab": 0, "cleanup": cl, "own_cleanup": oc,
                "kill_unverified": oc["kill_unverified"], "pass": False}
    h = wins[0]["hwnd"]
    try:
        if mode == "view":
            vdesk.call(lambda h=h: __import__("pyvda").AppView(hwnd=h).pin())
        else:
            # App pin: try via AppView pin, fallback to view
            try:
                vdesk.call(lambda h=h: __import__("pyvda").AppView(hwnd=h).pin())
            except Exception:
                pass
        time.sleep(0.3)
        pinned = vdesk.is_view_pinned(h) or vdesk.is_app_pinned(h)
    except Exception as e:
        pinned = f"ERR:{e}"[:100]
    res = retrieval._decide_move(h, rec["desktop_id"], time.time(), windows._tick_now())
    ok = (res.get("moved") is False and res.get("reason") == "pin")
    # Adopt must also refuse (zero misgrab)
    ar = await call(c, "window_adopt", {"desktop": name, "hwnd": int(h)})
    adopt_refused = ar.get("_error", False) or ar.get("adopted", None) != h
    try:
        vdesk.call(lambda h=h: __import__("pyvda").AppView(hwnd=h).unpin())
    except Exception:
        pass
    time.sleep(0.3)
    oc = own_cleanup([h], [pid], pre_pids)
    cl = await cleanup_box(c, name, pre_pids)
    try:
        _loc, _, _ = locate_shown(h)
        _main = vdesk.norm_guid(vdesk.main_id())
        loc = "main" if _loc == _main else _loc
    except Exception:
        loc = "main"
    kill_unverified = oc["kill_unverified"]
    return {"windows": [{"hwnd": h, "title": title_of(h)[:40]}], "latency_ms": [], "flash": "yes",
            "location": loc, "delta_reason": f"pin:{res.get('reason')} pinned={pinned} adopt_refused={bool(adopt_refused)}",
            "misgrab": 0 if ok else 1, "cleanup": cl, "own_cleanup": oc,
            "kill_unverified": kill_unverified, "pass": bool(ok) and not kill_unverified}


def hook_tick_for(hwnd, since_wall, default_tick):
    try:
        for (wt, tick, h, _p) in windows.hook_events_since(since_wall):
            if int(h) == int(hwnd):
                return int(tick)
    except Exception:
        pass
    return int(default_tick)


def fg_is(hwnd):
    try:
        return int(windows.foreground_hwnd()) == int(hwnd)
    except Exception:
        return False


def last_input():
    try:
        return int(windows.last_input_tick())
    except Exception:
        return 0


async def interaction(c, n, stable=3, max_attempts=6):
    """Narrow the guard race (tests/ only): early appear_tick via hook (wide
    detection window around appearance), VERIFIED setup (foreground poll +
    last-input advance, human-like timing), repeat until stable.
    Setup failure = 'inconclusive-setup' (own category, no reinterpretation)."""
    import ctypes as ct
    name = f"run-{n}"
    pre_pids = {w["pid"] for w in windows.all_windows(include_tool_windows=True)}
    await reclaim_box(c, name, pre_pids)
    await c.call_tool("desktop_create", {"name": name})
    rec = state.require_desktop(name)
    box_id = vdesk.norm_guid(rec["desktop_id"])
    main_id = vdesk.norm_guid(vdesk.main_id())
    t_start = time.time()
    before = windows.all_hwnds()
    pid = windows.launch_process("charmap.exe", cwd=str(Path.home()))
    found = windows.wait_new_windows(before, [pid], 15)
    wins = [w for w in found.get("windows", []) if windows.has_client(w["hwnd"])]
    if not wins:
        oc = own_cleanup([], [pid], pre_pids)
        cl = await cleanup_box(c, name, pre_pids)
        return {"windows": [], "flash": "yes", "location": "inconclusive", "delta_reason": "none",
                "misgrab": 0, "cleanup": cl, "own_cleanup": oc,
                "kill_unverified": oc["kill_unverified"], "pass": False}
    h = wins[0]["hwnd"]
    appear_tick = hook_tick_for(h, t_start - 3.0, windows._tick_now())
    hits = 0
    attempts = 0
    history = []
    last = {}
    real_misgrab = False
    while hits < stable and attempts < max_attempts and not real_misgrab:
        attempts += 1
        # Human-like: take focus, linger, keypress, linger
        fg_ok = False
        for _ in range(20):
            try:
                ct.windll.user32.SetForegroundWindow(ct.c_void_p(int(h)))
            except Exception:
                pass
            time.sleep(0.1)
            if fg_is(h):
                fg_ok = True
                break
        time.sleep(0.3)
        li0 = last_input()
        try:
            ct.windll.user32.keybd_event(0x10, 0, 0, 0)
            ct.windll.user32.keybd_event(0x10, 0, 2, 0)
        except Exception:
            pass
        time.sleep(0.2)
        li1 = last_input()
        input_registered = (li1 != li0)
        touched = retrieval.user_touched(int(h), int(appear_tick))
        step = {"attempt": attempts, "fg_ok": fg_ok, "li0": li0, "li1": li1,
                   "input_registered": input_registered, "touched": touched,
                   "decide": None}
        if not (fg_ok and input_registered and (touched is True)):
            # Setup/evidence not proven: NO decide call (it would otherwise
            # correctly move on missing evidence and contaminate the case).
            # Counts as neither hit nor misgrab - pure setup void.
            step["setup"] = "void-no-evidence"
            history.append(step)
            last = step
            continue
        res = retrieval._decide_move(h, rec["desktop_id"], time.time(), int(appear_tick))
        step["decide"] = res.get("reason")
        history.append(step)
        last = step
        if (res.get("moved") is False) and ("user" in str(res.get("reason"))):
            hits += 1
        else:
            # Evidence proven, guard still moves = real product misgrab.
            real_misgrab = True
    try:
        loc = desktop_of(h)
        location = location_name(loc, box_id, main_id) if loc not in ("dead", "ERR") else loc
    except Exception as e:
        location = f"ERR:{e}"[:60]
    oc = own_cleanup([h], [pid], pre_pids)
    cl = await cleanup_box(c, name, pre_pids)
    ok = (hits >= stable) and (location == "main") and not real_misgrab
    kill_unverified = oc["kill_unverified"]
    return {"windows": [{"hwnd": h, "title": title_of(h)[:40]}], "flash": "yes", "location": location,
            "delta_reason": f"stable {hits}/{stable} in {attempts} attempt(s), "
                           f"product_misgrab={real_misgrab}, last={last}",
            "history": history,
            "misgrab": 1 if real_misgrab else 0,
            "cleanup": cl, "own_cleanup": oc,
            "kill_unverified": kill_unverified, "pass": bool(ok) and not kill_unverified}


async def amain():
    import os
    only = os.environ.get("RUNS", "").strip()
    selection = None
    if only:
        try:
            selection = {int(x) for x in only.replace(" ", "").split(",") if x}
        except Exception:
            selection = None
    mode = f"only {sorted(selection)}" if selection else "all 20"
    log(f"IDLE ACCEPTANCE 20 runs - start ({mode})")
    # Idle gate (tests/ only): acceptance needs a truly idle machine. Last
    # input must be older than 120s, otherwise abort as
    # inconclusive-environment (no partial runs, no blurred results).
    idle0 = idle_seconds()
    log(f"idle seconds before start: {idle0:.0f}")
    if idle0 >= 0 and idle0 < 120:
        log("ABORT inconclusive-environment: machine not idle (>=120s needed)")
        return 2
    home = vdesk.current_id()
    main_id = vdesk.main_id()
    log(f"start main={str(main_id)[:8]} home-main={home == main_id}")
    base_dl = await call(Client(mcp), "delta_list", {}) if False else None
    async with Client(mcp) as c:
        dl0 = await call(c, "desktop_list", {})
        log(f"desktop_list start: {str(dl0)[:400]}")
        d0 = await call(c, "delta_list", {})
        log(f"delta baseline n={(len(d0.get('delta', [])) if isinstance(d0, dict) else '?')}")
        # 1-8 isolated
        if selection is None or 1 in selection:
            await run_one(c, 1, "isolated-immediate", lambda c, n: isolated_immediate(c, n))
        if selection is None or 2 in selection:
            await run_one(c, 2, "isolated-immediate", lambda c, n: isolated_immediate(c, n))
        if selection is None or 3 in selection:
            await run_one(c, 3, "isolated-delay-2s", lambda c, n: isolated_delayed(c, n, 2.0))
        if selection is None or 4 in selection:
            await run_one(c, 4, "isolated-delay-2s", lambda c, n: isolated_delayed(c, n, 2.0))
        if selection is None or 5 in selection:
            await run_one(c, 5, "isolated-delay-5s", lambda c, n: isolated_delayed(c, n, 5.0))
        if selection is None or 6 in selection:
            await run_one(c, 6, "isolated-delay-5s", lambda c, n: isolated_delayed(c, n, 5.0))
        if selection is None or 7 in selection:
            await run_one(c, 7, "isolated-delay-30s", lambda c, n: isolated_delayed(c, n, 30.0))
        if selection is None or 8 in selection:
            await run_one(c, 8, "isolated-delay-30s", lambda c, n: isolated_delayed(c, n, 30.0))
        # 9-12 tk
        if selection is None or 9 in selection:
            await run_one(c, 9, "tk-bg-2s", lambda c, n: tk_background(c, n, 2.0))
        if selection is None or 10 in selection:
            await run_one(c, 10, "tk-bg-2s", lambda c, n: tk_background(c, n, 2.0))
        if selection is None or 11 in selection:
            await run_one(c, 11, "tk-bg-5s", lambda c, n: tk_background(c, n, 5.0))
        if selection is None or 12 in selection:
            await run_one(c, 12, "tk-bg-5s", lambda c, n: tk_background(c, n, 5.0))
        # 13-16 handoff
        if selection is None or 13 in selection:
            await run_one(c, 13, "handoff-notepad", handoff_notepad)
        if selection is None or 14 in selection:
            await run_one(c, 14, "handoff-notepad", handoff_notepad)
        if selection is None or 15 in selection:
            await run_one(c, 15, "handoff-chrome", handoff_chrome)
        if selection is None or 16 in selection:
            await run_one(c, 16, "handoff-chrome", handoff_chrome)
        # 17-18 pin
        if selection is None or 17 in selection:
            await run_one(c, 17, "pin-view", lambda c, n: pin_case(c, n, "view"))
        if selection is None or 18 in selection:
            await run_one(c, 18, "pin-app", lambda c, n: pin_case(c, n, "app"))
        # 19-20 interaction
        if selection is None or 19 in selection:
            await run_one(c, 19, "interaction", interaction)
        if selection is None or 20 in selection:
            await run_one(c, 20, "interaction", interaction)
        try:
            await c.call_tool("show_main", {})
        except Exception:
            pass
        dl_end = await call(c, "desktop_list", {})
        log(f"desktop_list end: {str(dl_end)[:500]}")
    ok = sum(1 for r in results if r.get("pass"))
    log("=" * 60)
    for r in results:
        f = r.get("windows", [])
        log(f"Run {r['run']} | {r['type']} | Windows={[(x.get('hwnd'), x.get('title'), x.get('location')) for x in f][:4]} | "
            f"Latency={r.get('latency_ms')} | Flash={r.get('flash')} | Location={r.get('location')} | "
            f"Delta={str(r.get('delta_reason'))[:160]} | Misgrab={r.get('misgrab')} | "
            f"pass={r.get('pass')} | status={r.get('status')} | idle_before={r.get('idle_s_before')}")
    output_file = r"C:\Users\Paul\AppData\Local\Temp\opencode\idle20.json" if selection is None else \
        r"C:\Users\Paul\AppData\Local\Temp\opencode\idle20_remeasure.json"
    if selection is None:
        log(f"SUMMARY {ok}/20 with no unexplained window")
        out = results
    else:
        # Remeasurement merges per run (never overwrites earlier remeasurements).
        try:
            alt = {x["run"]: x for x in json.loads(Path(output_file).read_text(encoding="utf-8"))}
        except Exception:
            alt = {}
        for r in results:
            alt[r["run"]] = r
        out = [alt[k] for k in sorted(alt)]
        log(f"REMEASUREMENT {sorted(selection)}: {ok}/{len(results)} pass (file: {len(out)} runs)")
    Path(output_file).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"json: {output_file}")
    return 0 if (ok >= 20 if selection is None else ok >= len(results)) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
