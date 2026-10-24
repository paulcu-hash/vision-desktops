"""diagnose: show what the server swallowed - the ring buffer + health.

CLI:   python -m vision_desktops.diagnose            (last 20 entries)
       python -m vision_desktops.diagnose 50         (last 50)
       python -m vision_desktops.diagnose --demo     (deliberately raise an
        ctypes.ArgumentError, show it in the log, exit - the acceptance
        test for "errors are visible now")
MCP:   the `diagnose` tool returns the same report as text.

Reading is process-local: run the CLI in the same process to see its
buffer; the MCP tool shows the buffer of the running server.
"""
import argparse
import json
import platform
import sys

from . import errors


def _env_facts() -> list:
    """Cheap health facts - each one best effort, failures reported as text
    (a diagnose tool that crashes on a broken subsystem is worthless)."""
    facts = []
    try:
        import psutil
        facts.append(f"python {platform.python_version()} on {platform.platform()}")
        facts.append(f"process rss {psutil.Process().memory_info().rss // 1024 // 1024} MB")
    except Exception as exc:  # noqa: BLE001
        facts.append(f"psutil: {type(exc).__name__}: {exc}")
    try:
        from . import vdesk
        desks = vdesk.list_desktops()
        facts.append(f"virtual desktops: {len(desks)} "
                     f"({', '.join(str(d.get('number')) for d in desks)})")
    except Exception as exc:  # noqa: BLE001
        facts.append(f"vdesk: {type(exc).__name__}: {exc} "
                     "(pyvda/COM broken -> hops will fail)")
    try:
        from . import windows
        n = len(windows.all_windows(visible_only=True))
        facts.append(f"visible windows enumerable: {n}")
    except Exception as exc:  # noqa: BLE001
        facts.append(f"windows: {type(exc).__name__}: {exc} "
                     "(enumeration broken -> argtypes/Win32 problem?)")
    return facts


def report(limit: int = 20) -> str:
    """Human-readable diagnosis: health facts + the ring buffer."""
    ring = errors.ring_handler()
    out = ["=== vision-desktops diagnose ==="]
    out += [f"- {f}" for f in _env_facts()]
    if ring is None:
        out.append("- ring buffer: NOT INSTALLED (errors.install() never ran)")
    else:
        entries = ring.recent(limit)
        out.append(f"- ring buffer: {len(entries)}/{ring.capacity} entries "
                   f"(newest first, showing {min(limit, len(entries))})")
        if not entries:
            out.append("  (empty - nothing logged since install/clear)")
        for i, e in enumerate(entries, 1):
            out.append(f"\n[{i}] {e['time']} {e['level']} {e['logger']}")
            out.append(f"    {e['message']}")
            if e["traceback"]:
                for line in e["traceback"].splitlines():
                    out.append(f"    | {line}")
    return "\n".join(out)


def demo_argumenterror() -> str:
    """Deliberately provoke the EXACT historic ctypes.ArgumentError and show
    it in the log. Acceptance demo for the visibility work - it must appear
    in the ring buffer with a traceback.

    Two provocations:
      1. The argtypes-clobbering error class itself: a foreign RECT class
         passed to a function whose argtypes expect windows.RECT -> exactly
         "expected LP_RECT instance instead of pointer to RECT" (what other
         codebases used to hit on the shared ctypes.windll singleton before
         the own-WinDLL fix in windows.py/capture.py/hop.py).
      2. A real product swallow spot (vdesk._top: its former silent `pass`
         now logs with exc_info=True) - proving former swallow sites record.
    """
    import ctypes
    import logging

    errors.install()
    log = logging.getLogger(errors.LOGGER_NAME)
    from . import vdesk, windows  # local import: diagnose must work even if
    #                              a subsystem is broken
    ghost = "not-an-hwnd-demo"

    # (1) historic error class - provoked on a FRESH instance (no global
    # state touched, the pre-fix code did this on the shared singleton).
    class RECT(ctypes.Structure):  # "foreign" module's own RECT class
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    provocation = "none (BUG: no error was raised)"
    fresh = ctypes.WinDLL("user32")
    fresh.GetClientRect.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(windows.RECT)]
    try:
        fresh.GetClientRect(ctypes.c_void_p(0), ctypes.byref(RECT()))
    except ctypes.ArgumentError as exc:
        provocation = str(exc)
        # same pathway as the 91 transformed swallow spots:
        log.debug("swallowed in diagnose.demo_argumenterror() "
                  "(deliberately provoked ctypes.ArgumentError)",
                  exc_info=True)

    # (2) real product swallow spot (former `except: pass` in vdesk._top)
    vdesk._top(ghost, True)

    entries = errors.recent(10)
    ae = next((e for e in entries
               if "ctypes.ArgumentError:" in e.get("traceback", "")), {})
    top = next((e for e in entries
                if "swallowed in _top()" in e.get("message", "")), {})
    ok = bool(ae.get("traceback")) and bool(top.get("traceback"))
    shown = [
        f"deliberately provoked ctypes.ArgumentError: {provocation}",
        f"[1] historic error class in ring buffer: "
        f"{'YES' if ae.get('traceback') else 'NO (BUG)'}",
        f"     {ae.get('level')} {ae.get('logger')}: {ae.get('message')}",
        f"[2] product swallow spot (vdesk._top) in ring buffer: "
        f"{'YES' if top.get('traceback') else 'NO (BUG)'}",
        f"     {top.get('level')} {top.get('logger')}: {top.get('message')}",
        f"-> ACCEPTANCE {'PASSED' if ok else 'FAILED'} "
        f"(errors visible with traceback: {ok})",
    ]
    return "\n".join(shown)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="vision-desktops.diagnose",
                                description="Show swallowed errors "
                                            "(ring buffer) + health.")
    p.add_argument("limit", nargs="?", type=int, default=20,
                   help="how many ring entries (default 20)")
    p.add_argument("--demo", action="store_true",
                   help="raise a deliberate ctypes.ArgumentError and show "
                        "it in the log (acceptance demo)")
    p.add_argument("--json", action="store_true",
                   help="machine-readable ring buffer dump")
    a = p.parse_args(argv)
    errors.install()  # the CLI process has its own buffer - install so the
    #                  health checks below can already record into it
    if a.demo:
        print(demo_argumenterror())
        print("\n(full ring buffer below)")
    if a.json:
        print(json.dumps({"facts": _env_facts(),
                          "ring": errors.recent(a.limit)}, indent=2))
    else:
        print(report(a.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
