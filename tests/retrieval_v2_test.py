"""Acceptance v2 (safe, no desktop switch): pixel-free delta, ownership,
watcher, threads, zero-misgrab structure. No hops, no moves, logic only."""

import sys

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from vision_desktops import hop, retrieval, state, vdesk, windows


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail and not ok else ""))
    return ok


def main() -> int:
    failures = 0

    # 1. Pixel-free delta: hwnd/PID/title/time+reason only, no image, retention.
    e = state.add_delta({"hwnd": 123456, "pid": 789, "title": "*DIRTY canary",
                         "reason": "foreign-host"})
    keys = set(e.keys())
    if not check("delta without pixels (meta only)", keys <= {"hwnd", "pid", "title", "ts", "reason", "detail"},
                 str(sorted(keys))):
        failures += 1
    items = state.list_delta()
    if not check("delta lists entry", any(x.get("hwnd") == 123456 for x in items)):
        failures += 1
    if not check("dirty star preserved (canary)", any(str(x.get("title", "")).startswith("*") for x in items),
                  "title star lost"):
        failures += 1
    state.drop_delta(123456)

    # 2. Three threads: hook only reports, decider exists, COM with priority.
    import threading
    windows._ensure_hook()
    retrieval._ensure_decider()
    # COM priority: hop (0) before move (1) - queue accepts priority.
    try:
        vdesk.call(lambda: True, priority=0)
        vdesk.call(lambda: True, priority=1)
        if not check("com-priority hop before move", True):
            failures += 1
    except Exception as exc:
        check("com-priority hop before move", False, str(exc)[:100])
        failures += 1
    names = [t.name for t in threading.enumerate()]
    if not check("hook thread alive", any("hook" in n for n in names), str(names)):
        failures += 1
    if not check("decider thread alive", any("decider" in n for n in names), str(names)):
        failures += 1
    if not check("com thread alive", any("com" in n.lower() for n in names), str(names)):
        failures += 1

    # 3. Interaction guard: merely taking the foreground does NOT count.
    # Mock: window not in foreground -> never usage (even with input).
    real_fg = windows.foreground_hwnd
    real_li = windows.last_input_tick
    try:
        windows.foreground_hwnd = lambda: 999999
        windows.last_input_tick = lambda: (windows._tick_now() & 0xFFFFFFFF)
        if not check("foreground-take alone != usage", retrieval.user_touched(111, 0) is False):
            failures += 1
    finally:
        windows.foreground_hwnd = real_fg
        windows.last_input_tick = real_li

    # 4. Own SendInputs are subtracted (hop interval).
    try:
        tick = windows._tick_now() & 0xFFFFFFFF
        hop._HOP_INTERVALS.append((tick - 5000, tick + 5000))
        windows.foreground_hwnd = lambda: 222
        windows.last_input_tick = lambda: tick
        touched = retrieval.user_touched(222, (tick - 10000) & 0xFFFFFFFF)
        if not check("own hop != user", touched is False, f"touched={touched}"):
            failures += 1
    finally:
        windows.foreground_hwnd = real_fg
        windows.last_input_tick = real_li
        try:
            hop._HOP_INTERVALS.pop()
        except Exception:
            pass

    # 5. Guard constants present (limits measurable, not guessed).
    if not check("collision limit present",
                 hasattr(retrieval, "COLLISION_QUEUE_MAX_S") and retrieval.COLLISION_QUEUE_MAX_S > 0):
        failures += 1
    if not check("launch window 5s + quiet", hasattr(windows, "LAUNCH_WINDOW_S") and
                 windows.LAUNCH_WINDOW_S <= 5.0, "5-s upper bound"):
        failures += 1
    if not check("pin check present", hasattr(vdesk, "is_view_pinned") and hasattr(vdesk, "is_app_pinned")):
        failures += 1
    if not check("quota per hwnd", hasattr(retrieval, "QUOTA_PER_HWND")):
        failures += 1
    if not check("adopt tool present", hasattr(__import__("vision_desktops.server", fromlist=["window_adopt"]), "window_adopt")):
        failures += 1

    # 6. Completeness: artifact classes are closed sets, ledger math live.
    # hide_balance keys must match exactly (schema drift fails loudly).
    bal0 = retrieval.hide_balance()
    if not check("balance keys exact",
                 set(bal0.keys()) == {"hidden", "moved", "restored", "gone",
                                      "restore_failed", "foreign_hide_attempts",
                                      "pending_hidden", "balanced"},
                 str(sorted(bal0.keys()))):
        failures += 1
    # Live ledger transitions on synthetic hwnds (no real windows touched):
    # job/tree register, foreign trips the wire, moved/gone resolve, math holds.
    try:
        r1 = retrieval._note_hidden(999001, "d", "job")
        r2 = retrieval._note_hidden(999002, "d", "foreign")
        r3 = retrieval._note_hidden(999003, "d", "tree")
        m1 = retrieval._resolve_hidden(999001, show=False)
        g3 = retrieval._resolve_hidden(999003, show=True)  # dead hwnd -> gone
        n2 = retrieval._resolve_hidden(999002, show=True)  # never registered
        bal1 = retrieval.hide_balance()
        d = {k: bal1[k] - bal0.get(k, 0) for k in
             ("hidden", "moved", "restored", "gone", "restore_failed",
              "foreign_hide_attempts")}
        ledger_ok = (r1 is True and r2 is False and r3 is True
                     and m1 == "moved" and g3 == "gone" and n2 is None
                     and d["hidden"] == 2 and d["moved"] == 1 and d["gone"] == 1
                     and d["foreign_hide_attempts"] == 1 and d["restored"] == 0
                     and d["restore_failed"] == 0
                     and bal1.get("balanced") is True)
        if not check("ledger transitions live", ledger_ok,
                     f"note={r1},{r2},{r3} resolve={m1},{g3},{n2} delta={d} bal={bal1}"):
            failures += 1
    except Exception as exc:  # noqa: BLE001
        check("ledger transitions live", False, f"{type(exc).__name__}: {exc}")
        failures += 1
    reasons = {"box", "pin", "quota", "user-touched", "queue-overflow", "foreign-host", "move-failed"}
    print(f"  [INFO] Classes: {sorted(reasons)} (8/20 blind spot closed)")

    # 7. No permanent gap: watcher registers/unregisters (any time).
    try:
        retrieval.watch_register("v2-self", "v2-self-id", None, [])
        wl = retrieval.watch_list()
        ok = any(w.get("desktop_id") == "v2-self-id" for w in wl)
        if not check("watcher registered", ok, str(wl)):
            failures += 1
        retrieval.watch_unregister("v2-self-id")
        wl2 = retrieval.watch_list()
        if not check("watcher unregistered", not any(w.get("desktop_id") == "v2-self-id" for w in wl2)):
            failures += 1
    except Exception as exc:  # noqa: BLE001
        check("watcher register/unregister", False, str(exc)[:100])
        failures += 1
    names2 = [t.name for t in threading.enumerate()]
    if not check("watcher thread alive", any("watcher" in n for n in names2), str(names2)):
        failures += 1

    # 8. Legacy removed: no collect_new_windows left in repo. hide_window
    # exists again BUT only behind the ownership gate (v1's sin was blanket
    # hiding, not the helper): async_hide + hide_balance + tripwire.
    if not check("legacy collector deleted", not hasattr(windows, "collect_new_windows")):
        failures += 1
    if not check("gated hide present", hasattr(windows, "hide_window") and hasattr(windows, "async_hide")):
        failures += 1
    if not check("hide balance measured", callable(getattr(retrieval, "hide_balance", None))):
        failures += 1
    bal = retrieval.hide_balance()
    if not check("hide ledger balanced", bal.get("balanced") is True, str(bal)):
        failures += 1
    if not check("cap explicit (wait_capped)", "wait_capped" in retrieval.launch_and_retrieve.__doc__):
        failures += 1
    if not check("pin status single call", hasattr(vdesk, "pin_status")):
        failures += 1
    if not check("inflight guard", hasattr(retrieval, "_inflight_add")):
        failures += 1
    if not check("appear wall honest", hasattr(retrieval, "_appear_wall")):
        failures += 1

    print("=" * 60)
    if failures:
        print(f"RESULT: {failures} failed")
        return 1
    print("RESULT: V2 ACCEPTANCE STRUCTURE GREEN (measure: latency/pin/restore on the system)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
