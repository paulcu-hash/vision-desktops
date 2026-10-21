"""BLOCKER 2: Main stays untouched while 2 workers hop/launch.

 - Main visible before/after every hop (return-home duty).
 - 0 new windows on Main (inventory diff, location verified, not just enum).
 - desktop_* level on Main refuses hard (no switch!).
 - Screenshot diff Main before/after (direct BitBlt, NOT desktop_see -
   which refuses Main via guard; guard stays untouched).
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from fastmcp import Client
from PIL import Image, ImageChops

from vision_desktops import capture, hop, state, vdesk, windows
from vision_desktops.server import mcp

BOUND_PX = 30000  # Clock/tray animates; anything below = no pop-up


def text_of(result):
    return "\n".join(getattr(b, "text", "") for b in result.content)


async def call(client, tool, args):
    try:
        res = await client.call_tool(tool, args)
    except Exception as exc:
        return {"_error": True, "_raw": f"{type(exc).__name__}: {exc}"}
    txt = text_of(res)
    try:
        out = json.loads(txt or "null")
        if getattr(res, "is_error", False):
            out = {"_error": True, "_raw": txt} if not isinstance(out, dict) else \
                {**out, "_error": True}
        return out
    except Exception:
        return {"_raw": txt, "_error": bool(getattr(res, "is_error", False))}


def main_shot(tag):
    data, w, h, _ox, _oy = capture.grab_screen()
    out = state.CAPTURE_DIR / f"blocker2-main-{tag}.png"
    state.CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    out.write_bytes(capture.to_png(data, w, h))
    return out


def changed_pixels(a: Path, b: Path) -> int:
    with Image.open(a).convert("L") as ia, Image.open(b).convert("L") as ib:
        if ia.size != ib.size:
            return 10 ** 9
        diff = ImageChops.difference(ia, ib)
        hist = diff.histogram()
        return sum(c for v, c in enumerate(hist) if v > 12)


def inventory():
    """hwnd -> (title, desktop-id|ERR)."""
    inv = {}
    for w in windows.all_windows(include_tool_windows=False):
        try:
            did = vdesk.call(lambda h=w["hwnd"]: __import__("pyvda").AppView(hwnd=h).desktop.id)
            did = vdesk.norm_guid(did)
        except Exception:  # noqa: BLE001
            did = "ERR"
        inv[w["hwnd"]] = (w["title"], did)
    return inv


def nuke(name):
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
        vdesk.remove_desktop(rec["desktop_id"])
    except Exception:  # noqa: BLE001
        pass
    try:
        state.del_desktop(name)
    except Exception:  # noqa: BLE001
        pass


async def amain():
    failures = []

    def check(name, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("blocker-2: main stays untouched (2 workers)")
    print("=" * 60)
    home = vdesk.current_id()
    check("start on main", home == vdesk.main_id())
    baseline_n = len(vdesk.list_desktops())
    shot0 = main_shot("before")
    inv0 = inventory()
    home_violations = []
    try:
        async with Client(mcp) as c:
            for n in ("w1", "w2"):
                nuke(n)
                await c.call_tool("desktop_create", {"name": n})
            # Interleaved: launch w1, launch w2, type w1, type w2, keys, desktop level
            for n, tok in (("w1", "MAINW1"), ("w2", "MAINW2")):
                await call(c, "app_launch", {"desktop": n, "exe": "notepad.exe",
                                             "wait_seconds": 15})
                if vdesk.current_id() != home:
                    home_violations.append(f"launch-{n}")
            for n, tok in (("w1", "MAINW1"), ("w2", "MAINW2")):
                wl = await call(c, "window_list", {"desktop": n})
                hs = [w["hwnd"] for w in wl.get("windows", [])]
                if hs:
                    await c.call_tool("hop_type", {"desktop": n, "hwnd": hs[0], "text": tok})
                if vdesk.current_id() != home:
                    home_violations.append(f"type-{n}")
            for n in ("w1", "w2"):
                await call(c, "desktop_key", {"desktop": n, "keys": "escape"})
                if vdesk.current_id() != home:
                    home_violations.append(f"key-{n}")
            # Desktop level on worker (Start open/close) - Main never touched
            await call(c, "desktop_key", {"desktop": "w1", "keys": "win"})
            time.sleep(1.0)
            await call(c, "desktop_key", {"desktop": "w1", "keys": "escape"})
            time.sleep(0.5)
            if vdesk.current_id() != home:
                home_violations.append("desktop-level")
            check("always back on main", not home_violations, str(home_violations))

            # Hard Main refusal (without a single switch)
            refused = 0
            for label, fn in [
                ("desktop_type", lambda: hop.do_hop_desktop(home, "type", {"text": "x"})),
                ("desktop_click", lambda: hop.do_hop_desktop(home, "click",
                                                             {"x": 10, "y": 10})),
                ("desktop_key", lambda: hop.do_hop_desktop(home, "key", {"keys": "escape"})),
            ]:
                try:
                    fn()
                    check(f"main-guard {label}", False, "NO refusal!")
                except RuntimeError as exc:
                    if "Main" in str(exc):
                        refused += 1
                    else:
                        check(f"main-guard {label}", False, str(exc)[:100])
            check("main refused 3x", refused == 3, f"refused={refused}")
            check("no switch happened", vdesk.current_id() == home)

            # Worker cleanup (clean expected: unparked, no graveyard)
            for n in ("w1", "w2"):
                rm = await call(c, "desktop_remove", {"desktop": n, "kill_apps": True})
                if rm.get("parked_on_graveyard"):
                    try:
                        grec = state.require_desktop("graveyard")
                        missing = [x for x in rm["parked_on_graveyard"]
                                    if x not in grec.get("hwnds", [])]
                        if missing:
                            grec["hwnds"] = list(grec.get("hwnds", [])) + missing
                            state.put_desktop(grec)
                    except Exception:  # noqa: BLE001
                        pass
                    for h in rm["parked_on_graveyard"]:
                        for _ in range(3):
                            if not windows.is_window(h):
                                break
                            try:
                                await c.call_tool("hop_key", {"desktop": "graveyard",
                                                              "hwnd": h, "keys": "tab"})
                                await c.call_tool("hop_key", {"desktop": "graveyard",
                                                              "hwnd": h, "keys": "space"})
                            except Exception:  # noqa: BLE001
                                pass
                            await asyncio.sleep(1.0)
                    nuke("graveyard")
    except Exception as exc:  # noqa: BLE001
        check("suite without crash", False, f"{type(exc).__name__}: {exc}")
    finally:
        nuke("w1")
        nuke("w2")
        nuke("graveyard")
        try:
            vdesk.switch_to(home)
        except Exception:  # noqa: BLE001
            pass
    # Before/after: inventory + screenshot diff
    shot1 = main_shot("after")
    inv1 = inventory()
    new = [h for h in inv1 if h not in inv0]
    main_id = vdesk.norm_guid(home)
    on_main = [h for h in new if inv1[h][1] == main_id]
    check("main visible", vdesk.current_id() == home)
    check("0 new windows on Main", not on_main,
          str([(h, inv1[h][0][:40]) for h in on_main])[:200])
    print(f"  [INFO] new hwnds total: {len(new)} (all expected outside Main)")
    delta = changed_pixels(shot0, shot1)
    check("screenshot diff small", delta < BOUND_PX, f"delta={delta}")
    print(f"  [INFO] shots: {shot0.name}, {shot1.name}")
    check("desktop count baseline", len(vdesk.list_desktops()) == baseline_n)
    print("=" * 60)
    if failures:
        print(f"RESULT: {len(failures)} failed -> {failures}")
        return 1
    print("RESULT: BLOCKER-2 GREEN - Main untouched")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
