"""Desktop level: system UI without an app window + hard main guard.

Scenario from the deadlock report: if system UI (Start menu) holds the
foreground, the window level correctly refuses (no tracked window).
The desktop level needs none - its guard is "right desktop + verified
arrival". It NEVER switches Main (assertion, verified here).

Flow (generic, no app assumptions, all vision-verified):
  Main refusal (4 tools, without a single switch) ->
  desktop_see (fullscreen, not black) ->
  desktop_key 'win' (Start opens ON THE TEST DESKTOP, pixel diff) ->
  desktop_key 'escape' (Start closes, pixel diff) ->
  desktop_click (harmless click into empty area) ->
  clean up (Main visible again, state empty).

NOTE: hopping switches visibly for a moment (the intended trade-off).
Leave mouse/keyboard alone while it runs.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from PIL import Image, ImageChops

from vision_desktops import capture, hop, state, vdesk, windows

NAME = "d-sys"


def changed_pixels(a: Path, b: Path) -> int:
    with Image.open(a).convert("L") as ia, Image.open(b).convert("L") as ib:
        if ia.size != ib.size:
            return 10**9
        diff = ImageChops.difference(ia, ib)
        hist = diff.histogram()
        return sum(c for v, c in enumerate(hist) if v > 12)


def grab_to(tag: str) -> Path:
    from vision_desktops import hop as _hop

    rec_id = state.require_desktop(NAME)["desktop_id"]
    with _hop.desktop_session(rec_id):
        data, w, h, ox, oy = capture.grab_screen()
    out = state.CAPTURE_DIR / f"dsys-{tag}.png"
    state.CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    out.write_bytes(capture.to_png(data, w, h))
    return out


def main() -> int:
    failures = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("desktop-hop test (system-UI level + main guard)")
    print("=" * 60)
    home = vdesk.current_id()
    main_id = vdesk.main_id()
    check("start on main", home == main_id, f"{home[:8]} vs {main_id[:8]}")
    baseline_desks = len(vdesk.list_desktops())

    # 1. Hard main guard: refusal WITHOUT a single switch.
    refused = 0
    for label, action, payload in [
        ("desktop_see", "see-skip", {}),
        ("desktop_click", "click", {"x": 10, "y": 10}),
        ("desktop_type", "type", {"text": "x"}),
        ("desktop_key", "key", {"keys": "escape"}),
    ]:
        try:
            if action == "see-skip":
                # see runs via desktop_session directly (no do_hop_desktop).
                with hop.desktop_session(main_id):
                    pass
            else:
                hop.do_hop_desktop(main_id, action, payload)
            check(f"main-guard {label}", False, "NO refusal!")
        except RuntimeError as exc:
            if "Main" in str(exc):
                refused += 1
            else:
                check(f"main-guard {label}", False, str(exc)[:100])
    check("main refused 4x", refused == 4, f"refused={refused}")
    check("no switch happened", vdesk.current_id() == home)

    rec = None
    try:
        if state.get_desktop(NAME):
            try:
                old = state.require_desktop(NAME)
                vdesk.remove_desktop(old["desktop_id"])
            except Exception:  # noqa: BLE001
                pass
            state.del_desktop(NAME)
        info = vdesk.create_desktop(NAME)
        rec = {"name": NAME, "desktop_id": info["id"], "desktop_number": info["number"],
               "pids": [], "hwnds": []}
        state.put_desktop(rec)
        did = info["id"]

        # 2. Fullscreen viewing on a background desktop.
        shot0 = grab_to("base")
        with Image.open(shot0).convert("RGB") as img:
            colors = img.getcolors(maxcolors=10_000_000)
        check("desktop_see not black", colors is not None and len(colors) > 20,
              f"{len(colors) if colors else 0} colors")
        w, h = Image.open(shot0).size
        print(f"[INFO] desktop {w}x{h}, shots under {state.CAPTURE_DIR}")

        # 3. Deadlock scenario: Win opens Start ON THE TEST DESKTOP.
        hop.do_hop_desktop(did, "key", {"keys": "win"})
        time.sleep(1.2)  # Wait for Start animation (test timing, not server).
        shot1 = grab_to("start-open")
        d1 = changed_pixels(shot0, shot1)
        check("start-opens (pixel diff)", d1 > 5000, f"delta={d1}")

        # 4. Escape closes it - the window level could NOT do that (no window).
        hop.do_hop_desktop(did, "key", {"keys": "escape"})
        time.sleep(0.8)
        shot2 = grab_to("start-closed")
        d2 = changed_pixels(shot1, shot2)
        check("start-closes (pixel diff)", d2 > 5000, f"delta={d2}")

        # 5. Harmless click into empty desktop center (no icon, no double-click).
        r = hop.do_hop_desktop(did, "click", {"x": w // 2, "y": h // 2,
                                              "button": "left", "double": False})
        check("desktop_click ok", "desktop-click" in r.get("ok", ""), str(r))
        check("main visible afterwards", vdesk.current_id() == home)
    except Exception as exc:  # noqa: BLE001
        check("suite without crash", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if state.get_desktop(NAME):
                vdesk.remove_desktop(state.require_desktop(NAME)["desktop_id"])
                state.del_desktop(NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] cleanup: {exc}")
        try:
            vdesk.switch_to(home)
        except Exception:  # noqa: BLE001
            pass

    after = len(vdesk.list_desktops())
    check("desktop count back to baseline", after == baseline_desks,
          f"baseline={baseline_desks} after={after}")
    check("main visible at end", vdesk.current_id() == home)
    print("=" * 60)
    if failures:
        print(f"RESULT: {len(failures)} failed -> {failures}")
        return 1
    print("RESULT: DESKTOP LEVEL GREEN - system UI reachable, Main never touched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
