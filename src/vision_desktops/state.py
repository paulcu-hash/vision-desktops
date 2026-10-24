import json
import os
import threading
from pathlib import Path

import logging

log = logging.getLogger(__name__)


LOCK = threading.Lock()

STATE_DIR = Path(os.environ.get("VISION_DESKTOPS_STATE", str(Path.home() / ".vision-desktops")))
STATE_FILE = STATE_DIR / "state.json"
CAPTURE_DIR = STATE_DIR / "captures"
CAPTURE_KEEP = 200

CAPTURES_ENV = "VISION_DESKTOPS_SAVE_CAPTURES"
_CAPTURES_OFF = ("", "0", "false", "no", "off", "nein", "aus")


def captures_enabled() -> bool:
    """Screenshots auf Platte - nur auf Wunsch (Default: AUS).

    Jedes see/desktop_see erzeugt sonst ein Voll-PNG: bis zu 200 Dateien,
    unverschluesselt, in ~/.vision-desktops/captures. Das Speichern kostet
    Prozessorzeit (PNG) und bleibt unnoetig, wenn das Bild ohnehin nur als
    JPEG in der Antwort untergeht. VISION_DESKTOPS_SAVE_CAPTURES=1 (oder
    ein beliebiger anderer Wert ausser 0/false/off) aktiviert es."""
    return str(os.environ.get(CAPTURES_ENV, "")).strip().lower() \
        not in _CAPTURES_OFF


def save_capture(name: str, png: bytes):
    """PNG nach captures/ legen - nur wenn aktiviert, sonst None.

    Ein gescheiterter Speichervollzug ist KEIN Tool-Fehler: das Bild ist ja
    bereits in der Antwort, es fehlt nur die Datei."""
    if not captures_enabled():
        return None
    try:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        path = CAPTURE_DIR / name
        path.write_bytes(png)
        prune_captures()
        return path
    except Exception:  # noqa: BLE001
        log.debug("swallowed in save_capture() (was silent pass)", exc_info=True)
        return None


def _load_unlocked() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log.debug("swallowed in load_state() (was silent pass)", exc_info=True)
    return {"desktops": {}}


def _save_unlocked(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def load_state() -> dict:
    with LOCK:
        return _load_unlocked()


def save_state(state: dict) -> None:
    with LOCK:
        _save_unlocked(state)


# --- Offene Hide-Eintraege (Ueberleben eines harten Abbruchs) ---------------
# atexit (retrieval._restore_all_hidden_at_exit) laeuft bei Task-Manager-Kill
# oder Absturz NICHT. Ein mit SW_HIDE verstecktes Fenster waere dann fuer
# immer unsichtbar - Diebstahl durch Unsichtbarkeit. Darum steht jeder
# unabgeschlossene Hide-Eintrag HIER in state.json, und der Server-Start
# (retrieval.restore_hidden_at_start) holt sie zurueck.

def add_hidden(hwnd: int, info: dict) -> None:
    """Einen als versteckt registrierten hwnd festhalten (read-modify-write
    unter demselben LOCK wie alles andere - kein verlorenes Update)."""
    with LOCK:
        st = _load_unlocked()
        st.setdefault("hidden", {})[str(int(hwnd))] = dict(info)
        _save_unlocked(st)


def drop_hidden(hwnd) -> None:
    """Eintrag entfernen (Fenster sichtbar, tot oder unabhaengig aufgeloest)."""
    key = str(int(hwnd))
    with LOCK:
        st = _load_unlocked()
        if key in (st.get("hidden") or {}):
            del st["hidden"][key]
            _save_unlocked(st)


def hidden_entries() -> dict:
    """Kopie der offenen Hide-Eintraege {str(hwnd): info}."""
    with LOCK:
        return dict(_load_unlocked().get("hidden") or {})


def get_desktop(name: str):
    return load_state()["desktops"].get(name)


def put_desktop(rec: dict) -> None:
    st = load_state()
    st["desktops"][rec["name"]] = rec
    save_state(st)


def del_desktop(name: str) -> None:
    st = load_state()
    st["desktops"].pop(name, None)
    save_state(st)


def require_desktop(name: str) -> dict:
    rec = get_desktop(name)
    if rec is None:
        raise KeyError(f"Unknown desktop '{name}'. See desktop_list.")
    return rec


def prune_captures(keep: int = CAPTURE_KEEP) -> int:
    """Delete oldest captures above the limit (retention). Returns the number
    of deleted files. Never fails: errors are swallowed."""
    try:
        files = sorted((p for p in CAPTURE_DIR.iterdir() if p.is_file()),
                       key=lambda p: p.stat().st_mtime)
    except Exception:
        return 0
    doomed = files[:max(0, len(files) - max(1, keep))]
    n = 0
    for p in doomed:
        try:
            p.unlink()
            n += 1
        except Exception:
            log.debug("swallowed in prune_captures() (was silent pass)", exc_info=True)
    return n


DELTA_KEEP = 200  # same retention as captures: titles carry user content.


def list_delta() -> list:
    try:
        return list(load_state().get("delta", []))
    except Exception:  # noqa: BLE001
        return []


def add_delta(entry: dict) -> dict:
    """Main delta without pixels (v2): only hwnd, PID, title, timestamp + reason.
    No permanent screenshot of Main. Titles follow the same
    audit/retention rule as captures (DELTA_KEEP)."""
    from datetime import datetime, timezone

    rec = {"hwnd": int(entry.get("hwnd", 0)), "pid": int(entry.get("pid", 0)),
           "title": str(entry.get("title", ""))[:200],
           "ts": entry.get("ts") or datetime.now(timezone.utc).isoformat(),
           "reason": str(entry.get("reason", "orphan"))[:80]}
    if entry.get("detail"):
        rec["detail"] = str(entry["detail"])[:200]
    st = load_state()
    items = list(st.get("delta", []))
    items = [e for e in items if int(e.get("hwnd", 0)) != rec["hwnd"]]
    items.append(rec)
    st["delta"] = items[-max(1, DELTA_KEEP):]
    save_state(st)
    return rec


def drop_delta(hwnd: int) -> bool:
    st = load_state()
    items = list(st.get("delta", []))
    kept = [e for e in items if int(e.get("hwnd", 0)) != int(hwnd)]
    if len(kept) == len(items):
        return False
    st["delta"] = kept
    save_state(st)
    return True
