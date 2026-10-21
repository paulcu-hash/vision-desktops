"""Tk-delayed probe (tests/ only, temp): deterministic delayed window
(single titled Tk window, no transients, no font enumeration) via the real
MCP path. Tests whether the watcher path moves a LATE window when the APP
behaves deterministically. Own tree only, honest cleanup."""
import asyncio
import faulthandler
import sys
import time
from pathlib import Path

faulthandler.enable()
sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")
from fastmcp import Client
from vision_desktops import retrieval, state, vdesk, windows
from vision_desktops.server import mcp

PY = sys.executable
DELAY_HELPER = str(Path(r"C:\Users\Paul\vision-desktops\tests\window_placement_delayed_test.py").resolve())


async def main():
    name = "probe-tk"
    async with Client(mcp) as c:
        try:
            await c.call_tool("desktop_create", {"name": name})
        except Exception as e:
            print(f"create: {e}")
            return
        rec = state.require_desktop(name)
        box_id = vdesk.norm_guid(rec["desktop_id"])
        main_id = vdesk.norm_guid(vdesk.main_id())
        lr = await c.call_tool("app_launch", {
            "desktop": name, "exe": PY,
            "args": f'"{DELAY_HELPER}" --child-tk "TKPROBE" 10',
            "wait_seconds": 5})
        pid = lr.get("pid") if isinstance(lr, dict) else None
        print(f"launch pid={pid}", flush=True)
        for i in range(6):
            await asyncio.sleep(5.0)
            try:
                bal = retrieval.hide_balance()
            except Exception:
                bal = {}
            print(f"t~{(i+1)*5}s hid={bal.get('hidden')} mov={bal.get('moved')} "
                  f"res={bal.get('restored')} pend={bal.get('pending_hidden')}", flush=True)
        try:
            wl = await c.call_tool("window_list", {"desktop": name})
            wins = wl.get("windows", []) if isinstance(wl, dict) else []
        except Exception as e:
            print(f"list: {e}")
            wins = []
        for w in wins:
            print(f"win hwnd={w['hwnd']} pid={w.get('pid')} title={w.get('title')!r}", flush=True)
        ok = any("TKPROBE" in (w.get("title") or "") for w in wins)
        print("TKPROBE on box:", ok, flush=True)
        try:
            tree = set(windows.tree_pids([pid])) if pid else set()
        except Exception:
            tree = set()
        for w in wins:
            try:
                if int(w.get("pid", 0)) in tree:
                    windows.close_window(int(w["hwnd"]))
            except Exception:
                pass
        await asyncio.sleep(1.0)
        try:
            if pid:
                windows.kill_tree(pid)
        except Exception:
            pass
        try:
            wl = await c.call_tool("window_list", {"desktop": name})
            if not (wl.get("windows", []) if isinstance(wl, dict) else []):
                await c.call_tool("desktop_remove", {"desktop": name, "decisions": {}})
                print("removed", flush=True)
            else:
                print("KEPT (leftover)", flush=True)
        except Exception as e:
            print(f"remove: {e}", flush=True)
        try:
            state.del_desktop(name)
        except Exception:
            pass
        print("RESULT:", "TK-GREEN" if ok else "TK-RED", flush=True)


asyncio.run(main())
