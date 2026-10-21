"""Acceptance measurement v2 (order): 1) Show->Move latency + flash on Main
+ hide/restore balance, 2) pin/restore, 3) parallel launches (no 45-s pile-up).
Uses charmap (multi-instance, fast, no dialogs).

Proven windows are hidden at discovery (ownership gate) and restored on every
non-move classification - the balance below is the measured restore guarantee.
No mouse/keyboard input needed, no hops - only launch/move via COM.
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from vision_desktops import retrieval, state, vdesk, windows


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail and not ok else ""))
    return ok


def desktop_of(hwnd: int) -> str:
    def run():
        from pyvda import AppView
        return vdesk.norm_guid(AppView(hwnd=int(hwnd)).desktop.id)
    return vdesk.call(run)


def nuke(name: str):
    try:
        rec = state.require_desktop(name)
    except Exception:
        return
    for pid in rec.get("pids", []):
        try:
            windows.kill_tree(pid)
        except Exception:  # noqa: BLE001
            pass
    for h in rec.get("hwnds", []):
        try:
            if windows.is_window(h):
                windows.close_window(h)
        except Exception:  # noqa: BLE001
            pass
    time.sleep(1.0)
    try:
        retrieval.watch_unregister(rec["desktop_id"])
    except Exception:  # noqa: BLE001
        pass
    try:
        vdesk.remove_desktop(rec["desktop_id"])
    except Exception:  # noqa: BLE001
        pass
    try:
        state.del_desktop(name)
    except Exception:  # noqa: BLE001
        pass


def launch_raw_charmap():
    before = windows.all_hwnds()
    pid = windows.launch_process("charmap.exe", cwd=str(Path.home()))
    found = windows.wait_new_windows(before, [pid], 10)
    wins = [w for w in found["windows"] if windows.has_client(w["hwnd"])]
    return pid, wins


def main() -> int:
    failures = 0
    home = vdesk.current_id()
    print("retrieval measurement v2 (latency, pin/restore, parallel)")
    print("=" * 60)

    # 1) Show->Move latency + flash (cost without hiding).
    name = "m-lat"
    nuke(name)
    try:
        info = vdesk.create_desktop(name)
        rec = {"name": name, "desktop_id": info["id"], "desktop_number": info["number"],
               "pids": [], "hwnds": []}
        state.put_desktop(rec)
        t0 = time.time()
        bal0 = retrieval.hide_balance()
        res = retrieval.launch_and_retrieve(info["id"], "charmap.exe", "", 5.0,
                                            cwd=str(Path.home()), desktop_name=name)
        wall = time.time() - t0
        for h in res.get("moved_hwnds", []):
            if h not in rec.get("hwnds", []):
                rec.setdefault("hwnds", []).append(h)
        rec["pids"].append(res.get("pid", 0))
        state.put_desktop(rec)
        lats = res.get("latencies_ms", [])
        hand = res.get("handling_ms", [])
        if not check("1) move landed", bool(res.get("moved_hwnds")), str(res)[:200]):
            failures += 1
        else:
            mx = max(lats) if lats else -1
            mh = max(hand) if hand else -1
            print(f"  [INFO] flash Show->Move: {['%.0f' % v for v in lats]} ms "
                  f"(incl. app init; proven windows hidden at discovery)")
            print(f"  [INFO] handling client->move: {['%.0f' % v for v in hand]} ms "
                  f"(goal <{retrieval.LATENCY_GOAL_MS:.0f} ms, our share)")
            if mh < 0 or mh > retrieval.LATENCY_GOAL_MS:
                check("1) handling <250ms", False, f"max={mh:.0f} ms")
                failures += 1
            for h in res.get("moved_hwnds", []):
                try:
                    loc = desktop_of(h)
                    if not check("1) restore on box", loc == vdesk.norm_guid(info["id"]),
                                 f"{loc[:8]}"):
                        failures += 1
                except Exception as exc:  # noqa: BLE001
                    check("1) restore readable", False, str(exc)[:100])
                    failures += 1
            # Restore guarantee: per-launch balance delta (hidden ==
            # moved+restored+gone+restore_failed, nothing left invisible).
            time.sleep(1.0)  # let async show/hide workers drain
            bal1 = retrieval.hide_balance()
            dh = {k: bal1.get(k, 0) - bal0.get(k, 0)
                  for k in ("hidden", "moved", "restored", "gone",
                            "restore_failed", "foreign_hide_attempts")}
            print(f"  [INFO] hide balance delta: {dh} "
                  f"pending={bal1.get('pending_hidden')}")
            if not check("1) hide balanced",
                         dh["hidden"] == dh["moved"] + dh["restored"] + dh["gone"] + dh["restore_failed"],
                         str(dh)):
                failures += 1
            if not check("1) no restore failures", dh["restore_failed"] == 0, str(dh)):
                failures += 1
            if not check("1) no foreign hides", dh["foreign_hide_attempts"] == 0, str(dh)):
                failures += 1
            if not check("1) nothing pending hidden",
                         bal1.get("pending_hidden", -1) == 0, str(bal1.get("pending_hidden"))):
                failures += 1
            # Moved windows must actually be visible on the box (not hidden
            # anywhere): the theft-by-invisibility check.
            main_id = vdesk.norm_guid(vdesk.main_id())
            for h in res.get("moved_hwnds", []):
                try:
                    vis = windows.is_window_visible(h)
                    loc = desktop_of(h)
                    if not check("1) moved visible on box",
                                 vis and loc == vdesk.norm_guid(info["id"]),
                                 f"visible={vis} loc={loc[:8]}"):
                        failures += 1
                except Exception as exc:  # noqa: BLE001
                    check("1) moved visibility readable", False, str(exc)[:100])
                    failures += 1
        print(f"  [INFO] launch wall: {wall:.1f}s (window 5s + quiet 0.5s)")
    finally:
        nuke(name)

    # 2) Pin/restore: pinned -> never move (delta pin).
    name = "m-pin"
    nuke(name)
    pid = 0
    try:
        info = vdesk.create_desktop(name)
        rec = {"name": name, "desktop_id": info["id"], "desktop_number": info["number"],
               "pids": [], "hwnds": []}
        state.put_desktop(rec)
        pid, wins = launch_raw_charmap()
        if not check("2) raw window present", bool(wins), "no charmap"):
            failures += 1
            h = 0
        else:
            h = wins[0]["hwnd"]
            try:
                vdesk.call(lambda h=h: __import__("pyvda").AppView(hwnd=h).pin())
            except Exception as exc:  # noqa: BLE001
                check("2) pin can be set", False, str(exc)[:100])
                failures += 1
            time.sleep(0.3)
            pinned = vdesk.is_view_pinned(h) or vdesk.is_app_pinned(h)
            if not check("2) pin detected", pinned):
                failures += 1
            res = retrieval._decide_move(h, info["id"], time.time(), windows._tick_now())
            if not check("2) never move pinned", res.get("moved") is False and res.get("reason") == "pin",
                         str(res)):
                failures += 1
            try:
                vdesk.call(lambda h=h: __import__("pyvda").AppView(hwnd=h).unpin())
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
            res2 = retrieval._decide_move(h, info["id"], time.time(), windows._tick_now())
            if not check("2) moves after unpin", res2.get("moved") is True, str(res2)):
                failures += 1
            else:
                try:
                    if not check("2) restore after unpin",
                                 desktop_of(h) == vdesk.norm_guid(info["id"])):
                        failures += 1
                except Exception as exc:  # noqa: BLE001
                    check("2) restore readable", False, str(exc)[:100])
                    failures += 1
            try:
                if windows.is_window(h):
                    windows.close_window(h)
            except Exception:  # noqa: BLE001
                pass
        try:
            if pid:
                windows.kill_tree(pid)
        except Exception:  # noqa: BLE001
            pass
    finally:
        nuke(name)

    # 3) Parallel launches: no 45-s pile-up (decider only ms).
    for n in ("m-par-a", "m-par-b"):
        nuke(n)
    recs = {}
    try:
        for n in ("m-par-a", "m-par-b"):
            info = vdesk.create_desktop(n)
            rec = {"name": n, "desktop_id": info["id"], "desktop_number": info["number"],
                   "pids": [], "hwnds": []}
            state.put_desktop(rec)
            recs[n] = rec
        t0 = time.time()

        def _one(n):
            rec = recs[n]
            res = retrieval.launch_and_retrieve(rec["desktop_id"], "charmap.exe", "", 5.0,
                                                cwd=str(Path.home()), desktop_name=n)
            rec["pids"].append(res.get("pid", 0))
            for h in res.get("moved_hwnds", []):
                if h not in rec.get("hwnds", []):
                    rec.setdefault("hwnds", []).append(h)
            state.put_desktop(rec)
            return res

        with ThreadPoolExecutor(max_workers=2) as ex:
            r = list(ex.map(_one, ["m-par-a", "m-par-b"]))
        wall = time.time() - t0
        # Late windows (cold-start contention) are picked up by the watcher
        # "any time" after the launch window: grace period, then verify per
        # box via window_list (launch OR watcher retrieval both count - the
        # guarantee is that every window ends up in its box).
        time.sleep(8.0)
        boxed = {}
        for n in ("m-par-a", "m-par-b"):
            try:
                want = vdesk.norm_guid(recs[n]["desktop_id"])
                count = 0
                for w in windows.all_windows(include_tool_windows=True):
                    try:
                        if desktop_of(w["hwnd"]) == want and windows.has_client(w["hwnd"]):
                            count += 1
                    except Exception:  # noqa: BLE001
                        continue
                boxed[n] = count
            except Exception:  # noqa: BLE001
                boxed[n] = -1
        ok = all(bool(x.get("moved_hwnds")) for x in r) or all(v > 0 for v in boxed.values())
        if not check("3) both landed in parallel", ok,
                     f"wall={wall:.1f}s moved={[len(x.get('moved_hwnds', [])) for x in r]} boxed={boxed}"):
            failures += 1
        print(f"  [INFO] parallel wall: {wall:.1f}s (pile-up would be ~10s+ / timeout 45s)")
        if wall >= 30:
            check("3) no pile-up", False, f"wall={wall:.1f}s")
            failures += 1
    finally:
        for n in ("m-par-a", "m-par-b"):
            nuke(n)

    try:
        vdesk.switch_to(home)
    except Exception:  # noqa: BLE001
        pass
    print("=" * 60)
    if failures:
        print(f"RESULT: {failures} failed")
        return 1
    print("RESULT: MEASUREMENT GREEN (latency/flash, pin/restore, parallel)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
