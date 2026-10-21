# SECURITY_TODO - required before any release (deliberately NOT done now)

Status: research prototype for functional testing. No security release.

To complete before release:
1. Tool approvals per agent (Codex/OpenCode/Claude): default-deny hop_* and
   app_launch, approval per call or allowlist.
2. `app_launch` allowlist: only signed/trusted paths, no executables from
   Downloads/Temp without confirmation.
3. No admin operation: never run the server elevated; windows running as admin
   are then deliberately not controllable.
4. State/capture directories: restrict ACLs to the user, never leave sensitive
   screenshots unencrypted at rest (retention + deletion).
5. Input guard: hop only onto own desktops, never type into password/admin
   windows without explicit approval. Already enforced in code: `_enter_desktop`
   refuses the Main/host desktop by assertion (applies to BOTH window and
   desktop levels); window hops additionally need foreground proof, desktop
   hops need verified arrival (poll).
6. Audit log: record every hop (desktop, hwnd, action, time) append-only.
7. Network: later review per-desktop restrictions (sandbox/AppContainer).

Until then: local only, own user only, no release, no foreign prompts.
