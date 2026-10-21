"""Self-test: 2 parallel vision desktops + hop input + return to main.

Flow (generic, using Notepad as example - no app-specific path):
  remember main -> create desktops A + B -> launch 1 Notepad each ->
  see (screenshots in parallel) -> hop_type (serialized hopping) ->
  see again + pixel comparison -> verify main is visible again ->
  clean up (kill + remove desktops).

NOTE: hop_type briefly switches visibly to the target desktops and back
(the intended trade-off). Leave mouse/keyboard alone while it runs.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from PIL import Image, ImageChops

from vision_desktops import capture, hop, state, vdesk, windows

NAMES = ("vt-a", "vt-b")
TEXTS = ("VISION ALPHA 123", "VISION BETA 456")


def changed_pixels(a: Path, b: Path) -> int:
    with Image.open(a).convert("L") as ia, Image.open(b).convert("L") as ib:
        if ia.size != ib.size:
            return 10**9
        diff = ImageChops.difference(ia, ib)
        hist = diff.histogram()
        return sum(c for v, c in enumerate(hist) if v > 12)


def main() -> int:
    failures = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("vision-desktops selftest (2 parallel screens + hop)")
    print("=" * 60)
    home = vdesk.current_id()
    print(f"[INFO] main desktop: {home[:8]}... - hopping away briefly and back.")
    baseline = len(vdesk.list_desktops())
    check("desktops listable (pyvda/COM)", baseline >= 1)

    recs = {}
    try:
        for name in NAMES:
            if state.get_desktop(name):
                try:
                    old = state.require_desktop(name)
                    for pid in old.get("pids", []):
                        windows.kill_tree(pid)
                    vdesk.remove_desktop(old["desktop_id"])
                except Exception:  # noqa: BLE001
                    pass
                state.del_desktop(name)
            info = vdesk.create_desktop(name)
            rec = {"name": name, "desktop_id": info["id"], "desktop_number": info["number"],
                   "pids": [], "hwnds": []}
            state.put_desktop(rec)
            recs[name] = rec
            print(f"[INFO] Desktop '{name}' -> #{info['number']}")
        check("2 desktops created", len(recs) == 2)

        # Launch 1 Notepad per desktop generically + move to target desktop.
        for name, rec in recs.items():
            before = windows.all_hwnds()  # IDs only - never blocks.
            pid = windows.launch_process("notepad.exe", cwd=str(Path.home()))
            rec["pids"].append(pid)
            moved = []
            found = windows.wait_new_windows(before, rec["pids"], 20)
            print(f"[INFO] launch '{name}': handoff={found['handoff']} "
                  f"({len(found['windows'])} windows)")
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
                moved.append(w["hwnd"])
            state.put_desktop(rec)
            check(f"notepad on '{name}'", len(moved) >= 1, f"moved={moved}")

        # See in parallel: both screenshots must succeed independently.
        shots_before = {}
        for name, rec in recs.items():
            hwnd = rec["hwnds"][0]
            data, w, h = capture.grab_client(hwnd)
            out = state.CAPTURE_DIR / f"selftest-{name}-before.png"
            state.CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
            out.write_bytes(capture.to_png(data, w, h))
            shots_before[name] = out
        check("see in parallel on 2 desktops", len(shots_before) == 2)

        # Hop serialized: A -> main -> B -> main (the trade-off).
        for (name, rec), text in zip(recs.items(), TEXTS):
            hwnd = rec["hwnds"][0]
            t0 = time.time()
            result = hop.do_hop(rec["desktop_id"], hwnd, "type", {"text": text})
            dt = time.time() - t0
            now = vdesk.current_id()
            check(f"hop_type on '{name}' + back on main",
                  now == home, f"now={now[:8]} home={home[:8]} result={result} ({dt:.1f}s)")

        # Verify: screenshots must have changed (vision!).
        shots_typed = {}
        for (name, rec), text in zip(recs.items(), TEXTS):
            hwnd = rec["hwnds"][0]
            time.sleep(0.5)
            data, w, h = capture.grab_client(hwnd)
            out = state.CAPTURE_DIR / f"selftest-{name}-after.png"
            out.write_bytes(capture.to_png(data, w, h))
            shots_typed[name] = out
            delta = changed_pixels(shots_before[name], out)
            check(f"vision proof '{name}' (pixel delta {delta})", delta > 300, f"delta={delta}")

        # hop_key regression: select-all + delete must visibly apply
        # (catches KEYBDINPUT struct bugs) and empties the document,
        # so no save dialog blocks later.
        for name, rec in recs.items():
            hwnd = rec["hwnds"][0]
            hop.do_hop(rec["desktop_id"], hwnd, "key", {"keys": "ctrl+a"})
            hop.do_hop(rec["desktop_id"], hwnd, "key", {"keys": "delete"})
            now = vdesk.current_id()
            check(f"hop_key on '{name}' + back on main", now == home, f"now={now[:8]}")
            time.sleep(0.5)
            data, w, h = capture.grab_client(hwnd)
            out = state.CAPTURE_DIR / f"selftest-{name}-cleared.png"
            out.write_bytes(capture.to_png(data, w, h))
            delta = changed_pixels(shots_typed[name], out)
            check(f"key effect '{name}' (pixel delta {delta})", delta > 300, f"delta={delta}")

        # Long-text regression (exact match, not just pixels): WinUI Notepad
        # scrambles/truncates fire-and-forget typing beyond ~39 chars (async
        # TSF layout + show_main after fixed 0.25s). Long input therefore goes
        # via clipboard+paste. Read back the user-equivalent way: ctrl+a,
        # ctrl+c, read clipboard - no window-API peek (the test only uses
        # what a human could see/read).
        lines = [
            f"Line {i:02d}: Greetings AE OE UE ss ae oe ue 1234567890 "
            f"Pack my box with five dozen liquor jugs {i * 7}"
            for i in range(1, 61)
        ]
        long_text = "\n".join(lines)
        rec_a = recs[NAMES[0]]
        hwnd_a = rec_a["hwnds"][0]
        # Like a real agent loop: first look via see, then click into the
        # field (place caret - after several background hops the XAML focus
        # is not guaranteed in the editor), then type.
        data, w, h = capture.grab_client(hwnd_a)
        hop.do_hop(rec_a["desktop_id"], hwnd_a, "click",
                   {"x": w // 2, "y": h // 2, "button": "left", "double": False})
        hop.do_hop(rec_a["desktop_id"], hwnd_a, "type", {"text": long_text})
        now = vdesk.current_id()
        check("hop paste long + back on main", now == home, f"now={now[:8]}")
        time.sleep(0.5)
        data, w, h = capture.grab_client(hwnd_a)
        (state.CAPTURE_DIR / "selftest-vt-a-pasted.png").write_bytes(
            capture.to_png(data, w, h))
        hop.do_hop(rec_a["desktop_id"], hwnd_a, "key", {"keys": "ctrl+a"})
        hop.do_hop(rec_a["desktop_id"], hwnd_a, "key", {"keys": "ctrl+c"})
        got = hop.clipboard_get().replace("\r\n", "\n")
        if got == long_text:
            check(f"long text exact ({len(long_text)} chars, ASCII ok)", True)
        else:
            off = next((i for i, (a, b) in enumerate(zip(got, long_text)) if a != b),
                       min(len(got), len(long_text)))
            check("long text exact", False,
                  f"len got={len(got)} want={len(long_text)} "
                  f"first-diff@{off}: got={got[off:off+40]!r} want={long_text[off:off+40]!r}")

    except Exception as exc:  # noqa: BLE001
        check("suite without crash", False, f"{type(exc).__name__}: {exc}")
    finally:
        import ctypes as _ct

        def alive(hwnd: int) -> bool:
            try:
                return bool(_ct.windll.user32.IsWindow(_ct.c_void_p(hwnd)))
            except Exception:  # noqa: BLE001
                return False

        for name, rec in recs.items():
            try:
                # Order matters: first empty (no save dialog),
                # then close gently. Only touches own hwnds - foreign
                # processes (single-instance!) are never killed.
                for hwnd in rec.get("hwnds", []):
                    if alive(hwnd):
                        try:
                            hop.do_hop(rec["desktop_id"], hwnd, "key", {"keys": "ctrl+a"})
                            hop.do_hop(rec["desktop_id"], hwnd, "key", {"keys": "delete"})
                        except Exception as exc:  # noqa: BLE001
                            print(f"[WARN] clear {hwnd}: {exc}")
                time.sleep(0.5)
                for hwnd in rec.get("hwnds", []):
                    if alive(hwnd):
                        windows.close_window(hwnd)
                for pid in rec.get("pids", []):
                    windows.kill_tree(pid)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(2.0)
        leaked = [hwnd for rec in recs.values() for hwnd in rec.get("hwnds", []) if alive(hwnd)]
        if leaked:
            print(f"[WARN] Windows blocking (dialog?): {leaked} - please close via vision")
        for name in NAMES:
            try:
                rec = state.get_desktop(name)
                if rec:
                    vdesk.remove_desktop(rec["desktop_id"])
                    state.del_desktop(name)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] cleanup {name}: {exc}")
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
    print("RESULT: SELFTEST GREEN - 2 screens parallel, hop serialized, main restored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
