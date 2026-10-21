"""Window placement: active desktop (Windows) vs. app-dependent?

Question: does a NEW window land on the currently VISIBLE (active)
virtual desktop, or does it depend on the app?

Hypotheses:
  H0 (Windows default): isolated processes land on the active
    desktop. CreateProcess knows no desktop parameter,
    cf. windows.py wait_new_windows ("every new window appears
    on the VISIBLE desktop").
  H1 (app-dependent): some apps restore themselves (last
    position/desktop, session restore) or forward to a running
    process (single-instance/handoff, e.g. Chrome without its own
    profile) - then the app decides, not Windows.

Falsification logic (no blanket verdict):
  - 100% HIT across all isolated attempts (handoff=False), across all
    app types and all active locations (Main/A/B) -> H0 supported.
  - Systematic MISS for exactly one app type (e.g. charmap always Main,
    notepad always active) -> H1 for that type, H0 for the rest.
  - Handoff cases (handoff=True) are NEVER mixed into the H0 rate,
    but reported separately: if they still land active, single-instance
    also follows the active desktop; if they land on Main, THAT is
    app-dependent but does not refute H0 for normal processes.
  - A single MISS without repetition = anomaly, not evidence.
    Only >=2 MISS in the same cell (app x active location) counts as
    systematic. Otherwise: "unclear, repeat", never guess.

Confounders -> countermeasure (false-conclusion protection):
  1. app_launch/move distorts (prod retrieval moves to the box on
     ownership immediately). -> Raw windows.launch_process WITHOUT move.
     Measurement BEFORE every move. No app_launch in the measurement window.
  2. Hook only reports (no hiding since v2). Location proof NEVER
     via visible/invisible, only via pyvda AppView.desktop.
     LESSON FROM RUN 1 (03/10/2026): never-shown windows are UNKNOWN
     to MoveWindowToDesktop/AppView ("element not found",
     cf. selftest.py:84-90) - so BEFORE every desktop_of call
     register the view via show_window (does NOT change the desktop,
     only visibility), then measure. Without show = 100% ERR =
     false "no data" conclusion.
  3. Handoff mixes foreign logic. -> handoff=True strictly separated,
     own rate, own report.
  4. Implicit migration on remove_desktop (Windows moves surviving
     windows to the neighbor desktop). -> Measurement strictly BEFORE
     every remove. Never score cleanup windows. Remove only at the end.
  5. Race: WinUI init/titles arrive ms after SHOW. Too early = "no
     window" false conclusion. -> wait_new_windows with 20s timeout
     (like selftest), desktop_of with retry (5x0.5s). A single failure
     = INCONCLUSIVE, never MISS.
  6. Wrong active desktop (hop/switch in between, foreign Win+Tab).
     -> BEFORE launch verify current_id()==active via poll, AFTER
     launch check again. Deviation = INVALID (neither HIT nor
     MISS). In the measurement window NO hop_*/desktop_* (they switch).
  7. PID/handle reuse, foreign popups (tooltip, tray).
     -> before=all_hwnds(), mapping only via tree_pids + fresh
     hwnds from wait_new_windows. Ignore foreign windows.
  8. Sinks/message-only without client area (not viewable). -> Only
     has_client windows are the primary oracle. None of them =
     INCONCLUSIVE, never MISS.
  9. Pre-occupied desktops / order effects (app remembers desktop).
     -> Create A/B fresh + verify empty, trial order randomized
     (seed logged), fresh process per round, windows stay EMPTY
     (no typing -> no dirty/save dialog like blocker1, no focus race
     like type_focus_test).
 10. UAC/elevated (WinError 740) unreachable by design. -> Only
     non-elevated exes, 740 = SKIP this cell, not FAIL.
 11. Mouse/keyboard during visible switches. -> vdesk.switch_to
     switches VISIBLY (intended trade-off like selftest). Touch
     nothing during the run.
 12. View registration: show before measuring (run-1 lesson, see above).
 13. Foreign protection (run-1 lesson): Notepad (Store WinUI) is currently
     SINGLE-INSTANCE - with a running user host (e.g. pid with dirty
     "*-..." window) EVERY notepad launch hands off to the host.
     -> NEVER kill foreign PIDs (only starter pid), only close foreign
     hwnds when the title is clean (no "*" = no dirty like blocker1),
     otherwise leave alone + report as PROTECTED. H0 is then decided
     only via isolated apps (here: charmap, multi-instance), notepad
     only as handoff evidence for H1. Prior ownership is logged at
     the start.

Trial plan (generic, notepad/charmap examples - no special path):
  Desktops: w-place-a, w-place-b (fresh) + Main.
  Cells: active in {Main, A, B} x app in {notepad.exe, charmap.exe}
    x 3 repetitions = 18 trials, order shuffled.
  Optional: 2x Chrome shared profile (provokes handoff), separated.
  Per trial: activate -> verify -> before -> launch ->
    collect -> check active standstill -> desktop_of -> cleanup
    (close_window + kill_tree, no move, no typing).

NOTE: switches VISIBLY between Main/A/B.
Leave mouse/keyboard alone while it runs.
"""

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\Paul\vision-desktops\src")

from vision_desktops import state, vdesk, windows

NAME_A = "w-place-a"
NAME_B = "w-place-b"
REPEATS = 3
COLLECT_TIMEOUT = 20.0
SEED = 20261003

# Generic examples, no special path: two multi-instance types
# (WinUI Notepad vs. classic Win32 charmap). If one is missing, the
# cell counts as SKIP, not FAIL.
APPS = (
    ("notepad", "notepad.exe", ""),
    ("charmap", "charmap.exe", ""),
)

CHROME = (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
          "--new-window about:blank")


def desktop_of(hwnd: int) -> str:
    def run():
        from pyvda import AppView
        return vdesk.norm_guid(AppView(hwnd=int(hwnd)).desktop.id)
    return vdesk.call(run)


def locate(hwnd: int, tries: int = 5, delay: float = 0.5) -> str:
    """Desktop ID of a fresh window, view-safe (run-1 lesson).

    Never-shown windows are UNKNOWN to AppView ("element not found",
    cf. selftest.py:84-90). So BEFORE every query call show_window
    (registers the view) - it does NOT change the desktop assignment,
    only visibility. Without show every measurement would be ERR =
    false "no data".
    """
    last = ""
    try:
        windows.show_window(hwnd)  # Register view, no move.
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.4)
    for _ in range(tries):
        try:
            return desktop_of(hwnd)
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"[:100]
            # Show once more (transient half-born window), then retry.
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


def cleanup_window(hwnd: int, starter_pid: int, foreign_pids: set | None = None) -> str:
    """Close test window, kill starter process - spare foreign ones.

    - Foreign PIDs (handoff host, e.g. user Notepad with dirty "*-..."
      window, cf. run 1 pid 14456) are NEVER killed.
    - Windows with a dirty star ("*..." like the blocker1 oracle) are NEVER
      closed; they are reported as PROTECTED and left alone.
    - Since we never type, our own test windows are always clean; if the
      guard hits, it was guaranteed not ours -> no data loss.
    Returns: "closed" | "protected-dirty" | "dead".
    """
    foreign_pids = foreign_pids or set()
    try:
        if not windows.is_window(hwnd):
            return "dead"
    except Exception:  # noqa: BLE001
        return "dead"
    t = title_of(hwnd)
    if t.startswith("*"):
        return "protected-dirty"  # User work - do not touch.
    try:
        owner = next((w["pid"] for w in
                      windows.all_windows(include_tool_windows=True)
                      if w["hwnd"] == hwnd), None)
    except Exception:  # noqa: BLE001
        owner = None
    try:
        if windows.is_window(hwnd):
            windows.close_window(hwnd)  # Only this hwnd (cf. close_window docs).
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.8)
    # Kill ONLY our own starter - never the foreign host (blocker3 rule).
    try:
        if starter_pid and (owner is None or starter_pid == owner
                            or starter_pid not in (foreign_pids or set())):
            # starter_pid==owner (isolated) or starter dead after handoff:
            # kill_tree then hits only the dead starter (no-op), never host.
            windows.kill_tree(starter_pid)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.8)
    return "closed"


def main() -> int:
    failures = []
    def check(name: str, ok: bool, detail: str = ""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("window-placement test (active desktop vs. app-dependent)")
    print("=" * 60)
    home = vdesk.current_id()
    main_id = vdesk.main_id()
    check("start on main", home == main_id, f"{home[:8]} vs {main_id[:8]}")
    baseline = len(vdesk.list_desktops())

    for n in (NAME_A, NAME_B):
        nuke(n)
    try:
        results = []  # (app_label, active_label, pid, hwnd, location_id, verdict, detail)
        invalid = 0
        inconclusive = 0
        protected = 0
        skipped_cells = []
        # Snapshot prior ownership (foreign protection): everything alive NOW
        # may belong to the user (e.g. Notepad host with dirty window) and is
        # never killed/closed (run-1 lesson, pid 14456).
        pre_hwnds = windows.all_hwnds()
        pre_pids = {w["pid"] for w in
                    windows.all_windows(include_tool_windows=True)}
        print(f"[INFO] Prior ownership: {len(pre_hwnds)} hwnds, {len(pre_pids)} pids "
              f"(never touched)")

        info_a = vdesk.create_desktop(NAME_A)
        rec_a = {"name": NAME_A, "desktop_id": info_a["id"],
                 "desktop_number": info_a["number"], "pids": [], "hwnds": []}
        state.put_desktop(rec_a)
        info_b = vdesk.create_desktop(NAME_B)
        rec_b = {"name": NAME_B, "desktop_id": info_b["id"],
                 "desktop_number": info_b["number"], "pids": [], "hwnds": []}
        state.put_desktop(rec_b)
        ids = {"Main": main_id, "A": info_a["id"], "B": info_b["id"]}
        print(f"[INFO] active IDs: Main={main_id[:8]} A={info_a['id'][:8]} B={info_b['id'][:8]}")

        # Build trial list + shuffle (break order effects).
        trials = []
        for active_label in ("Main", "A", "B"):
            for app_label, exe, args in APPS:
                for rep in range(REPEATS):
                    trials.append((active_label, app_label, exe, args, rep))
        rng = random.Random(SEED)
        rng.shuffle(trials)
        print(f"[INFO] {len(trials)} trials, seed={SEED}, order shuffled")

        for idx, (active_label, app_label, exe, args, rep) in enumerate(trials):
            tag = f"#{idx:02d} active={active_label} app={app_label} rep={rep}"
            # 1. Activate + verify (poll, no hop in measurement window).
            try:
                vdesk.switch_to(ids[active_label])
            except Exception as exc:  # noqa: BLE001
                print(f"[INVALID] {tag}: switch failed: {exc}")
                invalid += 1
                continue
            if not await_active(ids[active_label]):
                print(f"[INVALID] {tag}: active location not verified")
                invalid += 1
                continue
            time.sleep(0.5)  # DWM settle.

            # 2. Launch raw (NO move, NO app_launch!).
            before = windows.all_hwnds()
            try:
                pid = windows.launch_process(f'"{exe}" {args}'.strip(), cwd=str(Path.home()))
            except RuntimeError as exc:
                msg = str(exc)
                if "740" in msg or "elevated rights" in msg:
                    print(f"[SKIP] {tag}: elevation needed ({exe})")
                    if exe not in skipped_cells:
                        skipped_cells.append(exe)
                    continue
                print(f"[INCONCLUSIVE] {tag}: launch error: {msg[:120]}")
                inconclusive += 1
                continue
            found = windows.wait_new_windows(before, [pid], COLLECT_TIMEOUT)

            # 3. Check standstill: still on active? Else INVALID.
            try:
                still = (vdesk.current_id() == vdesk.norm_guid(ids[active_label]))
            except Exception:  # noqa: BLE001
                still = False
            if not still:
                print(f"[INVALID] {tag}: desktop changed during launch")
                for w in found.get("windows", []):
                    r = cleanup_window(w["hwnd"], pid, pre_pids)
                    if r == "protected-dirty":
                        protected += 1
                try:
                    windows.kill_tree(pid)
                except Exception:  # noqa: BLE001
                    pass
                invalid += 1
                try:
                    vdesk.switch_to(ids[active_label])
                except Exception:  # noqa: BLE001
                    pass
                continue

            # 4. Keep handoff strictly separate (never in H0 rate!).
            if found.get("handoff"):
                for w in found.get("windows", []):
                    try:
                        loc = locate(w["hwnd"])
                    except Exception:
                        loc = "ERR"
                    results.append((app_label, active_label, pid, w["hwnd"], loc, "HANDOFF", tag))
                    print(f"[HANDOFF] {tag}: hwnd={w['hwnd']} pid={w['pid']} on {loc[:8]} (separate)")
                    r = cleanup_window(w["hwnd"], pid, pre_pids)
                    if r == "protected-dirty":
                        protected += 1
                        print(f"[PROTECTED] {tag}: hwnd={w['hwnd']} dirty - left alone")
                try:
                    windows.kill_tree(pid)
                except Exception:  # noqa: BLE001
                    pass
                continue

            cands = [w for w in found.get("windows", []) if windows.has_client(w["hwnd"])]
            if not cands:
                n_all = len(found.get("windows", []))
                print(f"[INCONCLUSIVE] {tag}: no client window (n={n_all}, sinks only?)")
                for w in found.get("windows", []):
                    r = cleanup_window(w["hwnd"], pid, pre_pids)
                    if r == "protected-dirty":
                        protected += 1
                try:
                    windows.kill_tree(pid)
                except Exception:  # noqa: BLE001
                    pass
                inconclusive += 1
                continue

            for w in cands:
                hwnd = w["hwnd"]
                try:
                    loc = locate(hwnd)
                except Exception as exc:  # noqa: BLE001
                    print(f"[INCONCLUSIVE] {tag}: hwnd={hwnd} view ERR: {exc}")
                    results.append((app_label, active_label, pid, hwnd, "ERR", "INCONCLUSIVE", tag))
                    inconclusive += 1
                    r = cleanup_window(hwnd, pid, pre_pids)
                    if r == "protected-dirty":
                        protected += 1
                    continue
                want = vdesk.norm_guid(ids[active_label])
                verdict = "HIT" if loc == want else "MISS"
                results.append((app_label, active_label, pid, hwnd, loc, verdict, tag))
                where = "active" if verdict == "HIT" else f"foreign({loc[:8]} vs active {want[:8]})"
                print(f"[{verdict}] {tag}: hwnd={hwnd} -> {where}")
                r = cleanup_window(hwnd, pid, pre_pids)
                if r == "protected-dirty":
                    protected += 1
                    print(f"[PROTECTED] {tag}: hwnd={hwnd} dirty - left alone")
            try:
                windows.kill_tree(pid)
            except Exception:  # noqa: BLE001
                pass

        # 5. Optional handoff cohort (Chrome shared profile, separated!).
        handoff_rows = [r for r in results if r[5] == "HANDOFF"]
        chrome_rows = []
        if Path(CHROME[0]).exists():
            print(f"[INFO] Chrome handoff cohort (2 rounds, active=A, separate from H0)")
            for rep in range(2):
                tag = f"chrome rep={rep}"
                vdesk.switch_to(ids["A"])
                if not await_active(ids["A"]):
                    print(f"[INVALID] {tag}: active A not verified")
                    invalid += 1
                    continue
                time.sleep(0.5)
                before = windows.all_hwnds()
                try:
                    pid = windows.launch_process(f'"{CHROME[0]}" {CHROME[1]}', cwd=str(Path.home()))
                except RuntimeError as exc:
                    print(f"[SKIP] {tag}: {str(exc)[:100]}")
                    continue
                found = windows.wait_new_windows(before, [pid], COLLECT_TIMEOUT)
                for w in found.get("windows", []):
                    try:
                        loc = locate(w["hwnd"])
                    except Exception:
                        loc = "ERR"
                    verdict = "HIT" if loc == vdesk.norm_guid(ids["A"]) else "MISS"
                    chrome_rows.append((w["hwnd"], loc, verdict, bool(found.get("handoff"))))
                    print(f"[CHROME-{verdict}] hwnd={w['hwnd']} handoff={found.get('handoff')} on {loc[:8]}")
                    # Courtesy: back on Main + cleanly closed (empty new tab).
                    try:
                        vdesk.move_window(w["hwnd"], main_id)
                        windows.show_window(w["hwnd"])
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(0.5)
                    r = cleanup_window(w["hwnd"], pid, pre_pids)
                    if r == "protected-dirty":
                        protected += 1
                try:
                    windows.kill_tree(pid)  # Hits starter, never host (foreign PID not in tree)
                except Exception:  # noqa: BLE001
                    pass
        else:
            print("[INFO] no Chrome -> handoff cohort SKIP")

        # 6. Evaluation (isolated only, HANDOFF never in rate!).
        iso = [r for r in results if r[5] in ("HIT", "MISS")]
        hits = sum(1 for r in iso if r[5] == "HIT")
        miss = sum(1 for r in iso if r[5] == "MISS")
        print("=" * 60)
        print(f"[INFO] isolated: n={len(iso)} HIT={hits} MISS={miss} "
              f"INVALID={invalid} INCONCLUSIVE={inconclusive} "
              f"PROTECTED={protected} SKIP={skipped_cells}")
        # Table per cell.
        cells = {}
        for app_label, active_label, pid, hwnd, loc, verdict, tag in iso:
            cells.setdefault((app_label, active_label), []).append(verdict)
        for (app_label, active_label), vals in sorted(cells.items()):
            print(f"  Cell {app_label} x {active_label}: " +
                  "/".join(vals) + f"  (n={len(vals)})")

        total_assessable = len(iso)
        if total_assessable == 0:
            check("main finding (no data)", False,
                  f"n=0, invalid={invalid} inconclusive={inconclusive} - NO conclusion possible")
        elif (invalid + inconclusive) > total_assessable:
            check("main finding (noisy)", False,
                  f"invalid+inconclusive ({invalid + inconclusive}) > assessable ({total_assessable})"
                  " - NO conclusion, repeat")
        else:
            # Systematic: >=2 MISS in same cell = app-dependent.
            sys_miss = {k: v for k, v in cells.items() if v.count("MISS") >= 2}
            if miss == 0:
                check("H0 supported (all isolated on active desktop)", True)
                print("RESULT: H0 SUPPORTED - Windows places new windows on the "
                      f"active desktop (n={total_assessable}, all HIT, "
                      f"apps={[a for a, _, _ in APPS]})")
            elif sys_miss:
                check("H1 systematic (app-dependent)", True)
                print(f"RESULT: H1 CONFIRMED (systematic) - cells with >=2 MISS: {sys_miss}. "
                      "Placement is (also) app-dependent.")
            else:
                check("single MISS (anomaly, no evidence)", True)
                print(f"RESULT: UNCLEAR - isolated MISS ({miss}/{total_assessable}), "
                      "no pattern. NO blanket verdict, repeat trial.")
                for app_label, active_label, pid, hwnd, loc, verdict, tag in iso:
                    if verdict == "MISS":
                        print(f"  MISS detail: {tag} hwnd={hwnd} on {loc[:8]}")

        if handoff_rows or chrome_rows:
            print(f"[INFO] Handoff separate: spontaneous={len(handoff_rows)} chrome={len(chrome_rows)} "
                  "(never mixed into H0 rate)")

    except Exception as exc:  # noqa: BLE001
        check("suite without crash", False, f"{type(exc).__name__}: {exc}")
    finally:
        for n in (NAME_A, NAME_B):
            nuke(n)
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
    print("RESULT: PLACEMENT TEST ASSESSABLE - see H0/H1 verdict above")
    return 0


if __name__ == "__main__":
    sys.exit(main())
