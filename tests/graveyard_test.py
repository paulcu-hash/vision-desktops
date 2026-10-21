"""Graveyard desktop: explicit parking instead of implicit migration.

Problem: remove_desktop() lets surviving windows (e.g. save dialogs) migrate
IMPLICITLY to the neighbor desktop via Windows - possibly visible on Main.
Fix: dedicated, permanent graveyard desktop (reserved record); the delete
flow parks survivors EXPLICITLY there before the empty worker desktop falls.
Rotation against accumulation: more than MAX -> oldest via WM_CLOSE.

Flow (generic, using Notepad as example - no app-specific path):
  Guards (create/remove 'graveyard' refused, no side effects) ->
  d-grave + 3 Notepads with unsaved text ->
  desktop_remove -> 3x parked, rotation fires at cap=2 ->
  every parked window is PROVABLY on the graveyard ID (not Main!) ->
  resolve dialogs via vision pattern (Tab/Space, graveyard record as anchor!) ->
  teardown (graveyard gone, baseline restored).

NOTE: hopping switches visibly for a moment (the intended trade-off).
Leave mouse/keyboard alone while it runs.
"""

import asyncio
import json
import sys
import time

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from fastmcp import Client

from vision_desktops import server as _server
from vision_desktops import hop, state, vdesk, windows
from vision_desktops.server import mcp

NAME = "d-grave"
TEXTS = ("GRAVE ONE 111", "GRAVE TWO 222", "GRAVE THREE 333")


def text_of(result) -> str:
    return "\n".join(getattr(b, "text", "") for b in result.content)


async def call(client, tool, args):
    try:
        res = await client.call_tool(tool, args)
    except Exception as exc:  # ToolError from _err()
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


def desktop_of(hwnd: int) -> str:
    def run():
        from pyvda import AppView

        return vdesk.norm_guid(AppView(hwnd=int(hwnd)).desktop_id)

    return vdesk.call(run)


def nuke_rec(name: str):
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


async def _dismiss(client, desktop: str, hwnds: list):
    """Close dialogs via vision pattern: first WM_CLOSE (opens the
    save dialog), then alt+n ("Don't save", mnemonic - independent of
    focus routing; Tab/Space land in the editor in WinUI Notepad).
    Without close_window there is no dialog and alt+n goes nowhere -
    exactly what produced the old 2/3 FAILs. 4 rounds, best effort."""
    errs = {}
    for h in list(hwnds):
        if windows.is_window(h):
            try:
                windows.close_window(h)
            except Exception as exc:  # noqa: BLE001
                errs[h] = f"{type(exc).__name__}: {str(exc)[:100]}"
        await asyncio.sleep(1.5)
        for _ in range(4):
            if not windows.is_window(h):
                break
            try:
                await client.call_tool("hop_key", {"desktop": desktop, "hwnd": h,
                                                   "keys": "alt+n"})
            except Exception as exc:  # noqa: BLE001
                errs[h] = f"{type(exc).__name__}: {str(exc)[:100]}"
            await asyncio.sleep(1.0)
    return errs


async def amain() -> int:
    failures = []

    def check(name: str, ok: bool, detail: str = ""):
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}" + (f": {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("graveyard test (explicit parking + rotation + guards)")
    print("=" * 60)
    home = vdesk.current_id()
    baseline = len(vdesk.list_desktops())
    check("start on main", home == vdesk.main_id())
    old_cap = _server.GRAVEYARD_MAX_WINDOWS
    _server.GRAVEYARD_MAX_WINDOWS = 2
    parked_all: list = []
    grave_had_live = False
    try:
        async with Client(mcp) as c:
            # 1. Guards: reserved name, no side effects.
            r = await call(c, "desktop_create", {"name": "graveyard"})
            check("create 'graveyard' refused", "_error" in r or "reserved" in json.dumps(r),
                  str(r)[:120])
            r = await call(c, "desktop_remove", {"desktop": "graveyard"})
            check("remove 'graveyard' refused", "_error" in r or "cannot be deleted" in json.dumps(r),
                  str(r)[:120])

            nuke_rec(NAME)
            # Graveyard may belong to a foreign session: never nuke it
            # unasked. Only record whether a LIVE one existed at start.
            try:
                _g0 = state.get_desktop("graveyard")
                _live0 = {d["id"] for d in vdesk.list_desktops()}
                grave_had_live = bool(_g0 and _g0.get("desktop_id") in _live0)
            except Exception:  # noqa: BLE001
                grave_had_live = False
            await c.call_tool("desktop_create", {"name": NAME})

            # 2. Three Notepads with unsaved text (blocks WM_CLOSE).
            # See -> act -> verify -> retry (max 2).
            hwnds = []
            for i in range(3):
                await call(c, "app_launch", {"desktop": NAME, "exe": "notepad.exe"})
                wl = await call(c, "window_list", {"desktop": NAME})
                fresh = [w["hwnd"] for w in wl.get("windows", [])
                         if w["hwnd"] not in hwnds and windows.has_client(w["hwnd"])]
                if not fresh:
                    check(f"launch {i}", False, "no new window")
                    return 1
                h = fresh[0]  # Largest new one = main window.
                hwnds.append(h)
                landed = False
                for _ in range(2):
                    try:
                        await c.call_tool("hop_type", {"desktop": NAME, "hwnd": h,
                                                       "text": TEXTS[i]})
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(1.0)
                    wl2 = await call(c, "window_list", {"desktop": NAME})
                    t2 = " | ".join(w.get("title", "") for w in wl2.get("windows", []))
                    if TEXTS[i] in t2:
                        landed = True
                        break
                if not landed:
                    check(f"text {i} landed", False, t2[:150])
                    return 1
            check("3 windows with text", True)

            # 3. Remove -> parking + rotation + honest pending (no verdict).
            rm = await call(c, "desktop_remove", {"desktop": NAME})
            parked_all = list(rm.get("parked_on_graveyard", []))
            check("remove ok", rm.get("removed") is True, str(rm)[:200])
            check("3x parked", len(rm.get("parked_on_graveyard", [])) == 3,
                  str(rm.get("parked_on_graveyard")))
            check("pending protocol (no verdict)",
                  isinstance(rm.get("pending"), list) and len(rm.get("pending", [])) >= 1,
                  str(rm.get("pending"))[:200])
            check("needs_agent_vision honest", "needs_agent_vision" in rm,
                  str(rm)[:200])
            check("rotation fired", rm.get("closed_by_rotation", []) != [],
                  str(rm.get("closed_by_rotation")))
            grec = state.get_desktop("graveyard")
            check("graveyard record exists", grec is not None)
            gid = (grec or {}).get("desktop_id", "")
            alive_tracked = [h for h in (grec or {}).get("hwnds", []) if windows.is_window(h)]
            check("cap 2 respected", len(alive_tracked) <= 2,
                  f"tracked={alive_tracked}")
            loc_ok = True
            for h in rm.get("parked_on_graveyard", []):
                try:
                    if desktop_of(h) != vdesk.norm_guid(gid):
                        loc_ok = False
                except Exception:
                    pass  # Closed meanwhile: location irrelevant, just not Main.
            check("parked on graveyard ID", loc_ok)
            try:
                cur = await call(c, "desktop_list", {})
                on_main = [d for d in cur if d.get("is_current")]
                check("main visible", len(on_main) == 1 and on_main[0]["id"] == home,
                      str(on_main)[:120])
                names = [d.get("owned_by") for d in cur]
                check("worker gone", NAME not in names, str(names))
            except Exception as exc:
                check("desktop_list readable", False, str(exc)[:120])

            # 4. Resolve dialogs via vision pattern (graveyard record as anchor!).
            # Rotation victims are no longer tracked -> briefly re-track
            # (for hop_key resolution; teardown cleans up afterwards).
            try:
                grec_now = state.require_desktop("graveyard")
                missing = [h for h in rm.get("parked_on_graveyard", [])
                           if h not in grec_now.get("hwnds", [])]
                if missing:
                    grec_now["hwnds"] = list(grec_now.get("hwnds", [])) + missing
                    state.put_desktop(grec_now)
            except Exception:  # noqa: BLE001
                pass
            for h in list(rm.get("parked_on_graveyard", [])):
                last_err = ""
                if windows.is_window(h):
                    try:
                        windows.close_window(h)  # Open dialog first, then dismiss
                    except Exception as exc:  # noqa: BLE001
                        last_err = f"{type(exc).__name__}: {str(exc)[:100]}"
                    time.sleep(1.5)
                for _ in range(4):
                    if not windows.is_window(h):
                        break
                    try:
                        await c.call_tool("hop_key", {"desktop": "graveyard", "hwnd": h,
                                                      "keys": "alt+n"})
                    except Exception as exc:  # noqa: BLE001
                        last_err = f"{type(exc).__name__}: {str(exc)[:100]}"
                    time.sleep(1.0)
                check(f"dialog {h} closed", not windows.is_window(h),
                      f"hwnd={h} last_err={last_err}")
    except Exception as exc:
        check("suite without crash", False, f"{type(exc).__name__}: {exc}")
    finally:
        _server.GRAVEYARD_MAX_WINDOWS = old_cap
        # Teardown with dialog resolution (otherwise residues migrate to Main):
        # first close via vision pattern, then nuke. Foreign graveyard
        # infra (other session) is never touched: only dismiss OWN hwnds,
        # remove desktop+record only if we created them.
        try:
            async with Client(mcp) as c2:
                try:
                    trec = state.require_desktop(NAME)
                    await _dismiss(c2, NAME, trec.get("hwnds", []))
                except Exception:  # noqa: BLE001
                    pass
                try:
                    grec = state.require_desktop("graveyard")
                    missing = [h for h in parked_all
                               if h not in grec.get("hwnds", [])]
                    if missing:
                        grec["hwnds"] = list(grec.get("hwnds", [])) + missing
                        state.put_desktop(grec)
                    await _dismiss(c2, "graveyard", list(parked_all))
                    # Remove own (now dead) hwnds from foreign list.
                    grec = state.require_desktop("graveyard")
                    grec["hwnds"] = [h for h in grec.get("hwnds", [])
                                     if h not in parked_all or windows.is_window(h)]
                    state.put_desktop(grec)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] teardown-dismiss: {exc}")
        nuke_rec(NAME)
        if not grave_had_live:
            nuke_rec("graveyard")
        try:
            vdesk.switch_to(home)
        except Exception:  # noqa: BLE001
            pass

    after = len(vdesk.list_desktops())
    check("desktop count back to baseline", after == baseline, f"baseline={baseline} after={after}")
    check("main visible at end", vdesk.current_id() == home)
    print("=" * 60)
    if failures:
        print(f"RESULT: {len(failures)} failed -> {failures}")
        return 1
    print("RESULT: GRAVEYARD GREEN - explicitly parked, rotated, Main never touched")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
