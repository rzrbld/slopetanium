# Slopetanium development

This repository owns the independently updated store, not the BananaOS firmware
or the apps it installs. Read README.md and the relevant existing tests before
changing package detection, installation, storage or display handling.

## Ownership and compatibility

- Keep application payloads in their authors' repositories/releases. Download and
  install their original assets; do not vendor app code or silently rewrite their
  scripts/binaries to accommodate a handheld.
- PortMaster ports use vanilla harbourmaster. Native Linux apps use the Apps
  backend. On BananaOS, ports appear in PortMaster/Ports and apps get home-menu
  tiles. Slopetanium itself is a home-menu app, not a PortMaster-menu entry.
- Auto-detection must support both port.json and legacy root launchers importing
  control.txt/get_controls. A root launcher forwarding to an app's launch.sh is
  not automatically a port. Preserve explicit user installer/launcher choices.
- Do not guess among multiple native launchers. Require an explicit entry or an
  appropriate upstream packaging fix. Reject unsafe archive paths and collisions
  without damaging the current installation.
- Apps, Slopetanium code, settings and caches must work on writable exFAT storage:
  no required symlinks, Unix ownership or executable mode bits. External settings
  stay outside the updatable package directory.
- Self-update uses staged replacement/rollback with a supported native-app
  manifest. Preserve settings, backups and user-created files; reject incompatible
  old self-update packages. Firmware seeds only a missing installation.
- A separately authorized app fork (for example rzrbld/gba-ds) is another normal
  app source, not a reason to special-case or copy its runtime into this store.

## UI, controls and tests

- Support unequal panel resolutions and explicit physical top-panel rotation.
  Do not assume two 640x480 screens or stretch all content from a 4:3 canvas.
  Keep D-pad/buttons and touch mappings correct after layout changes.
- Treat rumble as optional. Haptic failure must never disable basic input.
- Keep Python core compatibility with Python 3.7 and the UI's documented toolchain/
  glibc floor. Do not introduce unavailable compiler builtins without a fallback.
- Use tests/test_core.py, tests/test_apps.py and tests/test_native_self.py for
  changed backend behavior. UI changes need tests/test_ui_layout.py and a real
  unequal-panel check. Consult .github/workflows/build.yml for exact commands.
- Check the API's busy state before stopping an app or changing installation
  files. Never terminate an active PortMaster installation to finish a test.

## Commits and releases

- Every authored commit includes
  Co-authored-by: Codex <noreply@openai.com>
- Use the established author identity, preserve licenses and keep changes focused.
  Documentation-only handoffs may use [skip ci]. Do not commit generated binaries,
  credentials, raw personal logs, games or backups.
- Public app release assets are built/published by this repository's CI. Never
  upload a local replacement over an existing release or rewrite published tags.
- Keep the firmware seed pin in rzrbld/bananaos_duo separate from this app's own
  independently installed version. The firmware's nightly/manual-only full build
  policy does not prevent this smaller app's own normal CI builds.
