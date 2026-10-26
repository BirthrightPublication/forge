# Changelog

## 0.9.0 — dependent development candidate; verification in progress

- Build on the complete V0.8 candidate at `e2d66c7281107283bc81d3fcd6ebaa0a13118a64`, preserving its separate PR and all speech tools. V0.8 is not yet merged or released.
- Rename Download Studio to Media Downloads and introduce persistent Current Download Priority with manual-single, playlist and six-level channel scheduling, review approval, deterministic fairness and frozen batch progress.
- Keep pending discoveries outside the Universal Queue until transactional resource admission; preserve media identities, prior queue associations, approved interruptions and supported transfer state across restart.
- Add persistent selected-playlist imports, watched-channel destinations, future-only Instant Download controls, compact responsive channel cards and visual thumbnail previews without thumbnail exports.
- Extend real yt-dlp byte/stage reporting, verified output publication and private crash/retry recovery while retaining FFmpeg, Black Scripting, protected data and external updater compatibility.
- Stable publication remains subject to dependency review, final automated/native checks and explicit owner approval.

## 0.8.0 — candidate; CPU acceptance passed, CUDA/owner decision pending

- Carry the V0.7.1 deferred-script startup repair into V0.8 without merging or rewriting its separate PR/history.
- Add revisioned speech projects, chapters, named speakers, source-preserving markers, reading styles, pronunciation rules, custom expressions and immutable configuration snapshots.
- Add original Kokoro Onyx and complete installed voice inventories, normalized compatible embedding blends, searchable/favorite/pinned voice profiles, and task-exclusive Ming 0.5b adapters.
- Add opt-in model/environment installation with pinned revisions, verified artifacts, isolated CPU/CUDA environments and recoverable model quarantine. Tiny and Qwen remain unavailable where no verified runtime exists.
- Add 31 actual ordered DSP effects, region processing, bypass/A-B comparison, measured waveforms, real preview/export renders and preserved originals.
- Add safe EPUB/PDF/TXT/Markdown/HTML/SRT/VTT import, reusable verified segment caches, exact silence, incremental chapter assembly, cache-only export and WAV/FLAC/MP3/OPUS/M4B production exports.
- Add four Bark 3.0 English recognition catalogs with separate runtime, bounded audio chunks and truthful TXT/JSON output; retain Whisper timing, translation and speaker labels.
- Register speech and effects in the Universal Queue and existing linear Black Scripting. Preserve data/updater contracts.
- Native Windows CPU Kokoro/Ming/four-Bark/two-Whisper acceptance and real queued speech passed in run `40402290962`, with pinned model notices retained and isolated runtime fixes verified. No GPU runner is configured; CUDA hardware acceptance and the owner's release decision remain pending. This candidate has not been merged, tagged or published.

## 0.7.1 — startup repair candidate, pending owner review

- Wait for all deferred interface extensions before starting browser authorization and rendering. A fast local API could previously render Download Studio too early and report `Cannot read properties of undefined (reading 'description')`.
- Distinguish interface, engine connection and authorization failures, with recovery instructions appropriate to each. Browser rendering errors no longer direct users to reinstall dependencies.
- Read the setup banner version from `VERSION.txt` instead of displaying the historical V0.3 label. Keep the running-service guard and all data-preservation contracts.
- Add delayed-script startup and injected rendering/connection failure browser regressions.

## 0.7.0 — 2026-10-23 (published)

- Unify manual audio/video downloads with persistent Channel Watch and Download History; retain legacy routes, presets and verified media workers.
- Add saved/pinned/default destinations, stable source folders, content rules, priorities, optional historical imports, live/replay transitions, bounded retries and persistent download identities.
- Diagnose downloader/runtime/EJS/authentication/network failures, protect local cookie files, resume supported partial transfers and verify outputs before collision-safe publication.
- Replace the centered update modal with a compact right drawer, measured transfer progress, installation history and cancellable pending maintenance. Separate application approval from compatible component batches.
- Verify fresh imports and restarted root/PID/version/health before recording update completion. Preserve queued work, backups, environments and models; refuse to open or stop another installation's runtime.
- Use compact icon-first navigation, Settings in the former queue slot, unclipped tooltips, bottom-anchored Local Engine, left search and dynamic branding. Black Scripting toggles back to the previous workspace.
- Add form-ready synthetic address fields, copy/regenerate/lock controls and coherent US/UK/CA/AU/DE/JP samples. Addresses remain available during external form filling; generated secrets retain their clearing protections.
- Extend Python, browser, real local HTTP media, upgrade/rollback and native Windows release gates. Live YouTube access and physical GPU testing are reported separately.

## 0.6.0 — 2026-10-22 (published; see V0.6 release verification)

- Add Birthright Updates with actionable counts, current/available versions, changelogs, Later, individual installation and compatible batch installation.
- Introduce the application-independent Birthright Update Core and tested Ed25519 manifest contract; current GitHub assets retain their existing HTTPS/SHA-256 publication mode until publisher signing keys are configured.
- Download selected transcription models automatically, verify publisher file hashes, coordinate caches across processes and retain immutable model revisions.
- Diagnose CUDA 12/cuBLAS/cuDNN 9 loading separately from actual inference, avoid repeated known-broken CUDA dispatch and report Auto CPU fallback.
- Add approved dependency-class maintenance with queued-work preservation, environment backup, wheel staging, validation, recovery and automatic restart.
- Remember safe working settings separately from presets, without automatically storing content or secrets.
- Persist measured execution/stage durations and refine the compact charcoal/gold interface, FORGE branding and sidebar scrolling.
- Keep the existing Windows `app/` ZIP/checksum, source history, protected paths and rollback contracts. Native Windows and physical GPU evidence are reported separately.


## V0.5.0 Windows release packaging correction

- Added `FORGE-v0.5.0-windows-x64.zip` and its matching `.zip.sha256` contract, with the complete tagged application payload beneath `app/`.
- Excluded protected updater files, user state, original scripts, test fixtures and historical documentation from this installation payload. The existing V0.5.0 tag, application code and legacy release assets are preserved.
- Added test-gated Windows packaging automation for new tags, published releases and explicit backfills. Validation exercises the supplied PowerShell updater in disposable Windows installations, checks installed application startup and verifies state preservation before uploads.
- Native validation exposed the supplied updater's incorrect relative-path calculation when TEMP uses an 8.3 alias. Documented invoking the unchanged updater with this process's TEMP set to `B:\Forge\Build`; acceptance verifies that staging there preserves existing build ZIPs and user state.

## 0.5.0

- Added a Media Privacy workspace with internal navigation for Watermark Studio, Audio Fingerprint Lab, Video Fingerprint Lab, Media Tracking Auditor, Media Privacy Exporter, Content Credentials Inspector and Privacy Verification Lab.
- Added encoded text/logo and audible-clip watermarks, placement presets, repeated patterns, bounded movement, timing/fades, local asset validation, real encoded previews and NVIDIA H.264 attempts with CPU fallback. Visible marks require a new 8-bit H.264 encode; audio-only marks can retain the original primary video packets.
- Added local audio descriptors, spectral visualizations and optional installed-FFmpeg Chromaprint support; added sampled video perceptual hashes, color profiles, selected-frame previews and sampled comparison timelines. Results measure the selected methods and do not establish anonymity or defeat proprietary recognition.
- Added explicit reusable local fingerprint JSON references and bounded first-input comparisons without automatic reference indexing or external media lookup.
- Added shared parser-visible metadata/stream inspection and optional pinned C2PA inspection. Remote manifests, online revocation, remote identity resolution and trust-list refresh are disabled; signature integrity, issuer trust and media binding are reported separately.
- Added Standard/Advanced/Custom privacy exports with source-specific stream/encoding plans, safe remux or explicit re-encode, actual output inspection and optional private JSON/HTML verification reports.
- Extended Black Scripting with all seven operations, media-preserving inspection steps, original-to-current verification and actual collision-safe media handoffs. Saved V0.4 scripts and legacy workflow identifiers remain compatible.
- Added comparison/reference/asset snapshots, resource-aware media dispatch, bounded local decoder protocols, report controls and optional `scripts\Install Media Privacy.bat` setup. Core processing remains usable without the C2PA package.
- Preserved updater paths, originals, existing tools, environments, models, presets and data. Archived exact V0.4 documentation and validation evidence under `docs/history/v0.4` before editing current release notes.

The owner approved merging and releasing V0.5.0. Validation passed 478 Python tests, 324 real Chromium checks and upgrade/code rollback from V0.1, V0.2, V0.3 and V0.4. The release URL is [v0.5.0](https://github.com/BirthrightPublication/forge/releases/tag/v0.5.0); `VALIDATION.json` records CI tests/source and `CHECKSUMS.sha256` records exact asset hashes. Local upgrade evidence is in `docs/UPGRADE_INTEGRATION.json`. Native Windows and physical NVIDIA acceptance are not claimed. See `docs/V05_RELEASE.md` for capabilities, retention defaults and known boundaries.

## Repository publication — 2026-10-16

- Established the authoritative GitHub repository, permanent contributor instructions and source-only exclusions.
- Preserved V0.2–V0.4 source history; removed one unused Windows shortcut containing machine identifiers before initial publication. Application code is unchanged. Original history remains privately archived.
- Added test-gated release automation and explicit build instructions. Existing MIT terms retained with owner approval.
- During the initial V0.4 publication, V0.5 stayed on its development branch while release validation continued.

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
