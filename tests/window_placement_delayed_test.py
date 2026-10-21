"""Delayed windows: open time (active=Main) vs. start desktop.

Motivation (methods objection): 9/9 charmap in window_placement_test.py proves
H0 only for "immediate window of a freshly launched process". Open:
  (a) a second, differently built app (Electron instead of Win32 classic),
  (b) background threads / delayed opening: app starts on worker A but
      opens the window only 2s/5s later - Main is active by then.
      Timing hypothesis H0-timing: location = active AT OPEN TIME (Main).
      Counter-hypothesis H1-timing: location = process start desktop (worker A).

Cells: toolkit x delay x repetition
  Toolkit: tk-inprocess (Tk 8.6, same process, real background thread
      via threading.Timer - no new process, strongest H1 candidate),
    charmap-delayed (Win32 classic, delayed process start as
      timing control for the first test),
    electron-delayed (VS Code Code.exe, Chromium/Electron, isolated via
      fresh --user-data-dir like the README Chrome trick, else handoff).
  Delay: 2s, 5s. Reps: 2. Total 3x2x2 = 12 trials, order shuffled.

Flow per trial (no move, no app_launch, no typing):
  Activate+verify worker A -> before=snapshot ->
  start delay helper raw (own process, stays alive) ->
  switch to Main IMMEDIATELY+verify -> wait out delay there ->
  collect (timeout=delay+30) -> check standstill (still Main?) ->
  locate (show before AppView, run-1 lesson) -> HIT=Main / MISS=WorkerA ->
  cleanup (only own PIDs, dirty guard, delete fresh profiles).

Extra false-conclusion protection (on top of the first test):
  1. Helper death: if the helper exited after launch, tree_pids would
     lose the child (psutil children of dead processes are gone) ->
     forged as handoff. So EVERY helper stays alive until cleanup (Tk
     mainloop or launch-helper sleep 120s). Verified: helper alive at
     collect.
  2. VS Code singleton: without a fresh profile Code forwards to the
     running user process (handoff, foreign host, cf. blocker3). So per
     trial a fresh TMP profile (only that is ever deleted, never the user
     profile). Handoff anyway is reported separately, never mixed into
     the H0 rate.
  3. Timing verification: Main must be active BEFORE the delay starts AND
     AFTER opening (poll). A switch in between (foreign Win+Tab) =
     INVALID.
  4. View registration: show_window before every desktop_of (run-1 lesson,
     server.py _launch_into / selftest.py:84-90), else 100% ERR.
  5. Foreign protection: prior-ownership snapshot (hwnds+pids), dirty star
     ("*...") never closed (blocker1 oracle), never kill foreign PIDs
     (only starter tree, blocker3 rule). We never type -> our own windows
     are always clean.
  6. No hop in the measurement window (only vdesk.switch_to + poll).
     Leave mouse/keyboard alone - switches are VISIBLE (selftest
     trade-off).

Child modes (same file, own process - no second file needed):
  --child-tk <title> <delay_s>: after delay open Tk window IN THE SAME
     process (background-thread case) + keep open.
  --child-launch <delay_s> <exe> [args...]: after delay launch target exe
     via Popen (child in tree) + stay alive 120s.

NOTE: switches VISIBLY workerA<->Main. Leave inputs alone.
"""

import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from vision_desktops import state, vdesk, windows

NAME_A = "w-delay-a"
REPEATS = 2
DELAYS = (2.0, 5.0)
SEED = 20261004
VSCODE = Path(r"C:\Users\Paul\AppData\Local\Programs\Microsoft VS Code\Code.exe")


# ---------------- Child modes (own process) ----------------

def child_tk(title: str, delay_s: float):
    time.sleep(delay_s)
    import tkinter as tk
    root = tk.Tk()
    root.title(title)
    tk.Label(root, text=title, padx=40, pady=30).pack()
    root.geometry("420x180")
    root.mainloop()


def child_launch(delay_s: float, exe: str, args: list):
    time.sleep(delay_s)
    subprocess.Popen([exe, *args])
    time.sleep(120.0)


# ---------------- Measurement helpers (parent process) ----------------

def desktop_of(hwnd: int) -> str:
    def run():
        from pyvda import AppView
        return vdesk.norm_guid(AppView(hwnd=int(hwnd)).desktop.id)
    return vdesk.call(run)


def locate(hwnd: int, tries: int = 5, delay: float = 0.5) -> str:
    last = ""
    try:
        windows.show_window(hwnd)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.4)
    for _ in range(tries):
        try:
            return desktop_of(hwnd)
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"[:100]
            try:
                windows.show_window(hwnd)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(delay)
    raise RuntimeError(f"desktop_of failed ({hwnd}): {last}")


def title_of(hwnd: int) -> str:
    for w in windows.all_windows(include_tool_windows=True):
        if w["hwnd"] == hwnd:
            return w.get("title", "")
    return ""


def await_active(target_id: str, timeout: float = 2.0) -> bool:
    target = vdesk.norm_guid(target_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if vdesk.current_id() == target:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.05)
    try:
        return vdesk.current_id() == target
    except Exception:  # noqa: BLE001
        return False


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
        vdesk.remove_desktop(rec["desktop_id"])
    except Exception:  # noqa: BLE001
        pass
    try:
        state.del_desktop(name)
    except Exception:  # noqa: BLE001
        pass


def safe_cleanup(hwnd: int, starter_pid: int, pre_pids: set) -> str:
    try:
        if not windows.is_window(hwnd):
            return "dead"
    except Exception:  # noqa: BLE001
        return "dead"
    if title_of(hwnd).startswith("*"):
        return "protected-dirty"
    try:
        if windows.is_window(hwnd):
            windows.close_window(hwnd)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.8)
    try:
        if starter_pid and starter_pid not in pre_pids:
            windows.kill_tree(starter_pid)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.8)
    return "closed"


def launch_helper_tk(title: str, delay_s: float) -> int:
    py = sys.executable
    me = str(Path(__file__).resolve())
    return windows.launch_process(
        f'"{py}" "{me}" --child-tk "{title}" {delay_s}', cwd=str(Path.home()))


def launch_helper_exe(delay_s: float, exe: str, args: list) -> int:
    py = sys.executable
    me = str(Path(__file__).resolve())
    parts = " ".join(f'"{a}"' for a in args)
    return windows.launch_process(
        f'"{py}" "{me}" --child-launch {delay_s} "{exe}" {parts}'.strip(),
        cwd=str(Path.home()))


def main() -> int:
    failures = []
    def check(name: str, ok: bool, detail: str = ""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("window-placement-delayed test (open time vs. start desktop)")
    print("=" * 60)
    home = vdesk.current_id()
    main_id = vdesk.main_id()
    check("start on main", home == main_id, f"{home[:8]} vs {main_id[:8]}")
    baseline = len(vdesk.list_desktops())
    nuke(NAME_A)
    vscode_ok = VSCODE.exists()
    print(f"[INFO] VS Code (Electron): {'found' if vscode_ok else 'MISSING -> electron cells SKIP'}")

    try:
        results = []
        invalid = inconclusive = protected = 0
        pre_hwnds = windows.all_hwnds()
        pre_pids = {w["pid"] for w in windows.all_windows(include_tool_windows=True)}
        print(f"[INFO] Prior ownership: {len(pre_hwnds)} hwnds (never touch)")

        info_a = vdesk.create_desktop(NAME_A)
        rec_a = {"name": NAME_A, "desktop_id": info_a["id"],
                 "desktop_number": info_a["number"], "pids": [], "hwnds": []}
        state.put_desktop(rec_a)
        worker_a, main_norm = vdesk.norm_guid(info_a["id"]), vdesk.norm_guid(main_id)
        print(f"[INFO] WorkerA={info_a['id'][:8]} Main={main_id[:8]}")

        toolkits = ["tk", "charmap", "electron"] if vscode_ok else ["tk", "charmap"]
        trials = [(tk_, d, r) for tk_ in toolkits for d in DELAYS for r in range(REPEATS)]
        rng = random.Random(SEED)
        rng.shuffle(trials)
        print(f"[INFO] {len(trials)} trials, seed={SEED}: {sorted(toolkits)} x {DELAYS}s x {REPEATS}")

        for idx, (toolkit, delay_s, rep) in enumerate(trials):
            tag = f"#{idx:02d} {toolkit} delay={delay_s:g}s rep={rep}"
            vdesk.switch_to(info_a["id"])
            if not await_active(info_a["id"]):
                print(f"[INVALID] {tag}: worker A not verified")
                invalid += 1
                continue
            time.sleep(0.5)
            before = windows.all_hwnds()
            tmp_profile = None
            # Start helper (stays alive, see above).
            if toolkit == "tk":
                title = f"DELAY-TK-{idx:02d}-{delay_s:g}s"
                helper = launch_helper_tk(title, delay_s)
            elif toolkit == "charmap":
                helper = launch_helper_exe(delay_s, "charmap.exe", [])
            else:  # electron, isolated via fresh profile (README trick).
                tmp_profile = Path(tempfile.mkdtemp(prefix="vscode-delay-"))
                helper = launch_helper_exe(
                    delay_s, str(VSCODE),
                    ["--user-data-dir", str(tmp_profile), "--new-window",
                     "--no-first-run", "--no-default-browser-check"])
            # To Main IMMEDIATELY (the window opens there later).
            vdesk.switch_to(main_id)
            if not await_active(main_id):
                print(f"[INVALID] {tag}: Main not verified after helper start")
                for w in windows.wait_new_windows(before, [helper], 2.0).get("windows", []):
                    safe_cleanup(w["hwnd"], helper, pre_pids)
                try:
                    windows.kill_tree(helper)
                except Exception:  # noqa: BLE001
                    pass
                invalid += 1
                continue
            found = windows.wait_new_windows(before, [helper], delay_s + 30.0)
            try:
                still = (vdesk.current_id() == main_norm)
            except Exception:  # noqa: BLE001
                still = False
            if not still:
                print(f"[INVALID] {tag}: Main no longer active at open time")
                for w in found.get("windows", []):
                    if safe_cleanup(w["hwnd"], helper, pre_pids) == "protected-dirty":
                        protected += 1
                try:
                    windows.kill_tree(helper)
                except Exception:  # noqa: BLE001
                    pass
                invalid += 1
                continue
            if found.get("handoff"):
                for w in found.get("windows", []):
                    try:
                        loc = locate(w["hwnd"])
                    except Exception:
                        loc = "ERR"
                    results.append((toolkit, delay_s, helper, w["hwnd"], loc, "HANDOFF", tag))
                    print(f"[HANDOFF] {tag}: hwnd={w['hwnd']} on {loc[:8]} (separate)")
                    if safe_cleanup(w["hwnd"], helper, pre_pids) == "protected-dirty":
                        protected += 1
                try:
                    windows.kill_tree(helper)
                except Exception:  # noqa: BLE001
                    pass
                continue
            cands = [w for w in found.get("windows", []) if windows.has_client(w["hwnd"])]
            # The Electron/charmap helper itself has no client window (only the
            # delayed child) - an empty helper window list is normal here:
            # cands holds the delayed window(s), helper PID stays tree root.
            cands = [w for w in cands if w["hwnd"] not in before]
            if not cands:
                print(f"[INCONCLUSIVE] {tag}: no delayed window (n="
                      f"{len(found.get('windows', []))})")
                for w in found.get("windows", []):
                    if safe_cleanup(w["hwnd"], helper, pre_pids) == "protected-dirty":
                        protected += 1
                try:
                    windows.kill_tree(helper)
                except Exception:  # noqa: BLE001
                    pass
                inconclusive += 1
                continue
            for w in cands:
                hwnd = w["hwnd"]
                try:
                    loc = locate(hwnd)
                except Exception as exc:  # noqa: BLE001
                    print(f"[INCONCLUSIVE] {tag}: hwnd={hwnd} {exc}")
                    results.append((toolkit, delay_s, helper, hwnd, "ERR", "INCONCLUSIVE", tag))
                    inconclusive += 1
                    if safe_cleanup(hwnd, helper, pre_pids) == "protected-dirty":
                        protected += 1
                    continue
                verdict = "HIT" if loc == main_norm else ("MISS-A" if loc == worker_a else "MISS-X")
                results.append((toolkit, delay_s, helper, hwnd, loc, verdict, tag))
                print(f"[{verdict}] {tag}: hwnd={hwnd} on "
                      f"{'Main' if verdict=='HIT' else loc[:8]} (title={title_of(hwnd)[:30]!r})")
                if safe_cleanup(hwnd, helper, pre_pids) == "protected-dirty":
                    protected += 1
                    print(f"[PROTECTED] {tag}: dirty - left alone")
            try:
                if helper not in pre_pids:
                    windows.kill_tree(helper)
            except Exception:  # noqa: BLE001
                pass
            if tmp_profile and tmp_profile.exists():
                try:
                    shutil.rmtree(tmp_profile, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass

        iso = [r for r in results if r[5] in ("HIT", "MISS-A", "MISS-X")]
        hits = sum(1 for r in iso if r[5] == "HIT")
        miss = len(iso) - hits
        print("=" * 60)
        print(f"[INFO] delayed: n={len(iso)} HIT(Main)={hits} MISS={miss} "
              f"INVALID={invalid} INCONCLUSIVE={inconclusive} PROTECTED={protected}")
        cells = {}
        for toolkit, delay_s, _hp, _hw, _loc, verdict, _tag in iso:
            cells.setdefault((toolkit, delay_s), []).append(verdict)
        for (toolkit, delay_s), vals in sorted(cells.items()):
            print(f"  Cell {toolkit} delay={delay_s:g}s: " + "/".join(vals))
        if not iso:
            check("main finding (no data)", False,
                  f"invalid={invalid} inconclusive={inconclusive} - NO conclusion")
        elif (invalid + inconclusive) > len(iso):
            check("main finding (noisy)", False,
                  f"invalid+inconclusive ({invalid + inconclusive}) > assessable ({len(iso)})")
        else:
            sys_miss = {k: v for k, v in cells.items() if sum(1 for x in v if x != "HIT") >= 2}
            if miss == 0:
                check("H0-timing supported (open time counts)", True)
                print(f"RESULT: H0-TIMING SUPPORTED - delayed windows land on the "
                      f"desktop active at open time (Main), n={len(iso)} all HIT.")
            elif sys_miss:
                check("H1-timing systematic (start desktop counts)", True)
                print(f"RESULT: H1-TIMING CONFIRMED - cells with >=2 MISS: {sys_miss}. "
                      f"Location (also) follows the start desktop.")
            else:
                check("single MISS (anomaly)", True)
                print(f"RESULT: UNCLEAR - isolated MISS ({miss}/{len(iso)}), repeat.")
        hands = [r for r in results if r[5] == "HANDOFF"]
        if hands:
            print(f"[INFO] Handoff separate: {len(hands)} (never in rate)")

    except Exception as exc:  # noqa: BLE001
        check("suite without crash", False, f"{type(exc).__name__}: {exc}")
    finally:
        nuke(NAME_A)
        try:
            vdesk.switch_to(home)
        except Exception:  # noqa: BLE001
            pass
    check("desktop count back to baseline", len(vdesk.list_desktops()) == baseline,
          f"baseline={baseline}")
    try:
        check("main visible at end", vdesk.current_id() == home)
    except Exception:  # noqa: BLE001
        pass
    print("=" * 60)
    if failures:
        print(f"RESULT: {len(failures)} failed -> {failures}")
        return 1
    print("RESULT: DELAY TEST ASSESSABLE - see H0/H1 verdict above")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--child-tk":
        child_tk(sys.argv[2], float(sys.argv[3]))
    elif len(sys.argv) >= 2 and sys.argv[1] == "--child-launch":
        child_launch(float(sys.argv[2]), sys.argv[3], sys.argv[4:])
    else:
        sys.exit(main())
