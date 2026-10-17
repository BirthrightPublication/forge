# Changelog

## 0.5.0 — Release candidate

- Added a Media Privacy workspace with internal navigation for Watermark Studio, Audio Fingerprint Lab, Video Fingerprint Lab, Media Tracking Auditor, Media Privacy Exporter, Content Credentials Inspector and Privacy Verification Lab.
- Added encoded text/logo and audible-clip watermarks, placement presets, repeated patterns, bounded movement, timing/fades, local asset validation, real encoded previews and NVIDIA H.264 attempts with CPU fallback. Visible marks require a new 8-bit H.264 encode; audio-only marks can retain the original primary video packets.
- Added local audio descriptors, spectral visualizations and optional installed-FFmpeg Chromaprint support; added sampled video perceptual hashes, color profiles, selected-frame previews and sampled comparison timelines. Results measure the selected methods and do not establish anonymity or defeat proprietary recognition.
- Added explicit reusable local fingerprint JSON references and bounded first-input comparisons without automatic reference indexing or external media lookup.
- Added shared parser-visible metadata/stream inspection and optional pinned C2PA inspection. Remote manifests, online revocation, remote identity resolution and trust-list refresh are disabled; signature integrity, issuer trust and media binding are reported separately.
- Added Standard/Advanced/Custom privacy exports with source-specific stream/encoding plans, safe remux or explicit re-encode, actual output inspection and optional private JSON/HTML verification reports.
- Extended Black Scripting with all seven operations, media-preserving inspection steps, original-to-current verification and actual collision-safe media handoffs. Saved V0.4 scripts and legacy workflow identifiers remain compatible.
- Added comparison/reference/asset snapshots, resource-aware media dispatch, bounded local decoder protocols, report controls and optional `scripts\Install Media Privacy.bat` setup. Core processing remains usable without the C2PA package.
- Preserved updater paths, originals, existing tools, environments, models, presets and data. Archived exact V0.4 documentation and validation evidence under `docs/history/v0.4` before editing current release notes.

V0.5 final artifact validation, merge review and publication are pending. Native Windows and NVIDIA acceptance are not claimed. See `docs/V05_RELEASE.md` for capabilities, retention defaults and known boundaries.

## Repository publication — 2026-10-16

- Established the authoritative GitHub repository, permanent contributor instructions and source-only exclusions.
- Preserved V0.2–V0.4 source history; removed one unused Windows shortcut containing machine identifiers before initial publication. Application code is unchanged. Original history remains privately archived.
- Added test-gated release automation and explicit build instructions. Existing MIT terms retained with owner approval.
- V0.5 remains on its development branch while release validation continues.

## 0.4.0 — 2026-10-16

- Added compact tool-specific icon tabs, centered inputs and visible Start/Add to Queue actions.
- Added Queue 3.0 views, safe bulk record removal, native media output opening and a useful active-job dock.
- Added Auto/manual concurrency up to five with CPU/network/disk limits and measured NVIDIA memory admission using labeled model estimates.
- Added the dedicated Black Scripting header launcher and empty linear workbench, versioned script definitions, preset snapshots, import/export, editing and dependency-plan preview. Legacy workflow records/APIs remain compatible.
- Added Identity Studio, Obfuscation Studio, Random Address Generator and Random String Generator with secure local generation, configuration presets and explicit exports. Generated values are not saved in queue or preset history.
- Extended Metadata Hyper Cleaner with Standard/Maximum reduction, embedded-preview inspection, lossless compressed-image preservation where supported and opt-in remaining-data reports.
- Extended QR Studio with URL/contact/Wi-Fi, colors, in-memory PNG/SVG previews, bulk ZIP and non-secret presets. Wi-Fi credentials cannot enter persistent queue/preset storage.
- Added stable Birthright module IDs, offline development grants and future verification/device/update-policy abstractions, plus a dependency license review. No billing or activation.
- Preserved the V0.3 updater, local environments, models, data, originals and non-overwriting outputs.

## 0.3.0 — 2026-10-16

- Added manual local release inventory to Environment, with actual paths, semantic versions, compatibility, SHA-256, changed/new/preserved counts and per-package rejection reasons.
- Added verified rollback availability and fixed Windows updater/rollback console actions with explicit consent and final native confirmation.
- Unified browser/console preflight eligibility; incompatible, linked, target-conflicting and conflicting newest packages cannot be silently selected.
- Made interrupted-update dry runs read-only, repaired stale-marker recovery and persisted pre-maintenance queue pause preferences.
- Journaled manual rollback so interruption remains recoverable; strengthened malformed metadata, backup and Windows filename/reparse-point checks.
- Invalidated generated bytecode for replaced/restored Python sources and verified imported versions, preventing same-timestamp upgrades from reusing old code.
- Retained V0.2 tools, privacy, authorization, queue, workflows, original files and non-overwriting outputs; no dependency/model upgrade is performed.
- Added V0.3 API/browser/recovery regressions and version-independent upgrade/rollback validation for both supplied baselines. Native Windows execution remains an acceptance check.

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
