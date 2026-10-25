# vision-desktops

**Parallel vision screens as an MCP server (research prototype, NOT a release yet).**

The main agent (Codex, OpenCode, Claude Code, ...) runs on the visible
main desktop and orchestrates any number of background desktops - 2 or 100.
Screenshots run in parallel, inputs hop serially:

```
remember main -> target desktop -> click/type -> back on main -> next ...
```

## Two rules (absolute, always)

1. **Generalizable:** No code, no tool, no test assumes a specific app,
   website, or task. Every window program runs through the same generic
   loop. Find windows only via `hwnd` / `title_contains`, always derive
   actions from the current screenshot.

2. **100% vision:** No DOM, API, or accessibility shortcuts. The agent
   only sees screenshots (`see`) and acts on them. Coordinates come from
   screenshot pixels; inputs are real mouse/keyboard events (SendInput)
   on the target desktop.

## Technology (do not confuse!)

- **Used:** Modern Windows virtual-desktop system (Win+Tab level) via COM
  (`IVirtualDesktopManagerInternal`), encapsulated in code via `pyvda`.
  DWM renders all desktops -> screenshots even when invisible (PrintWindow).
- **NOT used:** Win32 API `CreateDesktop` (a raw desktop object not rendered
  by DWM). Unsuitable, never used anywhere.

## Tools (model-agnostic, MCP)

| Tool | What |
|---|---|
| `desktop_create` / `desktop_list` / `desktop_remove` | Background desktops (pyvda). Remove parks survivors on graveyard, never on Main. `app=` starts only allowlisted programs (refused before the desktop is created) |
| `app_launch` | Launch an allowlisted exe on the target desktop + event-driven retrieval (proven ownership only). Outside the allowlist: clear refusal; Downloads/Temp executables always refused |
| `window_list` | Windows of a desktop (hwnd, pid, title - deliberately no class/rect, answers stay small) |
| `see` | Screenshot (parallel), inline JPEG. Sole basis for decisions. Sparse: unchanged windows answer text only (no image bytes), `region='x,y,w,h'` captures just that crop, `max_width=640` for a compact image (PNG file only with `VISION_DESKTOPS_SAVE_CAPTURES`) |
| `hop_click` / `hop_type` / `hop_key` / `hop_scroll` | Hopping: there -> real input -> back on main (lock-serialized). `see_after` (default true) returns the result screenshot in the SAME call - no extra `see` roundtrip; `see_after=false` = text only |
| `desktop_see` / `desktop_click` / `desktop_type` / `desktop_key` / `desktop_scroll` | Desktop level for system UI without an app window (Start/Run/Tray). Refuses Main hard |
| `delta_list` / `delta_view` / `window_adopt` | Main delta without pixels (hwnd/PID/title/time+reason), targeted single view without retention, explicit adoption by vision verdict |
| `show_main` | Emergency: back to desktop #1 |
| `help` | Full protocol text (loops, removal verdicts, handoff/isolation, retrieval v2, adoption, limits) - the server instructions carry only the short core |
| `diagnose` | What the server swallowed: health facts + ring buffer of the last errors with tracebacks (formerly silent `except: pass`). CLI: `python -m vision_desktops.diagnose [--demo]` |

Loop per desktop (window level): `see` -> decide -> `hop_*` -> verify via `see`.
Loop for system UI (desktop level): `desktop_see` -> decide ->
`desktop_click` / `desktop_type` / `desktop_key` / `desktop_scroll` -> verify via `desktop_see`.

In short: see = window photo without switching, without taskbar. desktop_see = 1920x1080 fullscreen at origin (0,0) with taskbar, costs 1 switch ~480ms. Search/Start/Run/Tray always via desktop_see -> desktop_click/type/key, never see -> hop_*. Scale coordinates from each image using size_px/image_px. Before hop_type always see + hop_click + verify via see. desktop_remove only with window_list -> see verdict + decisions. Main is refused hard.

Interleave desktops = parallelism. Three loop rules:
- **Caret first:** Before `hop_type` always look via `see` and click into the
  target field (`hop_click`) - after background hops focus is not guaranteed
  in the editor. Then verify via `see`. Server side: before typing, `hop_type`
  polls `GetGUIThreadInfo` (2s) for keyboard focus in the window and refuses
  without focus (no blind typing); then calmer typing pace (~25ms) + length-
  scaled drain so no characters stay queued when switching back to Main on
  laggy systems (hit 3: `*EDGE IRTY`).
- **Long text:** `hop_type` types up to 20 chars for real; beyond that it
  automatically uses clipboard + `ctrl+v` (one event instead of thousands -
  WinUI layout would otherwise choke). Clipboard is restored. What goes where
  stays a vision decision.
- **System UI instead of window deadlock:** If OS chrome (Start/Run/Tray) holds
  the foreground, the window level correctly refuses (no tracked window, no
  blind typing). The desktop level needs none: its guard is "right desktop +
  verified arrival (poll, 2s timeout)" instead of "right window". Coordinates
  from `desktop_see` pixels, inputs via the same real-mode path - no third
  path. Main is refused by assertion (code, not docs). Honest limit: UAC /
  Secure Desktop is unreachable from virtual desktops by OS design - not a bug,
  a docs point.

## Single-instance apps (e.g. Chrome, Edge)

Some apps allow exactly one instance per data directory (Chromium
`ProcessSingleton`: lockfile + forwarding to the running process). Without its
own directory, an `app_launch` Chrome on desktop 2 lands in the Main process -
neither job nor `tree_pids` match. The server reports generically
(`handoff:true` + delta entry `foreign-host`, no app name) and leaves the
window on Main until the agent adopts it after `delta_view` by vision verdict
with `window_adopt` - no automation, no window theft:

```
chrome.exe --user-data-dir="C:\Users\Paul\.vision-desktops\profiles\<desktop>" --no-first-run --no-default-browser-check --new-window about:blank
```

- Empty folder = Chrome creates it (own cookies/history/lock).
- One folder per desktop = own process = freely movable.
- With CDP: additionally one `--remote-debugging-port` per desktop
  (otherwise the same conflict via the DevToolsActivePort file).
- Honestly measured: without its own folder Chrome delegates to the Main
  profile - the window may then appear only seconds to over a minute later
  (depending on load). `wait_seconds` is the control; if it appears in time,
  the server reports `handoff:true` instead of guessing.

### Chrome WITH your logins (main profile) - CAUTION!

If you want Chrome **with your real logins/cookies** (your account),
**NEVER** start `app_launch` with a `--user-data-dir` argument - that
deliberately creates an empty profile WITHOUT logins!

Only ever the fully human way:

1. `desktop_create` - start an empty screen
2. `desktop_see` -> via `desktop_key` (`win`) / `desktop_click` on the taskbar
   or Win search for `chrome` -> Enter -> verify via `desktop_see`

So launch Chrome via taskbar or Win search like a human, not via the argument
when starting the box! `handoff:true` is normal here (single-instance forwards
to the Main process, yet the new window still opens on the target desktop).
Be careful: never guess/copy a profile path, never force a second instance
onto the same main profile via arguments - corruption/lock risk.

## Installation (research, no security hardening!)

```powershell
cd C:\Users\Paul\vision-desktops
pip install -e .
python -m compileall -q src
python tests\selftest.py   # 2 Notepads in parallel, visibly hopping back and forth!
```

MCP registration - **Codex** (`C:\Users\Paul\.codex\config.toml`):

```toml
[mcp_servers.vision-desktops]
command = "C:\\Users\\Paul\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"
args = ["-m", "vision_desktops"]
env = { PYTHONPATH = "C:\\Users\\Paul\\vision-desktops\\src" }
startup_timeout_sec = 30
tool_timeout_sec = 180
```

**OpenCode** (`opencode.json`):

```json
{ "mcp": { "vision-desktops": {
  "type": "local",
  "command": ["C:\\Users\\Paul\\AppData\\Local\\Programs\\Python\\Python312\\python.exe", "-m", "vision_desktops"],
  "environment": { "PYTHONPATH": "C:\\Users\\Paul\\vision-desktops\\src" } } } }
```

**Claude Code**: `claude mcp add vision-desktops -- <python.exe> -m vision_desktops`
(plus `PYTHONPATH` pointing at `src`).

Then restart the agent. Example prompt:

> Create 2 desktops (research, build). Launch Notepad on each.
> Take turns: see -> hop_type -> verify via see. Clean up everything at the end.

## Environment (opt-in, all optional)

| Variable | Effect |
|---|---|
| `VISION_DESKTOPS_SAVE_CAPTURES=1` | `see` / `desktop_see` additionally write the full PNG into `~/.vision-desktops/captures` (kept: 200). **Default off** - no file, no `saved_to`, the folder stays empty; the embedded JPEG is what the agent sees anyway |
| `VISION_DESKTOPS_LOG=stderr` | Log swallowed errors (tracebacks) to stderr. **Never stdout** - stdout is the MCP transport |
| `VISION_DESKTOPS_STATE=<dir>` | State directory (default `~/.vision-desktops`) |
| `VISION_DESKTOPS_ALLOWED_APPS=a.exe,b.exe` | Extend the launch allowlist for `app_launch` / `desktop_create` (comma-separated, operator-side environment only - the model cannot change it) |

## Manual operation via harness (no agent)

`tests/harness.py` calls individual tools directly. Important in PowerShell:
inline JSON breaks quoting - so always pass args via file:

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
$env:PYTHONPATH = "C:\Users\Paul\vision-desktops\src"
& $py tests\harness.py list-tools
Set-Content "$env:TEMP\args.json" '{"desktop": "w1"}'
& $py tests\harness.py window_list "@$env:TEMP\args.json"
```

## Status

Research intermediate step: the main agent parallelizes tasks across desktops.
Later: one agent per desktop. Release hardening (agent approvals, sandbox)
follows - the launch allowlist for `app_launch`/`desktop_create` is in place,
the rest is listed in `SECURITY_TODO.md`. **Do not release until then.**

## Verified (self-test green, re-verified 19/10/2026 with the v2 move chain)

- 2 desktops in parallel: `see` at the same time, hop inputs serialized with
  return to main. Vision proof via pixel delta.
- `hop_type` + `hop_key` (incl. combos like `ctrl+a`) work on background
  desktops, incl. modern WinUI Notepad.
- `hop_click` delivers precise real clicks (verified: tab-close ``x``
  hit first try, window reacted exactly).
- Guard rule: without a verified foreground there is no click - an error
  message rather than a blind click into foreign windows.
- **Event-driven retrieval v2 (ownership-gated hide):** hook only reports;
  proven windows (job/tree pid at event time) are hidden at discovery
  (async, fail-open - foreign windows are never hidden), every non-move
  classification restores them via show_window. Measured restore guarantee
  (`hide_balance`: hidden == moved+restored+gone+restore_failed, no foreign
  hides). Suspend-start with job object (fallback `tree_pids` for
  broker/store/explorer/nested job), 5-s upper bound + 0.5-s quiet criterion,
  auto-move only on proven ownership (job OR tree).
  Foreign-host candidates go to the Main delta (hwnd/PID/title/time+reason only,
  no screenshot) and stay on Main until `window_adopt`. Four threads: hook
  reports, decider (ms decisions only; launch loops run in parallel in the
  caller), watcher retrieves late windows (updater, login) "any time", COM
  executes (hop before move). `wait_seconds` > 5 is explicitly reported as
  `wait_capped` (no silent capping). Handling client->move < 250ms (measured
  64ms/3ms incl. the v2 move chain, our share, 19.10.2026; SHOW->move
  ~1.0-1.8s is cold app-init, not retrieval).
  Parallel launches share the decider without pile-up (2 launches, 7.4s wall
  incl. the 5s window + 0.5s quiet criterion vs 45s timeout), with collision
  queue (2-s upper bound, open: measure) ->
  delta. SHOW->move flash stays informational (app-init dominated; proven
  windows are hidden at discovery, hidden windows never persist invisibly -
  see hide_balance). Pin check (never move view/app), owner co-move, frame-host child mapping, focus only on actually
  lost foreground (before/after). Only windows with a real client area
  (no sinks). Measurement:
  `tests/retrieval_measure_test.py` (latency/flash, pin/restore, parallel).
- **Hide balance (measured restore guarantee, 11.10.2026):** every window
  hidden via the ownership gate ends visible somewhere or dead -
  `hidden == moved+restored+gone+restore_failed`, nothing pending:

  | phase | hidden | moved | restored | gone | restore_failed | foreign | pending | balanced |
  |---|---|---|---|---|---|---|---|---|
  | launch (measure test) | 6 | 1 | 4 | 1 | 0 | 0 | 0 | yes |
  | watcher, delay-30s (idle run 7) | 5 | 2 | 3 | 0 | 0 | 0 | 0 | yes |

  `window_list` reports `pid` per window (additive field, ends blind
  measuring against untracked handles).
- Win11 behavior (generic, no app-specific paths): single-instance apps forward
  windows to running processes (delta `foreign-host`, adoption only explicit
  by vision verdict).
- **Graveyard desktop (verified):** `desktop_remove` never deletes with live
  windows - survivors (e.g. save dialogs) are parked EXPLICITLY on the
  permanent `graveyard` desktop (reserved record, reachable via `hop_*` /
  `desktop_*`) instead of implicitly wandering to the neighbor desktop.
  Rotation: more than 10 parked -> oldest via WM_CLOSE. Name reserved, record
  not deletable. The move never touches ownership/parenting. If a window can
  neither be closed nor parked, `desktop_remove` honestly reports it as
  `needs_agent_vision` - the agent resolves it via vision loop.
- **Remove protocol (no silent death):** No `desktop_remove` without an agent
  verdict. First look via vision (`window_list` -> `see`: dirty? dialog?
  needed later?) and decide per window: save (`hop_`), park (graveyard), or
  approve drop (`decisions={hwnd: 'drop'|'park'|'save'}`). Without a verdict
  windows are only parked, never deleted - `removed:true` alone never means
  "content was clean". Returns: `parked_on_graveyard` + `pending` (still needs
  verdict) + `needs_agent_vision` for open items. `save` parks and stays
  pending.
- **Honest handoff (field, not prose):** On single-instance forwarding,
  `app_launch` reports `handoff:true` + `foreign:true` (+ delta `foreign-host`,
  generic: PID, no app name). Foreign windows stay on Main until explicit
  adoption. Foreign PIDs are never killed (`killed:[]` for a dead starter).
- **Desktop level (09/06/2026, verified):** `desktop_see` (fullscreen via BitBlt
  after verified switch) + `desktop_click` / `desktop_type` / `desktop_key` via
  the same real-mode path. Test: Win opens Start on the worker desktop (pixel
  diff), Escape closes it, Main refused 4x without a single switch. `see` now
  reports `image_px` (JPEG size) alongside `size_px` so agents scale correctly
  on downscaled images.
