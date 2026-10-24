import copy
import contextlib
import json
import os
import threading
import time
from pathlib import Path

import logging

log = logging.getLogger(__name__)


# RLock (nicht Lock): der gesamte Ablauf "load -> aendern -> save" laeuft in
# EINER kritischen Sektion, und Helfer dufen darin verschachtelt werden
# (update_desktop ruft _load_unlocked/_save_unlocked, modify ist wiederver-
# wendbar). Sonst kann der Watcher-Thread (_watch_add_to_rec) den
# Server-Thread (_launch_into) ueberschreiben: beide speichern einen ganzen
# rec-Snapshot, der letzte Schreiber gewinnt - ein verlorener PID-Eintrag
# heisst, dass desktop_remove diesen Prozess NICHT beendet.
LOCK = threading.RLock()

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


# Prozess-Cache fuer state.json: gecachten Inhalt plus Datei-Stamp
# (mtime_ns + size). Stamp stimmt -> kein Platten-Lesen noetig; Stamp
# weicht ab (eigener oder fremder Schreibvorgang) -> neu laden. Nie wird
# ein aelterer Inhalt unter neuem Stamp ausgeliefert.
_CACHE: dict = {"st": None, "stamp": None}


def _file_stamp():
    """(mtime_ns, size) der state.json oder None wenn nicht vorhanden."""
    try:
        st = STATE_FILE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load_unlocked() -> dict:
    """state.json lesen - mit Prozess-Cache (Stamp-Vergleich).

    Reihenfolge ist entscheidend: ERST stat, DANACH lesen. Ein fremder
    Prozess, der zwischen beiden schreibt, erzeugt dann einen Stamp, der
    NIE zum gecachten Inhalt passt -> naechster Aufruf liest neu. Nie kann
    ein aelterer Inhalt unter einem neueren Stamp ausgeliefert werden
    (verlorenes Update auch ueber Prozessgrenzen hinweg)."""
    stamp = _file_stamp()
    cached = _CACHE["st"]
    if cached is not None and stamp == _CACHE["stamp"]:
        return copy.deepcopy(cached)
    if stamp is None:
        # Keine Datei: Standardwert cachen (fremder Prozess kann sie
        # jederzeit anlegen -> dann passt der Stamp nicht mehr).
        parsed = cached if cached is not None else {"desktops": {}}
        _CACHE["st"] = parsed
        _CACHE["stamp"] = None
        return copy.deepcopy(parsed)
    try:
        parsed = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.debug("swallowed in load_state() (was silent pass)", exc_info=True)
        # Kaputte/teilgeschriebene Datei: den letzten GUTEN Stand behalten,
        # statt alles auf Default zurueckzusetzen.
        if cached is not None:
            return copy.deepcopy(cached)
        parsed = {"desktops": {}}
    _CACHE["st"] = parsed
    _CACHE["stamp"] = stamp
    return copy.deepcopy(parsed)


def _save_unlocked(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Temporaedatei PROZESS- und THREAD-eindeutig: zwei Server-Instanzen
    # (bzw. zwei Threads) schreiben sonst in dieselbe state.tmp hinein und
    # zerstoeren sich gegenseitig beim os.replace.
    tmp = STATE_FILE.with_name(
        f"{STATE_FILE.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    _replace_with_retry(tmp, STATE_FILE)
    # Cache NEU AUFBAUEN statt blind setzen: noch einmal von der Platte
    # (stat VOR read) - waere ein fremder Prozess dazwischengekommen, steht
    # SEIN Inhalt im Cache und nichts geht verloren.
    _CACHE["st"] = None
    _CACHE["stamp"] = None
    _load_unlocked()


REPLACE_TRIES = 10      # Windows: transientes "Access denied" (Viren-
REPLACE_DELAY_S = 0.02  # scanner, Indexer, lesender Fremdprozess)


def _replace_with_retry(tmp, dst) -> None:
    """os.replace mit Retry: eine geteilte Datei wirft auf Windows
    momentan Access denied - ohne Retry waere GENAU dieser Speichervorgang
    verloren (der Fehler bricht die modify()-Sektion ab). Nach
    REPLACE_TRIES wird der letzte Fehler ehrlich weitergeworfen - nie still
    als "gespeichert" gebucht."""
    last: Exception | None = None
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError as exc:  # noqa: PERF203 (transient by design)
            last = exc
            time.sleep(REPLACE_DELAY_S * (attempt + 1))
    try:
        tmp.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        log.debug("swallowed in _replace_with_retry() (cleanup) "
                  "(was silent pass)", exc_info=True)
    raise last if last else RuntimeError("os.replace failed")


def load_state() -> dict:
    with LOCK:
        return _load_unlocked()


def save_state(state: dict) -> None:
    with LOCK:
        _save_unlocked(state)


@contextlib.contextmanager
def modify():
    """load -> aendern -> save ATOMAR: die gesamte Aenderung liegt in
    EINER kritischen Sektion (RLock), kein anderer Thread kann dazwischen-
    schreiben. So sieht der Mutierende immer den FRISCHEN Stand - der
    Klassiker "zwei Threads speichern je ihren Snapshot, der letzte
    gewinnt" entfaellt."""
    with LOCK:
        st = _load_unlocked()
        yield st
        _save_unlocked(st)


def update_desktop(name: str, mutate):
    """NUR diesen Desktop atomar aendern: frischen rec laden, `mutate(rec)`
    anwenden, zurueckspeichern - alles unter dem Lock.

    Liefer den geaenderten frischen rec zurueck (fuer den Aufrufer, der
    lokale Snapshot nachfuehren will) oder None wenn der Name unbekannt ist.
    Genau das loest den Konflikt _watch_add_to_rec vs. _launch_into: beide
    haengen ihre Aenderung an denselben FRISCHEN rec, statt je einen
    aeltern Snapshot zu ueberschreiben."""
    with LOCK:
        st = _load_unlocked()
        rec = (st.get("desktops") or {}).get(name)
        if rec is None:
            return None
        mutate(rec)
        _save_unlocked(st)
        return rec


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
    """Ganzen rec-Snapshot speichern - ATOMAR (ein Laden, ein Schreiben,
    nichts dazwischen). Fuer Feldaenderungen an einem BESTEHENDEN rec
    update_desktop() benutzen: das haengt die Aenderung an den frischen
    Stand an, statt einen aeltern Snapshot drueberzulegen."""
    with modify() as st:
        st["desktops"][rec["name"]] = rec


def del_desktop(name: str) -> None:
    with modify() as st:
        st["desktops"].pop(name, None)


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
    with modify() as st:
        items = list(st.get("delta", []))
        items = [e for e in items if int(e.get("hwnd", 0)) != rec["hwnd"]]
        items.append(rec)
        st["delta"] = items[-max(1, DELTA_KEEP):]
    return rec


def drop_delta(hwnd: int) -> bool:
    with modify() as st:
        items = list(st.get("delta", []))
        kept = [e for e in items if int(e.get("hwnd", 0)) != int(hwnd)]
        if len(kept) == len(items):
            return False
        st["delta"] = kept
    return True
