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
