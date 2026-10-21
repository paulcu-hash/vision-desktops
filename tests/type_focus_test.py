"""Input-race regression test: hop_type immediately after launch.

Symptom (hit 3): the first character was lost ("EDGE IRTY" instead of
"EDGE DIRTY") because right after launch/focus change the keyboard focus
was not yet in the input field. Fix: _await_focus waits for focus in the
window before every keystroke (caret rule, server side); timeout raises
an error instead of blind typing.

Flow per round (3x, generic, using Notepad as example - no app-specific path):
  fresh Notepad -> type IMMEDIATELY (no click, no sleep!) ->
  read back exactly (ctrl+a, ctrl+c, clipboard - user-equivalent, no peek) ->
  check equality (including first character) -> empty+close window.

NOTE: hops briefly switch visibly to the target desktop and back
(the intended trade-off). Leave mouse/keyboard alone while it runs.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from vision_desktops import hop, state, vdesk, windows

NAME = "vt-focus"
ROUNDS = 3


def launch_notepad(rec):
    """Fetch a fresh Notepad onto the worker desktop. Returns hwnd."""
    before = windows.all_hwnds()  # IDs only - never blocks.
    pid = windows.launch_process("notepad.exe", cwd=str(Path.home()))
    rec["pids"].append(pid)
    found = windows.wait_new_windows(before, rec["pids"], 20)
    for w in found["windows"]:
        if not windows.has_client(w["hwnd"]):
            continue
        try:
            vdesk.move_window(w["hwnd"], rec["desktop_id"])
        except Exception:  # noqa: BLE001
            try:
                windows.show_window(w["hwnd"])  # Register view...
                vdesk.move_window(w["hwnd"], rec["desktop_id"])  # ...then move over.
            except Exception:  # noqa: BLE001
                continue  # No hiding (report only) - window stays where it is.
        windows.show_window(w["hwnd"])
        rec["hwnds"].append(w["hwnd"])
        state.put_desktop(rec)
        return w["hwnd"]
    state.put_desktop(rec)
    return 0


def read_back(desktop_id: int, hwnd: int) -> str:
    hop.do_hop(desktop_id, hwnd, "key", {"keys": "ctrl+a"})
    hop.do_hop(desktop_id, hwnd, "key", {"keys": "ctrl+c"})
    return hop.clipboard_get().replace("\r\n", "\n")


def close_test_window(desktop_id: int, hwnd: int, pid: int):
    import ctypes as _ct

    def alive(h: int) -> bool:
        try:
            return bool(_ct.windll.user32.IsWindow(_ct.c_void_p(h)))
        except Exception:  # noqa: BLE001
            return False

    try:
        if alive(hwnd):
            hop.do_hop(desktop_id, hwnd, "key", {"keys": "ctrl+a"})
            hop.do_hop(desktop_id, hwnd, "key", {"keys": "delete"})
        time.sleep(0.5)
        if alive(hwnd):
            windows.close_window(hwnd)
        windows.kill_tree(pid)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] close {hwnd}: {exc}")
    time.sleep(1.0)
    return alive(hwnd)


def main() -> int:
    failures = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("type-focus test (input race: launch -> type immediately -> read exactly)")
    print("=" * 60)
    home = vdesk.current_id()
    baseline = len(vdesk.list_desktops())
    check("start on main", home == vdesk.main_id())

    rec = None
    try:
        if state.get_desktop(NAME):
            try:
                old = state.require_desktop(NAME)
                for pid in old.get("pids", []):
                    windows.kill_tree(pid)
                vdesk.remove_desktop(old["desktop_id"])
            except Exception:  # noqa: BLE001
                pass
            state.del_desktop(NAME)
        info = vdesk.create_desktop(NAME)
        rec = {"name": NAME, "desktop_id": info["id"], "desktop_number": info["number"],
               "pids": [], "hwnds": []}
        state.put_desktop(rec)
        did = info["id"]

        for i in range(ROUNDS):
            marker = f"DIRTY{i} abc xyz"
            hwnd = launch_notepad(rec)
            check(f"round {i}: launch", hwnd != 0)
            if not hwnd:
                continue
            pid = rec["pids"][-1]
            # The critical moment: type with NO click/sleep before it.
            hop.do_hop(did, hwnd, "type", {"text": marker})
            got = read_back(did, hwnd)
            print(f"round {i}: want={marker!r} got={got!r}", flush=True)
            if got == marker:
                check(f"round {i}: exact from first character ({len(marker)} chars)", True)
            else:
                off = next((j for j, (a, b) in enumerate(zip(got, marker)) if a != b),
                           min(len(got), len(marker)))
                check(f"round {i}: exact from first character", False,
                      f"len got={len(got)} want={len(marker)} "
                      f"first-diff@{off}: got={got[off:off+20]!r} want={marker[off:off+20]!r}")
            still = close_test_window(did, hwnd, pid)
            check(f"round {i}: cleaned up", not still, f"hwnd={hwnd}")
            try:
                rec = state.require_desktop(NAME)
                rec["hwnds"] = [h for h in rec.get("hwnds", []) if h != hwnd]
                rec["pids"] = [p for p in rec.get("pids", []) if p != pid]
                state.put_desktop(rec)
            except Exception:  # noqa: BLE001
                pass
        check("back on main", vdesk.current_id() == home)
    except Exception as exc:  # noqa: BLE001
        check("suite without crash", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if state.get_desktop(NAME):
                vdesk.remove_desktop(state.require_desktop(NAME)["desktop_id"])
                state.del_desktop(NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] cleanup {NAME}: {exc}")
        try:
            vdesk.switch_to(home)
        except Exception:  # noqa: BLE001
            pass

    after = len(vdesk.list_desktops())
    check("desktop count back to baseline", after == baseline, f"baseline={baseline} after={after}")
    print("=" * 60)
    if failures:
        print(f"RESULT: {len(failures)} failed -> {failures}")
        return 1
    print("RESULT: FOCUS RACE GREEN - no character loss after launch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
