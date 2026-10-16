# Changelog

## 0.2.0 — 2026-10-16

- Replaced tab-scoped browser authorization with remembered HttpOnly sessions and short-lived one-use launcher tickets; preserved Host/Origin/CSRF protections and added explicit forget control.
- Stabilized the loopback port and added updater/startup interlocks.
- Added a self-contained Update FORGE.bat for Build-folder ZIP updates, full file-manifest verification, protected paths, code/SQLite backups, failure rollback and interrupted-update recovery.
- Added native maintenance freezing in V0.2 for subsequent updates; idle checks cover the whole queue.
- Added resource-aware dispatch, queue order, batch controls, workflow dependencies, failure blocking and graph retry.
- Added saved local media workflows with immutable option snapshots and non-overwriting intermediate/final outputs.
- Added sentence timing layouts, subtitle layout targets, conservative clean copies with raw companions, CSV/HTML exports and interactive transcript review.
- Added an optional isolated local speaker-labeling adapter; requires external engine/model setup and target-PC validation.
- Added metadata inspection, selective media tag removal/editing and before/after review.
- Added bitrate/bit-depth and optional single-pass normalization controls.
- Added bounded playlists, subtitle sidecars and fixed filename layouts to download adapters.
- Added guided optional installers, read-only GPU diagnostics, cache inventory and consented model-download jobs.
- Added favorites and scrollable navigation; preserved the established Studio palette/layout.
- Retained originals, history, presets, environments and non-destructive output behavior.

## 0.1.0 — 2026-10-16

Initial working framework and tool adapters. Added local launcher/source for Windows, loopback authorization, independent Birthright-inspired studio, persistent universal queue, dedicated isolated workers, original-path and browser-staging inputs, non-destructive publication, destination controls, presets and environment/release discovery.

Added working local QR/password/image metadata functions and real FFmpeg media retuning/metadata processing. Added optional faster-whisper and yt-dlp adapters with explicit installation/model-download permission and honest not-installed indicators. Original uploaded files retained unchanged, never executed by the app.

Validation-driven fixes include sample-rate-aware pitch processing instead of automatic Rubber Band selection after measured short-tone drift; PNG palette/transparency retention; collision-safe file publication; interrupted-job recovery; no-speech subtitle export; password display clearing on navigation; browser storage restrictions handled without breaking startup; UTF-8 native dialog transport; and top-of-page queue actions to avoid scrolling to submit.

Future approved scope is tracked in README.md; it is not marked completed in this release.
