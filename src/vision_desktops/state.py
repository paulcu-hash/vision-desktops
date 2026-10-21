import json
import os
import threading
from pathlib import Path

LOCK = threading.Lock()

STATE_DIR = Path(os.environ.get("VISION_DESKTOPS_STATE", str(Path.home() / ".vision-desktops")))
STATE_FILE = STATE_DIR / "state.json"
CAPTURE_DIR = STATE_DIR / "captures"
CAPTURE_KEEP = 200


def load_state() -> dict:
    with LOCK:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        return {"desktops": {}}


def save_state(state: dict) -> None:
    with LOCK:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE_FILE)


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
            pass
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
