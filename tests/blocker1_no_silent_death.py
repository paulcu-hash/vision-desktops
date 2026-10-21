"""BLOCKER 1 (release blocker, SPEC frozen): no silent death.

 dirty Notepad + desktop_remove without verdict -> must NEVER end with
 removed:true and parked:[]. Expected: parked on graveyard + pending:[hwnd] +
 needs_agent_vision. With {hwnd:drop} approval -> may close.
 With {hwnd:park} -> sits on the graveyard ID.

 Status: the pending/drop/park protocol does not exist yet -> this test
 stays RED until implemented and freezes the spec.
 Oracle without screenshots: title star (dirty) + HWND death + park location.
 Two rounds (sd1/sd2) because the close path is currently non-deterministic.
"""

import asyncio
import json
import sys
import time

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from fastmcp import Client

from vision_desktops import state, vdesk, windows
from vision_desktops.server import mcp


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


def desktop_of(hwnd):
    def run():
        from pyvda import AppView

        return vdesk.norm_guid(AppView(hwnd=int(hwnd)).desktop_id)

    return vdesk.call(run)


def titles():
    return {w["hwnd"]: w["title"] for w in windows.all_windows(include_tool_windows=True)}


async def dismiss(client, desktop, hwnds):
    for h in list(hwnds):
        for _ in range(3):
            if not windows.is_window(h):
                break
            try:
                await client.call_tool("hop_key", {"desktop": desktop, "hwnd": h,
                                                   "keys": "tab"})
                await client.call_tool("hop_key", {"desktop": desktop, "hwnd": h,
                                                   "keys": "space"})
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1.0)


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

    print("blocker-1: no silent death (spec frozen)")
    print("=" * 60)
    home = vdesk.current_id()
    baseline = len(vdesk.list_desktops())
    try:
        async with Client(mcp) as c:
            for rnd, (name, tok) in enumerate([("sd1", "BLOCKA1"), ("sd2", "BLOCKB2")]):
                nuke(name)
                await c.call_tool("desktop_create", {"name": name})
                await call(c, "app_launch", {"desktop": name, "exe": "notepad.exe",
                                             "wait_seconds": 15})
                wl = await call(c, "window_list", {"desktop": name})
                wins = [w for w in wl.get("windows", []) if windows.has_client(w["hwnd"])]
                if not wins:
                    check(f"round{rnd} launch", False, "no window")
                    continue
                h = wins[0]["hwnd"]
                await c.call_tool("hop_type", {"desktop": name, "hwnd": h, "text": tok})
                time.sleep(0.5)
                dirty = titles().get(h, "").startswith("*")
                check(f"round{rnd} dirty assessable (star)", dirty, titles().get(h, "")[:60])
                if not dirty:
                    continue
                rm = await call(c, "desktop_remove", {"desktop": name, "kill_apps": True})
                parked = rm.get("parked_on_graveyard", [])
                nav = "needs_agent_vision" in rm
                pending = rm.get("pending", None)
                died = not windows.is_window(h)
                print(f"  [INFO] round{rnd}: removed={rm.get('removed')} parked={parked} "
                      f"nav={nav} pending={pending} window-dead={died}")
                # SPEC (a): dirty + dead + parked nowhere + no verdict = green log -> FORBIDDEN
                silent_green = (died and not parked and not nav and pending is None
                                and rm.get("removed") is True)
                check(f"round{rnd} no silent-death log", not silent_green,
                      f"removed:true + parked:[] for dead dirty window {h}")
                # SPEC (b): pending-verdict protocol exists
                check(f"round{rnd} pending protocol", pending is not None,
                      "no 'pending' field (drop/park approval missing)")
                # Per-round teardown (not part of the spec)
                if parked:
                    try:
                        grec = state.require_desktop("graveyard")
                        missing = [x for x in parked if x not in grec.get("hwnds", [])]
                        if missing:
                            grec["hwnds"] = list(grec.get("hwnds", [])) + missing
                            state.put_desktop(grec)
                    except Exception:  # noqa: BLE001
                        pass
                    await dismiss(c, "graveyard", parked)
                    nuke("graveyard")
                nuke(name)
    except Exception as exc:  # noqa: BLE001
        check("suite without crash", False, f"{type(exc).__name__}: {exc}")
    finally:
        nuke("sd1")
        nuke("sd2")
        nuke("graveyard")
        try:
            vdesk.switch_to(home)
        except Exception:  # noqa: BLE001
            pass
    check("desktop count baseline", len(vdesk.list_desktops()) == baseline)
    check("main visible", vdesk.current_id() == home)
    print("=" * 60)
    if failures:
        print(f"RESULT: {len(failures)} failed -> {failures}")
        return 1
    print("RESULT: BLOCKER-1 GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
