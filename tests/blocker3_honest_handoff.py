"""BLOCKER 3: honest handoff, no foreign kill.

 Chrome (shared profile, user process alive) via app_launch on worker hx:
 - handoff:true expected (no silent takeover).
 - Window stays on Main in the delta (foreign-host), adoption only explicit
   (explicit field, not a prose hint) -> SPEC, field currently missing.
 - desktop_remove: killed:[] (starter dead), host process alive afterwards,
   no process kill on a shared PID.
 - Courtesy: window back on Main + cleanly closed (fresh empty
   new tab, no content), no graveyard residue.

 NOTE: briefly moves a fresh user window (empty new tab) and restores it.
 No content affected.
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


def chrome_main_pids():
    out = set()
    for w in windows.all_windows(include_tool_windows=False):
        if (w["class"] or "") == "Chrome_WidgetWin_1":
            try:
                if desktop_of(w["hwnd"]) == vdesk.norm_guid(vdesk.main_id()):
                    out.add(w["pid"])
            except Exception:  # noqa: BLE001
                pass
    return out


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

    print("blocker-3: honest handoff, no foreign kill")
    print("=" * 60)
    home = vdesk.current_id()
    baseline = len(vdesk.list_desktops())
    hosts0 = chrome_main_pids()
    check("user chrome present on main", bool(hosts0), "no Chrome -> cannot provoke handoff")
    if not hosts0:
        print("RESULT: SKIP (no running Main Chrome)")
        return 2
    try:
        async with Client(mcp) as c:
            nuke("hx")
            await c.call_tool("desktop_create", {"name": "hx"})
            lr = await call(c, "app_launch", {"desktop": "hx",
                                              "exe": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                                              "args": "--new-window about:blank",
                                              "wait_seconds": 20})
            check("handoff:true", lr.get("handoff") is True, str(lr)[:200])
            # v2: foreign host always goes to the delta and stays on Main until adoption.
            dl = await call(c, "delta_list", {})
            dhwnds = [e.get("hwnd") for e in dl.get("delta", [])
                      if e.get("reason") == "foreign-host"]
            check("delta foreign-host", bool(dhwnds), str(dl)[:200])
            # SPEC: explicit foreign marker (field, not prose)
            foreign = [k for k in lr.keys() if "foreign" in k.lower()]
            check("foreign marker (field)", bool(foreign),
                  f"keys={sorted(lr.keys())} (prose hint only)")
            wl = await call(c, "window_list", {"desktop": "hx"})
            hwnds = [w["hwnd"] for w in wl.get("windows", [])]
            check("no auto-move (stays on Main)", not hwnds, str(wl)[:150])
            h = int(dhwnds[0]) if dhwnds else 0
            if h:
                check("handoff window on main", desktop_of(h) == vdesk.norm_guid(home))
                owner = None
                for w in windows.all_windows(include_tool_windows=True):
                    if w["hwnd"] == h:
                        owner = w["pid"]
                        break
                check("owner is host pid", owner in hosts0,
                      f"owner={owner} hosts={sorted(hosts0)}")
                # Explicit adoption (vision verdict, no automation).
                try:
                    await c.call_tool("delta_view", {"hwnd": h})
                except Exception as exc:  # noqa: BLE001
                    check("single delta_view", False, str(exc)[:100])
                ar = await call(c, "window_adopt", {"desktop": "hx", "hwnd": h})
                check("adopt ok", ar.get("adopted") == h, str(ar)[:150])
                loc = desktop_of(h)
                rec = state.require_desktop("hx")
                check("window on worker after adopt", loc == vdesk.norm_guid(rec["desktop_id"]),
                      f"{loc[:8]} vs worker")
                # Courtesy: back on Main, cleanly closed (empty new tab)
                vdesk.move_window(h, home)
                windows.show_window(h)
                time.sleep(0.5)
                check("back on main", desktop_of(h) == vdesk.norm_guid(home))
                windows.close_window(h)
                time.sleep(1.0)
            rm = await call(c, "desktop_remove", {"desktop": "hx", "kill_apps": True})
            check("removed", rm.get("removed") is True, str(rm)[:200])
            check("killed empty (starter dead)", rm.get("killed", None) == [], str(rm.get("killed")))
            check("no parking needed", rm.get("parked_on_graveyard", []) == [],
                  str(rm.get("parked_on_graveyard")))
            hosts1 = chrome_main_pids()
            check("no foreign kill (hosts alive)", bool(hosts1), f"before={sorted(hosts0)}")
            print(f"  [INFO] host pids before={sorted(hosts0)} after={sorted(hosts1)}")
    except Exception as exc:  # noqa: BLE001
        check("suite without crash", False, f"{type(exc).__name__}: {exc}")
    finally:
        nuke("hx")
        nuke("graveyard")
        try:
            vdesk.switch_to(home)
        except Exception:  # noqa: BLE001
            pass
    check("desktop count baseline", len(vdesk.list_desktops()) == baseline)
    check("main visible", vdesk.current_id() == home)
    check("no graveyard residue", state.get_desktop("graveyard") is None)
    print("=" * 60)
    if failures:
        print(f"RESULT: {len(failures)} failed -> {failures}")
        return 1
    print("RESULT: BLOCKER-3 GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
