"""vision-desktops MCP server: parallel vision screens for every agent.

Model-agnostic: works with any MCP-capable agent
(Codex, OpenCode, Claude Code, ...). The main agent runs on the
visible main desktop and orchestrates any number of invisible
virtual desktops - 2 or 100, all DWM-rendered.

Loop per desktop (100% vision, generic for ANY window program):
  see -> decide -> hop_click / hop_type / hop_key -> repeat
Second stage for system UI without app windows (Start/Run/Tray):
  desktop_see -> decide -> desktop_click / desktop_type / desktop_key
  -> repeat (hard-refuses Main, same guards, same input path).

Screenshots run in parallel, inputs hop serialized:
  remember main -> target desktop -> click/type -> back to main.
"""

import base64
import json
import re
import time
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from . import capture, errors, hop, messages, retrieval, state, vdesk, windows
from . import diagnose as diagnose_module

import logging

log = logging.getLogger(__name__)


mcp = FastMCP(
    "vision-desktops",
    instructions=(
        "Parallel vision-based computer use on Windows virtual desktops. "
        "The main agent runs on the visible main desktop and orchestrates "
        "any number of background desktops (Win+Tab level via pyvda, NOT "
        "CreateDesktop). Every action is 100% vision: coordinates always "
        "come from the latest `see` screenshot of the same window. "
        "Screenshots run in parallel; inputs hop serially: the server "
        "switches to the target desktop, performs REAL mouse/keyboard "
        "input, and returns to main - cursor restored. "
        "Loop per desktop: see -> decide -> hop_click/hop_type/hop_key/hop_scroll "
        "-> repeat. Interleave desktops for parallelism. Generic for ANY "
        "window program: locate windows only via hwnd/title_contains, "
        "never assume app layout. "
        "Second stage for OS chrome without app windows (Start menu, Run "
        "dialog, tray): desktop_see -> decide -> "
        "desktop_click/desktop_type/desktop_key/desktop_scroll -> repeat. Same vision rule "
        "(fullscreen pixels are the only basis), same real-input path, no "
        "window handle needed. desktop_* REFUSES the main/host desktop by "
        "hard assertion - worker desktops only. "
        "UAC/Secure-Desktop prompts are unreachable by OS design, not a bug."
        "Removal protocol (no silent close): never call desktop_remove blind. "
        "First window_list -> see (dirty? dialog? needed later?) and judge per "
        "window: save via hop_*, park on graveyard, or approve drop via "
        "decisions={hwnd: 'drop'|'park'|'save'}. Without verdict only parked, "
        "never deleted; removed:true alone never means content was clean. "
        "Single-instance apps (browsers, Electron, modern Store apps) hand "
        "new windows over to an already running process: those windows belong "
        "to a foreign process and launch reports handoff:true. For real "
        "isolation, start such apps with a separate data/profile directory "
        "per desktop if they support it (e.g. browsers take a separate "
        "user-data directory per desktop, plus a separate remote-debugging "
        "port per desktop for CDP). "
        "Event-driven retrieval (v2): auto-move only with proven ownership "
        "(job hit or process tree). Foreign-host candidates always go to "
        "delta (hwnd/pid/title/time only, no Main screenshot) and stay on "
        "Main until explicit adoption. New guards: interaction guard (no "
        "move when user-touched) and collision clause (queue moves during "
        "see->hop sessions until rest, 2s cap then delta). Adoption is an "
        "explicit agent step: delta_list -> delta_view (single on-demand "
        "view, no retention) -> window_adopt (vision verdict, pin and "
        "user-touched guards still apply). Pinned windows/apps never move."
    ),
)

NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

# Graveyard desktop: dedicated, permanent, one reserved worker rec.
# Rationale: remove_desktop() would IMPLICITLY migrate surviving windows to a
# neighbor desktop (Windows default behavior, possibly visible on
# Main). Instead we park them EXPLICITLY here - via the existing
# move_window mechanism (changes neither parent nor owner, only the
# desktop assignment; invisible desktop = no frame, no focus).
# As a normal rec, parked windows stay reachable via hop_*/desktop_* tools.
GRAVEYARD = "graveyard"
GRAVEYARD_MAX_WINDOWS = 10

# Open verification step (collision clause): after hop_* the box is in
# session (see->hop->see), moves wait until rest. Rest = hop mutex free AND
# no open verify step. Verify opens on hop_*, closes on see (with a 30-s
# timeout against hangs).
_PENDING_VERIFY: dict = {}
VERIFY_GRACE_S = 30.0


def _verify_open(desktop: str) -> None:
    _PENDING_VERIFY[desktops_key(desktop)] = time.time()


def _verify_close(desktop: str) -> None:
    _PENDING_VERIFY.pop(desktops_key(desktop), None)


def desktops_key(name: str) -> str:
    return str(name)


def _is_rest() -> bool:
    if hop.hop_busy():
        return False
    now = time.time()
    for _k, ts in list(_PENDING_VERIFY.items()):
        if (now - float(ts)) < VERIFY_GRACE_S:
            return False
    # Clean up expired entries.
    for _k, ts in list(_PENDING_VERIFY.items()):
        if (now - float(ts)) >= VERIFY_GRACE_S:
            _PENDING_VERIFY.pop(_k, None)
    return True


retrieval.set_rest_fn(_is_rest)


def _txt(obj) -> list:
    if isinstance(obj, str):
        return [TextContent(type="text", text=obj)]
    return [TextContent(type="text", text=json.dumps(obj, ensure_ascii=False, indent=2))]


def _err(msg: str):
    raise ToolError(msg)


def _require_desktop(name: str) -> dict:
    """Load a desktop record with an English error for unknown names."""
    try:
        return state.require_desktop(name)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "Unbekannter Desktop" in text:
            _err(messages.unknown_desktop(name))
        _err(messages.translate_external_error(text))


def _tool_err(exc: Exception):
    """Surface tool failures in English (maps legacy German pass-through)."""
    return _err(messages.translate_external_error(str(exc)))


def _shot_blocks(desktop: str, target: dict, max_width: int,
                 embed_image: bool = True, payload: dict | None = None) -> list:
    """Text-Meta + optionales JPEG eines Fensters - EINE Codebasis fuer
    `see` und die hop_*-Tools mit see_after.

    `payload` (z.B. das hop-Ergebnis) kommt UNTER die Shot-Meta: so
    liefert ein einziger Tool-Call Aktion und Ergebnisbild."""
    data, w, h = capture.grab_client(target["hwnd"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    # PNG nur auf Wunsch (VISION_DESKTOPS_SAVE_CAPTURES): sonst kein File,
    # kein saved_to - das JPEG in der Antwort ist ohnehin das, was der
    # Agent sieht. Default: der captures-Ordner bleibt leer.
    path = state.save_capture(f"{desktop}-{stamp}.png",
                              capture.to_png(data, w, h))
    shot = {"window_title": target["title"],
            "hwnd": target["hwnd"], "size_px": [w, h],
            "image_px": list(capture.fit_width(w, h, max(320, max_width))),
            "hint": messages.SEE_HINT}
    if path:
        shot["saved_to"] = str(path)
    meta = {**shot, **(payload or {})}
    blocks: list = [TextContent(type="text",
                                text=json.dumps(meta, ensure_ascii=False))]
    if embed_image:
        jpeg = capture.to_jpeg(data, w, h, max_width=max(320, max_width))
        blocks.append(ImageContent(type="image",
                                   data=base64.b64encode(jpeg).decode("ascii"),
                                   mimeType="image/jpeg"))
    return blocks


def _hop_out(desktop: str, target: dict, result, see_after: bool,
             max_width: int) -> list:
    """Ausgabe eines hop_*-Tools: Ergebnis, bei see_after ZUSAEMTZLICH das
    Ergebnisbild im SELBEN Call - spart pro Aktion einen LLM-Roundtrip
    (kein separates `see` mehr noetig).

    Das Bild uebernimmt die Verifikation (_verify_close), genau wie `see`.
    Schlaegt der Screenshot fehl, bleibt das Hop-Ergebnis als Text erhalten
    (ein Aktionsergebnis darf nie durch einen Shot verloren gehen)."""
    _verify_open(desktop)
    if not see_after:
        return _txt(result)
    try:
        payload = dict(result) if isinstance(result, dict) else {"result": result}
        blocks = _shot_blocks(desktop, target, max_width, payload=payload)
        _verify_close(desktop)
        return blocks
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _hop_out() (see_after shot failed, "
                  "falling back to text)", exc_info=True)
        return _txt(result)


def _resolve(rec: dict, hwnd: int = 0, title_contains: str = "") -> dict:
    wins = _windows_of(rec)
    if not wins:
        raise RuntimeError(messages.no_windows(rec["name"]))
    if hwnd:
        for w in wins:
            if w["hwnd"] == int(hwnd):
                return w
        raise RuntimeError(messages.hwnd_wrong_desktop(hwnd, rec["name"]))
    if title_contains:
        matches = [w for w in wins if title_contains.lower() in w["title"].lower()]
        if not matches:
            known = [w["title"] for w in wins]
            raise RuntimeError(messages.no_window_match(title_contains, known))
        return matches[0]
    return wins[0]


def _windows_of(rec: dict) -> list:
    pids = set(windows.tree_pids(windows.live_roots(rec.get("pids", []))))
    tracked = set(rec.get("hwnds", []))
    out, alive = [], []
    for w in windows.all_windows(include_tool_windows=True):
        if w["hwnd"] in tracked:
            out.append(w)
            alive.append(w["hwnd"])
        elif w["pid"] in pids and w["hwnd"] not in tracked:
            out.append(w)
    if set(alive) != tracked:
        rec["hwnds"] = alive
        state.put_desktop(rec)
    out.sort(key=lambda w: w["area"], reverse=True)
    return out


def _launch_into(rec: dict, exe: str, args: str, wait_seconds: float) -> dict:
    """Event-driven retrieval (v2): suspended start with a job, auto-move only
    with proven ownership (job/tree). Foreign-host candidates go to delta and
    stay on Main until explicit adoption (no window theft). Launch window max
    5 s (+0.5 s rest); the watcher picks up late windows "any time"
    (watch_register). wait_seconds > 5 is explicitly reported as
    wait_capped."""
    res = retrieval.launch_and_retrieve(rec["desktop_id"], exe, args,
                                        wait_seconds, cwd=str(Path.home()),
                                        desktop_name=rec.get("name", ""))
    pid = int(res.get("pid", 0))
    # PID-Identitaet: als {pid,ctime} speichern, damit nach Neustart eine
    # recycelte PID nicht als "eigen" gilt (fail-closed, s. live_roots).
    rec.setdefault("pids", []).append({"pid": pid, "ctime": windows.pid_ctime(pid)})
    for h in res.get("moved_hwnds", []):
        if int(h) not in rec.get("hwnds", []):
            rec.setdefault("hwnds", []).append(int(h))
    state.put_desktop(rec)
    result: dict = {"pid": pid, "moved_windows": res.get("moved_windows", []),
                    "moved_hwnds": [int(h) for h in res.get("moved_hwnds", [])],
                    "handoff": bool(res.get("handoff")),
                    "foreign": bool(res.get("foreign", res.get("handoff"))),
                    "used_job": bool(res.get("used_job", False)),
                    "launch_window_s": res.get("launch_window_s", 5.0),
                    "wait_capped": bool(res.get("wait_capped", False))}
    if res.get("wait_capped"):
        result["wait_note"] = messages.LAUNCH_WINDOW_NOTE
    if res.get("latencies_ms"):
        result["latencies_ms"] = res["latencies_ms"]
    if res.get("handling_ms"):
        result["handling_ms"] = res["handling_ms"]
    if res.get("hide_balance"):
        result["hide_balance"] = res["hide_balance"]
    if res.get("delta"):
        # Delta entries without pixels: hwnd/PID/title/time + reason.
        result["delta"] = [{"hwnd": e.get("hwnd"), "pid": e.get("pid"),
                            "title": e.get("title"), "reason": e.get("reason")}
                           for e in res["delta"]]
    moved_pids: list = []
    try:
        for w in windows.all_windows(include_tool_windows=True):
            if w["hwnd"] in res.get("moved_hwnds", []):
                moved_pids.append(w["pid"])
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _launch_into() (was silent pass)", exc_info=True)
    if result["handoff"] and moved_pids:
        result["foreign_owner_pid"] = moved_pids[0]
    if result["handoff"]:
        result["hint"] = messages.HANDOFF_HINT
        return result
    note = ""
    if not result["moved_windows"]:
        note = messages.NO_PROVEN_OWNERSHIP_NOTE
    result["note"] = note
    return result


@mcp.tool()
def desktop_create(name: str, app: str = "", args: str = "", wait_seconds: float = 20) -> list:
    """Create a new background desktop (Win+Tab level via pyvda).

    Args:
        name: Short ID (letters, digits, -, _). 'graveyard' is reserved.
        app: Optional program to start immediately (generic: any exe).
        args: Command-line arguments for it.
        wait_seconds: Launch window (max 5 s + 0.5 s rest; larger values
            are reported as wait_capped, late windows are picked up by the
            watcher at any time).
    """
    if not NAME_RE.match(name):
        return _err(messages.NAME_PATTERN_ERROR)
    if name == GRAVEYARD:
        return _err(messages.GRAVEYARD_RESERVED)
    if state.get_desktop(name):
        return _err(messages.desktop_exists(name))
    info = vdesk.create_desktop(name)
    rec = {"name": name, "desktop_id": info["id"], "desktop_number": info["number"],
           "pids": [], "hwnds": [], "created_at": datetime.now().isoformat()}
    state.put_desktop(rec)
    result = {"desktop": name, "number": info["number"], "id": info["id"]}
    if app:
        result["launch"] = _launch_into(rec, app, args, wait_seconds)
        state.put_desktop(rec)
    return _txt(result)


@mcp.tool()
def desktop_list() -> list:
    """List all virtual desktops (number, name, owner, currently visible).

    Reconcile flags: 'orphan' = alive but no rec (foreign/manual - only touch
    after looking, never blind). 'stale' = rec points at a dead desktop
    (cleanup candidate, not a live target).
    """
    desks = vdesk.list_desktops()
    recs = state.load_state()["desktops"]
    owners = {rec["desktop_id"]: name for name, rec in recs.items()}
    systems = {rec["desktop_id"] for rec in recs.values() if rec.get("system")}
    current = vdesk.current_id()
    live_ids = {d["id"] for d in desks}
    main_id = min(desks, key=lambda d: d["number"])["id"] if desks else None
    for d in desks:
        d["owned_by"] = owners.get(d["id"])
        d["is_current"] = d["id"] == current
        if d["id"] in systems:
            d["system"] = True
        # Reconcile: alive without a rec (and not Main) is orphaned - touch
        # only after looking, never blind.
        if d["owned_by"] is None and d["id"] != main_id:
            d["orphan"] = True
    # Stale recs (pointing at dead desktops) would otherwise be invisible.
    for name, rec in recs.items():
        if rec["desktop_id"] not in live_ids:
            entry = {"number": None, "id": rec["desktop_id"], "name": "",
                     "owned_by": name, "is_current": False, "stale": True}
            if rec.get("system"):
                entry["system"] = True
            desks.append(entry)
    return _txt(desks)


def _ensure_graveyard() -> dict:
    """Get or lazily create the graveyard rec. Validates the desktop ID
    (manually deleted -> recreate). Idempotent."""
    rec = state.get_desktop(GRAVEYARD)
    if rec:
        try:
            alive = {d["id"] for d in vdesk.list_desktops()}
            if rec.get("desktop_id") in alive:
                return rec
        except Exception:  # noqa: BLE001
            log.debug("swallowed in _ensure_graveyard() (was silent pass)", exc_info=True)
    info = vdesk.create_desktop(GRAVEYARD)
    rec = {"name": GRAVEYARD, "desktop_id": info["id"],
           "desktop_number": info["number"], "pids": [], "hwnds": [],
           "system": True, "created_at": datetime.now().isoformat()}
    state.put_desktop(rec)
    return rec


def _park_on_graveyard(hwnds: list, drop_approved: set | None = None) -> dict:
    """Park surviving windows explicitly on the graveyard desktop.

    Prevents implicit Windows migration to the neighbor desktop on
    deletion. Rotation: more than GRAVEYARD_MAX_WINDOWS -> close oldest via
    WM_CLOSE (best effort; stubborn dialogs stay and can be resolved
    via the graveyard rec with the vision loop).
    Honesty: rotation closes drop-approved windows first; victims
    without a verdict are explicitly reported as rotation_closed_undecided
    (no silent death in a green log).
    """
    alive = [int(h) for h in hwnds if windows.is_window(h)]
    if not alive:
        return {"parked": [], "closed_by_rotation": [],
                "rotation_closed_undecided": [], "graveyard_size": 0}
    approved = drop_approved or set()
    grave = _ensure_graveyard()
    parked = []
    for h in alive:
        try:
            vdesk.move_window(h, grave["desktop_id"])
            parked.append(h)
        except Exception:  # noqa: BLE001
            continue
    grave = _require_desktop(GRAVEYARD)
    known = [h for h in grave.get("hwnds", []) if windows.is_window(h)]
    for h in parked:
        if h not in known:
            known.append(h)
    closed = []
    closed_undecided = []
    while len(known) > GRAVEYARD_MAX_WINDOWS:
        # Drop-approved victims first, then oldest (but then flagged).
        idx = next((i for i, h in enumerate(known) if h in approved), 0)
        oldest = known.pop(idx)
        windows.close_window(oldest)
        closed.append(oldest)
        if oldest not in approved:
            closed_undecided.append(oldest)
    grave["hwnds"] = known
    state.put_desktop(grave)
    return {"parked": parked, "closed_by_rotation": closed,
            "rotation_closed_undecided": closed_undecided,
            "graveyard_size": len(known)}


@mcp.tool()
def desktop_remove(desktop: str, kill_apps: bool = True, decisions: dict | None = None) -> list:
    """Remove a background desktop, optionally killing apps. Main stays.

    No desktop_remove without an agent verdict. Look at it first via vision
    (window_list -> see: dirty? dialog? needed later?) and decide per
    window: save (hop_), park (graveyard), or approve drop. Without a
    verdict windows are only parked, never deleted.

    Args:
        desktop: Worker desktop to remove ('graveyard' is off limits).
        kill_apps: Kill own processes (only effective with drop approval for
            all live windows; foreign PIDs are never killed).
        decisions: Verdict per window as {hwnd: verdict} with verdict in
            {'drop', 'park', 'save'}. 'drop' = WM_CLOSE (+kill on full
            approval) allowed. 'park' = park on graveyard, decided.
            'save' = park + stays pending (the agent still owes saving it
            via the hop_ loop). Windows without a verdict are parked and
            reported as pending - never closed silently.

    Surviving windows are parked EXPLICITLY on the permanent
    graveyard desktop - never implicitly migrated to Main.
    """
    try:
        if desktop == GRAVEYARD:
            return _err(messages.GRAVEYARD_PERMANENT)
        rec = _require_desktop(desktop)
        verdicts: dict = {}
        if decisions:
            if not isinstance(decisions, dict):
                return _err(messages.DECISIONS_SHAPE_ERROR)
            for k, v in decisions.items():
                try:
                    h = int(k)
                except (TypeError, ValueError):
                    return _err(messages.decisions_key_error(k))
                vv = str(v).strip().lower()
                if vv not in ("drop", "park", "save"):
                    return _err(messages.decisions_value_error(k, v))
                verdicts[h] = vv
        alive_now = [h for h in rec.get("hwnds", []) if windows.is_window(h)]
        drop_hwnds = {h for h in alive_now if verdicts.get(h) == "drop"}
        # Close only with drop approval (hits only these hwnds - important
        # for single-instance apps whose process may belong to the user
        # and must never be killed).
        for hwnd in sorted(drop_hwnds):
            try:
                windows.close_window(hwnd)
            except Exception:  # noqa: BLE001
                log.debug("swallowed in desktop_remove() (was silent pass)", exc_info=True)
        time.sleep(1.5)
        killed = []
        if kill_apps and alive_now and drop_hwnds >= set(alive_now):
            # Kill only on full approval for all live windows. Foreign PIDs
            # (handoff) are never in rec['pids'] and are therefore never hit.
            # PID-Identitaet: nach einem Neustart kann eine gespeicherte PID
            # einem fremden Programm gehoeren (Windows recycelt PIDs).
            # live_roots() verifiziert die create_time - fremde/geleakte
            # PIDs werden NICHT getoetet (fail-closed).
            for pid in windows.live_roots(rec.get("pids", [])):
                if windows.kill_tree(pid):
                    killed.append(pid)
            time.sleep(1.5)
        survivors = [h for h in rec.get("hwnds", []) if windows.is_window(h)]
        # Honesty over green logs: what closed cleanly, what died without a
        # verdict (e.g. crashed on its own)? Report both explicitly.
        closed_cleanly = [h for h in sorted(drop_hwnds) if not windows.is_window(h)]
        died_without_verdict = [h for h in alive_now
                                if h not in drop_hwnds and not windows.is_window(h)]
        # Park explicitly BEFORE deleting the desktop - otherwise Windows
        # implicitly migrates survivors to the neighbor desktop.
        park = _park_on_graveyard(survivors, drop_approved=drop_hwnds)
        vdesk.remove_desktop(rec["desktop_id"])
        state.del_desktop(desktop)
        try:
            retrieval.watch_unregister(rec["desktop_id"])
        except Exception:  # noqa: BLE001
            log.debug("swallowed in desktop_remove() (was silent pass)", exc_info=True)
        vdesk.show_main()  # Rule: always show Main at the end.
        titles = {}
        try:
            for w in windows.all_windows():
                if w["hwnd"] in survivors:
                    titles[w["hwnd"]] = w["title"]
        except Exception:  # noqa: BLE001
            log.debug("swallowed in desktop_remove() (was silent pass)", exc_info=True)
        # pending = whatever still lives without a park verdict (drop-approved
        # windows that survived via a dialog, plus everything without a
        # verdict). Dead windows without a verdict must never pass as "cleanly
        # closed" - see rotation_closed_undecided below.
        still_alive = [h for h in survivors if windows.is_window(h)]
        pending = [{"hwnd": h, "title": titles.get(h, "")}
                   for h in still_alive if verdicts.get(h) != "park"]
        result: dict = {"desktop": desktop, "killed": killed, "removed": True,
                        "visible": "main", "parked_on_graveyard": park["parked"],
                        "pending": pending}
        if closed_cleanly:
            result["closed_cleanly"] = closed_cleanly
        if died_without_verdict:
            result["died_without_verdict"] = died_without_verdict
        if park["closed_by_rotation"]:
            result["closed_by_rotation"] = park["closed_by_rotation"]
        if park["rotation_closed_undecided"]:
            result["rotation_closed_undecided"] = park["rotation_closed_undecided"]
        leftovers = [h for h in survivors if h not in park["parked"]]
        if leftovers:
            # Neither closable nor movable: report honestly instead of
            # silently showing up on Main.
            result["needs_agent_vision"] = {
                "hint": messages.NEEDS_AGENT_VISION_UNMOVABLE_HINT,
                "windows": [{"hwnd": h, "title": titles.get(h, "")} for h in leftovers],
            }
        elif pending or park["rotation_closed_undecided"]:
            result["needs_agent_vision"] = {
                "hint": messages.NEEDS_AGENT_VISION_PENDING_HINT,
                "windows": pending,
            }
        elif park["parked"]:
            result["graveyard_hint"] = messages.GRAVEYARD_HINT
        return _txt(result)
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def app_launch(desktop: str, exe: str, args: str = "", wait_seconds: float = 20) -> list:
    """Start any program on a background desktop.

    Generic: no app assumptions. Windows with proven ownership (job/tree)
    are automatically brought to the target desktop (launch window max 5 s,
    wait_capped for larger values; the watcher picks up late windows at any
    time). Single-instance apps (browsers, Electron) forward to a running
    process - reported as handoff:true + delta; real isolation then only
    comes from a separate data/profile directory per desktop (if the app
    supports it).
    """
    try:
        rec = _require_desktop(desktop)
        return _txt(_launch_into(rec, exe, args, wait_seconds))
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def window_list(desktop: str) -> list:
    """Live top-level windows of a desktop (hwnd, pid, title, class)."""
    try:
        rec = _require_desktop(desktop)
        wins = [{"hwnd": w["hwnd"], "pid": w["pid"], "title": w["title"], "class": w["class"], "rect": w.get("rect")}
                for w in _windows_of(rec)]
        return _txt({"desktop": desktop, "windows": wins})
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def see(desktop: str, hwnd: int = 0, title_contains: str = "",
        max_width: int = 1280, embed_image: bool = True) -> list:
    """Screenshot of a background window (100% vision).

    Callable in parallel for any number of desktops. The returned screenshot
    is the only basis for decisions: read click coordinates from exactly
    this image and pass them to hop_click/hop_type.
    """
    try:
        rec = _require_desktop(desktop)
        target = _resolve(rec, hwnd, title_contains or "")
        blocks = _shot_blocks(desktop, target, max_width, embed_image)
        _verify_close(desktop)
        return blocks
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def hop_click(desktop: str, x: int, y: int, hwnd: int = 0, title_contains: str = "",
              button: str = "left", double: bool = False,
              see_after: bool = True, max_width: int = 1280) -> list:
    """Hop: jump to the target desktop, click for real, back to main.

    Args:
        desktop: Target desktop.
        x: X pixel from the last `see` screenshot of the same window.
        y: Y pixel from the last `see` screenshot.
        hwnd: Target window (0 = largest), or use title_contains.
        button: 'left' or 'right'. double: double-click.
        see_after: Return the result screenshot IN THIS CALL (default true,
            saves one LLM roundtrip). false = text only.
        max_width: Width of that embedded JPEG.
    """
    try:
        rec = _require_desktop(desktop)
        target = _resolve(rec, hwnd, title_contains or "")
        if button not in ("left", "right"):
            return _err(messages.BUTTON_ERROR)
        result = hop.do_hop(rec["desktop_id"], target["hwnd"], "click",
                            {"x": int(x), "y": int(y), "button": button, "double": bool(double)})
        return _hop_out(desktop, target, result, bool(see_after), int(max_width))
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def hop_type(desktop: str, text: str, hwnd: int = 0, title_contains: str = "",
             see_after: bool = True, max_width: int = 1280) -> list:
    """Hop: jump to the target desktop, type real text, back to main.

    Args:
        desktop: Target desktop. text: Unicode text (\\n = Enter, \\t = Tab).
        see_after: Result screenshot in this call (default true).
        max_width: Width of that embedded JPEG.
    """
    try:
        rec = _require_desktop(desktop)
        target = _resolve(rec, hwnd, title_contains or "")
        result = hop.do_hop(rec["desktop_id"], target["hwnd"], "type", {"text": text})
        return _hop_out(desktop, target, result, bool(see_after), int(max_width))
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def hop_key(desktop: str, keys: str, hwnd: int = 0, title_contains: str = "",
            see_after: bool = True, max_width: int = 1280) -> list:
    """Hop: send a key combo on the target desktop, back to main.

    Args:
        keys: e.g. 'enter', 'ctrl+s', 'alt+f4', 'ctrl+shift+t'.
        see_after: Result screenshot in this call (default true).
        max_width: Width of that embedded JPEG.
    """
    try:
        rec = _require_desktop(desktop)
        target = _resolve(rec, hwnd, title_contains or "")
        result = hop.do_hop(rec["desktop_id"], target["hwnd"], "key", {"keys": keys})
        return _hop_out(desktop, target, result, bool(see_after), int(max_width))
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def hop_scroll(desktop: str, lines: float = -3, hwnd: int = 0, title_contains: str = "",
               see_after: bool = True, max_width: int = 1280) -> list:
    """Hop: scroll on the target desktop, back to main (negative = down).

    see_after: Result screenshot in this call (default true).
    max_width: Width of that embedded JPEG.
    """
    try:
        rec = _require_desktop(desktop)
        target = _resolve(rec, hwnd, title_contains or "")
        result = hop.do_hop(rec["desktop_id"], target["hwnd"], "scroll", {"lines": float(lines)})
        return _hop_out(desktop, target, result, bool(see_after), int(max_width))
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def show_main() -> list:
    """Switch the visible desktop back to main (desktop #1)."""
    try:
        vdesk.show_main()
        return _txt(messages.SHOW_MAIN_VISIBLE)
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def desktop_see(desktop: str, max_width: int = 1280, embed_image: bool = True) -> list:
    """Fullscreen screenshot of a background desktop (desktop stage, 100% vision).

    For system UI without app windows: Start menu, Run dialog, tray, desktop
    background. The image is the only basis for decisions; pass coordinates
    from it 1:1 to desktop_click/desktop_type/desktop_key (scaling via
    size_px/image_px if needed, see hint).
    Protection: the Main/host desktop is hard-refused (assertion, no switch).
    Limit: UAC and other Secure-Desktop dialogs are NOT reachable from
    virtual desktops by OS design - that is not a bug.
    """
    try:
        rec = _require_desktop(desktop)
        with hop.desktop_session(rec["desktop_id"]):
            data, w, h, ox, oy = capture.grab_screen()
        # Offline after session exit (after show_main + cursor restore):
        # JPEG/PNG encode + file write no longer hold the hop lock.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        # PNG nur auf Wunsch (VISION_DESKTOPS_SAVE_CAPTURES), s. state.py.
        path = state.save_capture(f"{desktop}-desk-{stamp}.png",
                                  capture.to_png(data, w, h))
        meta = {"desktop": desktop,
                "size_px": [w, h], "origin_px": [ox, oy],
                "image_px": list(capture.fit_width(w, h, max(320, max_width))),
                "hint": messages.DESKTOP_SEE_HINT}
        if path:
            meta["saved_to"] = str(path)
        blocks: list = [TextContent(type="text", text=json.dumps(meta, ensure_ascii=False))]
        if embed_image:
            jpeg = capture.to_jpeg(data, w, h, max_width=max(320, max_width))
            blocks.append(ImageContent(type="image", data=base64.b64encode(jpeg).decode("ascii"),
                                       mimeType="image/jpeg"))
        _verify_close(desktop)
        return blocks
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def desktop_click(desktop: str, x: int, y: int,
                  button: str = "left", double: bool = False) -> list:
    """Desktop stage: jump to a background desktop, click for real, back.

    Args:
        desktop: Target desktop (NEVER Main - refused).
        x: X pixel from the last `desktop_see` image.
        y: Y pixel from the last `desktop_see` image.
        button: 'left' or 'right'. double: double-click.
    Same real-mode path as hop_click, just without a window reference -
    for system UI (Start/Run/tray). Guard: verified desktop arrival.
    """
    try:
        rec = _require_desktop(desktop)
        if button not in ("left", "right"):
            return _err(messages.BUTTON_ERROR)
        result = hop.do_hop_desktop(rec["desktop_id"], "click",
                                    {"x": int(x), "y": int(y),
                                     "button": button, "double": bool(double)})
        _verify_open(desktop)
        return _txt(result)
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def desktop_type(desktop: str, text: str) -> list:
    """Desktop stage: jump to a background desktop, type text, back.

    Args:
        desktop: Target desktop (NEVER Main - refused).
        text: Unicode text (\\n = Enter, \\t = Tab). Long text automatically
            goes via clipboard+paste (like hop_type).
    """
    try:
        rec = _require_desktop(desktop)
        result = hop.do_hop_desktop(rec["desktop_id"], "type", {"text": text})
        _verify_open(desktop)
        return _txt(result)
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def desktop_key(desktop: str, keys: str) -> list:
    """Desktop stage: send a key combo on a background desktop, back.

    Args:
        desktop: Target desktop (NEVER Main - refused).
        keys: e.g. 'enter', 'escape', 'win', 'win+r', 'alt+f4'.
    """
    try:
        rec = _require_desktop(desktop)
        result = hop.do_hop_desktop(rec["desktop_id"], "key", {"keys": keys})
        _verify_open(desktop)
        return _txt(result)
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def desktop_scroll(desktop: str, lines: float = -3) -> list:
    """Desktop stage: scroll on a background desktop, back to main.

    Args:
        desktop: Target desktop (NEVER Main - refused).
        lines: Scroll lines (negative = down, positive = up).
    Same real-mode path as hop_scroll, just without a window reference -
    for system UI (Start list and others). Guard: verified desktop arrival.
    """
    try:
        rec = _require_desktop(desktop)
        result = hop.do_hop_desktop(rec["desktop_id"], "scroll", {"lines": float(lines)})
        _verify_open(desktop)
        return _txt(result)
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def delta_list() -> list:
    """Main delta without pixels (v2): per entry only hwnd, PID, title, time + reason.

    No permanent screenshot of Main. Classes: box (moved), delta (on Main:
    foreign-host, pin, quota, queue-overflow), user-touched. For more than a
    title: targeted single view via delta_view (only on demand, without
    retention). Adoption only explicitly via window_adopt (vision verdict)."""
    try:
        return _txt({"delta": state.list_delta()})
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def delta_view(hwnd: int, max_width: int = 1280) -> list:
    """Targeted single view of exactly one delta hwnd (client capture, only on
    demand, without retention, without a file). Basis for the explicit
    adoption via window_adopt."""
    try:
        h = int(hwnd)
        if not windows.is_window(h):
            return _err(messages.hwnd_gone(h))
        data, w, hgt = capture.grab_client(h)
        meta = {"hwnd": h, "size_px": [w, hgt],
                "image_px": list(capture.fit_width(w, hgt, max(320, max_width))),
                "hint": messages.DELTA_VIEW_HINT}
        blocks: list = [TextContent(type="text", text=json.dumps(meta, ensure_ascii=False))]
        jpeg = capture.to_jpeg(data, w, hgt, max_width=max(320, max_width))
        blocks.append(ImageContent(type="image", data=base64.b64encode(jpeg).decode("ascii"),
                                   mimeType="image/jpeg"))
        return blocks
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def window_adopt(desktop: str, hwnd: int) -> list:
    """Take a window into the box (explicit agent step, no automatism).

    Handoff windows sit on Main in the delta until the agent takes them over
    after delta_view via a vision verdict. Provenance is deliberately skipped
    here (agent verdict replaces job/tree). Pin and interaction guards still
    apply: pinned windows or windows touched by the user are never moved
    (zero false grabs). The move itself is the tested v2 chain: the window
    is cloaked (invisible on Main), moved, uncloaked and committed with an
    immediate re-move, verified by desktop check plus a vision leak check
    (region shots on Main - no visible flicker, no silent move failure)."""
    try:
        rec = _require_desktop(desktop)
        h = int(hwnd)
        if not windows.is_window(h):
            return _err(messages.hwnd_gone(h))
        # No separate pin check here: move_if_unpinned reports "pin" in the
        # same COM round-trip as the move (one round-trip, not two).
        # Adoption: the window has existed for a long time - only current use
        # counts as a usage window (foreground + input after appearance).
        # Appearance ~= 60 s ago minimizes false grabs without blocking
        # legitimate adoption (window open, measure!).
        try:
            appear = int(windows._tick_now()) - 60_000
        except Exception:  # noqa: BLE001
            appear = 0
        if retrieval.user_touched(h, appear & 0xFFFFFFFF):
            try:
                state.add_delta({"hwnd": h, "pid": retrieval.wintypes_pid(h),
                                 "title": retrieval._title_of(h),
                                 "reason": "user-touched",
                                 "detail": messages.USER_TOUCHED_DETAIL})
            except Exception:  # noqa: BLE001
                log.debug("swallowed in window_adopt() (was silent pass)", exc_info=True)
            return _err(messages.adopt_user_touched(h))
        fg_before = windows.foreground_hwnd()
        outcome = vdesk.move_if_unpinned(h, rec["desktop_id"], priority=0,
                                         pixel=True)
        if outcome == "pin":
            try:
                state.add_delta({"hwnd": h, "pid": retrieval.wintypes_pid(h),
                                 "title": retrieval._title_of(h), "reason": "pin"})
            except Exception:  # noqa: BLE001
                log.debug("swallowed in window_adopt() (was silent pass)", exc_info=True)
            return _err(messages.adopt_pinned(h))
        if outcome != "moved":
            try:
                windows.show_window(h)
                outcome = vdesk.move_if_unpinned(h, rec["desktop_id"], priority=0,
                                         pixel=True)
            except Exception:  # noqa: BLE001
                log.debug("swallowed in window_adopt() (was silent pass)", exc_info=True)
            if outcome != "moved":
                return _err(messages.adopt_failed(outcome))
        try:
            # Explicit adoption: the window must end visible on target
            # (it may arrive hidden from the ownership hide).
            windows.show_window(h)
        except Exception:  # noqa: BLE001
            log.debug("swallowed in window_adopt() (was silent pass)", exc_info=True)
        try:
            retrieval._resolve_hidden(h, show=False)
            retrieval.drop_watch_pending(rec["desktop_id"], h)
        except Exception:  # noqa: BLE001
            log.debug("swallowed in window_adopt() (was silent pass)", exc_info=True)
        try:
            for owned in windows.owned_hwnds(h):
                try:
                    if vdesk.move_if_unpinned(owned, rec["desktop_id"],
                                              priority=0, pixel=True) != "moved":
                        continue
                    try:
                        windows.show_window(owned)
                    except Exception:  # noqa: BLE001
                        log.debug("swallowed in window_adopt() (was silent pass)", exc_info=True)
                    try:
                        retrieval._resolve_hidden(owned, show=False)
                    except Exception:  # noqa: BLE001
                        log.debug("swallowed in window_adopt() (was silent pass)", exc_info=True)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            log.debug("swallowed in window_adopt() (was silent pass)", exc_info=True)
        try:
            if fg_before and windows.foreground_hwnd() != fg_before \
                    and windows.foreground_hwnd() == h:
                import ctypes as _ct

                _ct.windll.user32.SetForegroundWindow(_ct.c_void_p(fg_before))
        except Exception:  # noqa: BLE001
            log.debug("swallowed in window_adopt() (was silent pass)", exc_info=True)
        if h not in rec.get("hwnds", []):
            rec.setdefault("hwnds", []).append(h)
            state.put_desktop(rec)
        try:
            state.drop_delta(h)
        except Exception:  # noqa: BLE001
            log.debug("swallowed in window_adopt() (was silent pass)", exc_info=True)
        return _txt({"adopted": h, "desktop": desktop})
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


@mcp.tool()
def diagnose(limit: int = 20) -> list:
    """Show what the server swallowed: health facts + the ring buffer of the
    last errors (time, level, logger, message, traceback - newest first).

    Use this whenever a tool result looks like a silent failure: former
    `except: pass` spots now log with exc_info=True and land here. Also run
    `python -m vision_desktops.diagnose --demo` in the server's env to prove
    a ctypes.ArgumentError is captured."""
    try:
        return _txt({"diagnose": diagnose_module.report(int(limit))})
    except Exception as exc:  # noqa: BLE001
        return _tool_err(exc)


def main():
    # Ringpuffer installieren, BEVOR der Serverarbeit beginnt: alles, was
    # Module unten schlucken (log.debug + exc_info), landet fuer das
    # `diagnose`-Tool im Speicher. Nie stdout (MCP-Transport), optional
    # stderr via VISION_DESKTOPS_LOG=stderr.
    errors.install()
    mcp.run()
