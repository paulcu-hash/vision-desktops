"""Central agent-facing prose for vision-desktops.

All user/agent-visible strings for server.py and retrieval.py live here so
wording stays consistent and testable. Reason-code literals (pin, quota,
user-touched, queue-overflow, foreign-host, move-failed, box, delta,
handoff, foreign, ...) and state.json schema keys are NOT translated and
are therefore not part of this module.
"""

# --- Canonical server error strings (exact wording, tests match on these) ---

NAME_PATTERN_ERROR = "name must match [A-Za-z0-9_-]{1,40}"

GRAVEYARD_RESERVED = "'graveyard' is reserved (parking desktop, see desktop_remove)."

GRAVEYARD_PERMANENT = "'graveyard' is permanent and cannot be deleted."

DECISIONS_SHAPE_ERROR = "decisions must be {hwnd: 'drop'|'park'|'save'}"

MAIN_REFUSED = (
    "Refused: target is the Main/host desktop. Desktop hops run only on "
    "worker desktops (#2-N) - the visible screen is never touched."
)

# --- Programmstart-Allowlist (Prompt-Injection-Schutz fuer
# app_launch/desktop_create) -----------------------------------------------
#
# Tool-Argumente stammen aus der Modell-Ausgabe - und deren Inhalt kann von
# eingeschleustem Text (Webseite, Datei, Screenshot-Text) beeinflusst sein.
# Ohne diese Liste wuerde ein eingeschleusterter Satz beliebiges Programm
# starten. Nur hier genannte Programme duerfen ueber die Launch-Tools starten.
#
# Die Liste ist bewusst "Basis-Programme, keine Shell/kein Skript-Host":
# cmd, powershell, wscript, mshta, rundll32, regsvr32, certutil & Co.
# fehlen absichtlich. python/pythonw bleiben enthalten, damit eigene
# Testfenster und Skripte startbar bleiben - ein Interpreter bleibt damit
# moeglich; die harte Kante ist der Agent-seitige Approval (SECURITY_TODO 1),
# nicht diese Liste.
#
# Operator-Erweiterung NUR ueber Environment (Modell kann das nicht setzen):
#   VISION_DESKTOPS_ALLOWED_APPS="meinexe.exe,anderes.exe"
ALLOWED_APPS = (
    "calc.exe",
    "charmap.exe",
    "chrome.exe",
    "code.exe",
    "explorer.exe",
    "firefox.exe",
    "msedge.exe",
    "mspaint.exe",
    "notepad.exe",
    "python.exe",
    "pythonw.exe",
)

ALLOWED_APPS_ENV = "VISION_DESKTOPS_ALLOWED_APPS"

# Aus diesen Ordnern startet NIE etwas (SECURITY_TODO 2): dort kann jeder
# eine kopierte exe ablegen - auch unter erlaubtem Namen. Pfadsegment-
# Vergleich, nicht Namensvergleich.
DENIED_LAUNCH_DIRS = ("downloads", "temp")

APP_EMPTY_ERROR = "app/exe is empty - nothing to launch."


def app_not_allowed(exe: str, allowed: list) -> str:
    """Clear refusal for a program outside the launch allowlist."""
    return (
        f"Program '{exe}' is not on the launch allowlist - refused "
        "(prompt-injection guard for app_launch/desktop_create: tool "
        "arguments are model output, not operator intent). "
        f"Allowed: {', '.join(allowed)}. "
        f"Operators extend the list via {ALLOWED_APPS_ENV} "
        "(comma-separated, environment only - never via tool arguments)."
    )


def app_from_denied_dir(exe: str, folder: str) -> str:
    """Refusal for executables living in Downloads/Temp."""
    return (
        f"Program '{exe}' points into a '{folder}' folder - refused. "
        "Executables from Downloads/Temp are never started (anyone can "
        "drop a copy there, even under an allowlisted name); move it "
        "elsewhere and add its name to the allowlist."
    )


def desktop_exists(name: str) -> str:
    """Error when a desktop name is already taken."""
    return f"Desktop '{name}' already exists - remove it first"


def unknown_desktop(name: str) -> str:
    """Error when a desktop name has no record."""
    return f"Unknown desktop '{name}'. See desktop_list."


def decisions_key_error(key) -> str:
    """Error when a decisions key is not a hwnd."""
    return f"decisions key '{key}' is not a hwnd"


def decisions_value_error(key, value) -> str:
    """Error when a decisions value is not drop|park|save."""
    return f"decisions[{key}]='{value}' invalid - only drop|park|save"


# --- Delta detail strings ---

USER_TOUCHED_DETAIL = "touched by the user"

# --- Window resolution errors ---


def no_windows(desktop_name: str) -> str:
    """Desktop record has no windows yet."""
    return f"Desktop '{desktop_name}' has no windows. Use app_launch first."


def hwnd_wrong_desktop(hwnd: int, desktop_name: str) -> str:
    """hwnd does not belong to the given desktop."""
    return f"hwnd {hwnd} does not belong to desktop '{desktop_name}'."


def no_window_match(title_contains: str, known: list) -> str:
    """No title substring match among known windows."""
    return f"No window with '{title_contains}'. Windows: {known}"


# --- Generic tool errors ---

BUTTON_ERROR = "button must be 'left' or 'right'"


def hwnd_gone(hwnd: int) -> str:
    """Delta hwnd no longer exists."""
    return f"hwnd {hwnd} no longer exists"


def adopt_pinned(hwnd: int) -> str:
    """Adoption refused because the window/app is pinned."""
    return f"hwnd {hwnd} is pinned - never move (acceptance v2)"


def adopt_user_touched(hwnd: int) -> str:
    """Adoption refused because the user touched the window."""
    return f"hwnd {hwnd} was touched by the user - no move"


def adopt_failed(outcome: str) -> str:
    """Adoption move failed."""
    return f"adopt-move failed: {outcome}"


SHOW_MAIN_VISIBLE = "Visible: Main desktop."

# --- Launch / handoff prose ---

LAUNCH_WINDOW_NOTE = (
    "Launch window capped at 5 s (spec); the watcher picks up late windows at any time."
)

HANDOFF_HINT = (
    "Single-instance forwarding or foreign host: no proven ownership "
    "(neither job nor tree). Windows are in the delta on Main and are only "
    "taken over via explicit adoption (window_adopt after delta sighting) - "
    "no automatism. For real isolation, start the app - if supported - "
    "with a separate data/profile directory per desktop."
)

NO_PROVEN_OWNERSHIP_NOTE = (
    "No window with proven ownership appeared. Foreign candidates may be "
    "in the delta (delta_list/delta_view); adoption only explicitly via window_adopt."
)

# --- Screenshot hints ---

SEE_HINT = (
    "Use click coordinates from this screenshot directly. "
    "If the embedded image is smaller than size_px (see image_px), "
    "scale coordinates per axis by size_px/image_px."
)

SEE_UNCHANGED_HINT = (
    "Window is pixel-identical to your last see of it: that screenshot is "
    "still valid, no new image is embedded (saves tokens). Coordinates from "
    "it still apply unchanged. A later see returns an image as soon as "
    "pixels differ."
)

REGION_ERROR = (
    "region must be 'x,y,w,h' in client pixels of this window "
    "(e.g. '0,0,640,400'), all values integers, width/height > 0."
)

DESKTOP_SEE_HINT = (
    "Pass coordinates from this image directly to desktop_click/type/key. "
    "Screen coordinate = origin_px + image pixel. "
    "If the embedded image is smaller than size_px (see image_px), "
    "scale coordinates per axis by size_px/image_px."
)

DELTA_VIEW_HINT = (
    "Single view without retention. No coordinates needed on adoption "
    "(window_adopt only moves)."
)

# --- help-Tool (Punkt 14: vollstaendiger Protokolltext, aus den
# Server-Instructions herausgeloesst - dort bleibt nur der Kern) ---

HELP_TEXT = """vision-desktops - full protocol

LOOP, window level (any window program):
  window_list -> see -> decide -> hop_click/hop_type/hop_key/hop_scroll
  -> see (verify). Screenshots run in parallel across desktops; inputs
  hop serially (the server switches to the target desktop, performs REAL
  mouse/keyboard input, returns to main, cursor restored). Interleave
  desktops for parallelism. see spares tokens: an unchanged window
  answers text only (previous screenshot still valid); region='x,y,w,h'
  captures a crop, max_width=640 a compact image. hop_* return the result
  screenshot in the SAME call (see_after, default true) - no extra see.

LOOP, desktop level (OS chrome without an app window: Start menu, Run
  dialog, tray): desktop_see -> desktop_click/desktop_type/desktop_key/
  desktop_scroll -> desktop_see. Same vision rule (fullscreen pixels are
  the only basis, scale via size_px/image_px), same real-input path, no
  window handle needed. desktop_* REFUSES the main/host desktop by hard
  assertion - worker desktops only.

VISION RULE (100%): coordinates always come from the latest screenshot
  of the SAME window/level. Generic for ANY program: locate windows only
  via hwnd/title_contains, never assume app layout.

REMOVAL (no silent close): never call desktop_remove blind. First
  window_list -> see (dirty? dialog? needed later?) and judge per window:
  save via hop_*, park on graveyard, or approve drop via
  decisions={hwnd: 'drop'|'park'|'save'}. Without a verdict windows are
  only parked, never deleted; removed:true alone never means content was
  clean.

SINGLE-INSTANCE APPS (browsers, Electron, modern Store apps) hand new
  windows over to an already running process: those windows belong to a
  foreign process and launch reports handoff:true. For real isolation,
  start such apps with a separate data/profile directory per desktop if
  they support it (e.g. browsers take a separate user-data directory per
  desktop, plus a separate remote-debugging port per desktop for CDP).

RETRIEVAL v2 (event-driven): auto-move only with proven ownership (job
  hit or process tree). Foreign-host candidates always go to the delta
  (hwnd/pid/title/time only, no Main screenshot) and stay on Main until
  explicit adoption. Guards: interaction guard (no move when
  user-touched) and collision clause (queue moves during see->hop
  sessions until rest, 2s cap then delta).

ADOPTION is an explicit agent step: delta_list -> delta_view (single
  on-demand view, no retention) -> window_adopt (vision verdict; pin and
  user-touched guards still apply). Pinned windows/apps never move.

LAUNCH ALLOWLIST (prompt-injection guard): app_launch and desktop_create
  only start programs on the server allowlist; a refusal names the
  allowed ones (operators extend via VISION_DESKTOPS_ALLOWED_APPS in the
  environment, never through tool arguments). Executables under
  Downloads/Temp are always refused. desktop_create refuses BEFORE the
  desktop exists - no leftover desktop.

LIMIT: UAC and other Secure-Desktop prompts are unreachable from virtual
  desktops by OS design - not a bug."""

# --- Removal protocol hints ---

NEEDS_AGENT_VISION_UNMOVABLE_HINT = (
    "These windows could neither be closed nor parked. "
    "Inspect them with window_list/see and close them yourself "
    "via hop_click/hop_key."
)

NEEDS_AGENT_VISION_PENDING_HINT = (
    "No desktop_remove without an agent verdict. These windows were "
    "only parked, never deleted - inspect via window_list/see "
    "(dirty? dialog? needed later?) and decide per window: "
    "save (hop_), park (graveyard), or approve drop "
    "(decisions={hwnd: 'drop'|'park'|'save'})."
)

GRAVEYARD_HINT = (
    "Parked windows are on the permanent 'graveyard' desktop "
    "(invisible, no focus). If needed, resolve them via the 'graveyard' rec "
    "with the vision loop (window_list/see/hop_*)."
)


def translate_external_error(text: str) -> str:
    """Pass external error text through unchanged.

    All producing modules (state.py, hop.py) raise English errors directly,
    so no mapping is needed. Unknown texts pass through as-is.
    """
    return text
