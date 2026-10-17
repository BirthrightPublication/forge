# FORGE V0.4 feature and compatibility notes

V0.4 builds on the completed V0.3 source. It adds a compact Studio, Queue 3.0, Black Scripting and the expanded local privacy utilities. All development entitlements are granted offline. Existing launchers, ports, browser authorization, Build/Backups locations and updater behavior are retained.

## Studio and Queue 3.0

Tool settings are grouped into relevant icon tabs with keyboard arrow/Home/End navigation, focus tooltips and one active section. The source area is centered above settings; output controls remain outside the tabs. Start submits and resumes dispatch; Add to Queue retains the current pause setting. Native-path and staged-browser sources keep their existing distinctions.

Queue defaults to Active & Queued. Failed/blocked/interrupted work has a separate badge; completed/cancelled records are in History. The UI includes every active or failed job and the latest 500 records; older terminal records remain in SQLite and can be queried using the queue API. Bulk actions act independently and report individual refusals. Record removal never deletes published files and is refused where a retained dependency still needs the record. Priority controls do not preempt running work or bypass dependencies. Retry creates fresh work; Pause affects new dispatch, not an encoder already running.

Auto and manual modes both cap concurrency at the configured ceiling, at most five. Auto reduces admission under measured CPU/system-memory pressure. Resource caps cover CPU, GPU, network and disk. NVIDIA telemetry is used only when the selected physical device can be identified; unknown telemetry conservatively serializes inference. Model VRAM budgets are estimates, explicitly distinguished from measured free memory. Matching GPU model loads are serialized because isolated processes cannot share an already-loaded model. The scheduler delays admission rather than silently changing models or killing active inference. This is conservative admission, not a guarantee against every engine allocation failure.

The dock identifies the active file/tool and running/queued counts. Percentages appear only for measured progress; unavailable ETA/VRAM are not fabricated. Native Open launches verified audio/video/raster and plain-text transcript output files on Windows using fixed operations. HTML/SVG use browser preview/download or the containing folder.

## Black Scripting

The gold header launcher opens the full-width workbench. There is no normal sidebar entry. A new workbench starts empty. Plus adds a known operation; clicking a node opens its current settings or a saved tool preset. Nodes can be duplicated, removed, reordered and disabled. The output terminal has a real destination configuration; it is not a dummy processing job.

Supported operations are retune, metadata and final transcription, with up to 24 nodes. A script is a schema-versioned typed node/connection definition. Execution is linear only; branches, cycles, disconnected edges and unsupported node versions are rejected. Download chaining, arbitrary Python/BAT/shell scripts and graphical branches are outside this release. Scripts import only validated JSON definitions of known operations.

Save, rename, duplicate, import and export operate on local script definitions. Options are snapshots, so changing a tool preset does not silently alter a saved script. Legacy V0.2/V0.3 workflows are projected into this workbench without rewriting their original records. Preview validates registered sources, enabled steps, formats and dependencies before Run. The preview token binds the configuration and source fingerprint; changed plans require another preview. Each source gets a separate chain; a failed parent blocks its child. Actual published output paths feed subsequent operations, and completed intermediate files remain available.

## FORGE OS

- Identity Studio: fictional/legal-style names, ordinary aliases and online pseudonyms; four region-inspired vocabularies, naming order/separators/suffixes and batches.
- Obfuscation Studio: fictional plate-like `A/a/9/X` patterns with allowed separators and exclusions, identifiers, usernames and secure passwords. No government record lookup or validity claim.
- Random Address Generator: clearly synthetic US/GB/CA/DE/AU/JP-inspired layouts, field selection, unit option and postal-code patterns. It does not verify deliverability or represent real residences.
- Random String Generator: passwords, usernames, email local parts/reserved-domain emails, tokens and identifiers; policy/hex/base64url/custom alphabets with exclusions. `secrets` supplies OS randomness. Batch limit 1,000; configurable length bounds depend on type. Configuration presets never store generated values.

Generated rows stay in browser/server memory unless the user explicitly copies or exports them. TXT/JSON/CSV export is local; CSV neutralizes formula-leading values. Sensitive generator displays clear on navigation, hidden-tab/search transitions and the configured short display lifetime. Exported files and the OS clipboard are ordinary user-controlled copies. Small name vocabularies can repeat; uniqueness or cryptographic strength of fictional names is not claimed.

Metadata Hyper Cleaner supports actual JPEG/PNG/WebP and existing media formats. Standard retains color profiles and compatible subtitles; Maximum removes them. Normal image reduction retains compressed image data where supported; orientation correction can re-encode and can alter JPEG quality. Animated cleaning, PDF and Office are unsupported. Selective removal/editing applies to supported media tags. Inspection inventories known EXIF/JFIF thumbnails and FFprobe attachments; it does not inspect every embedded pixel or prove anonymity. Before/after and remaining-data reports are opt-in and can contain private metadata. Originals remain unchanged.

QR Studio handles text, HTTP(S) URL, vCard contacts and Wi-Fi credentials. PNG/SVG output supports size, quiet zone, dark/light colors and error correction. Low-contrast/inverted colors are rejected. Direct previews/downloads and bulk ZIP (50 items) do not create jobs. Text/contact jobs and presets persist locally with disclosure. Wi-Fi mode or WIFI: text is rejected by persistent queue/preset endpoints. Explicitly exported Wi-Fi images still contain credentials. Actual decoding tests cover PNG and rasterized SVG payloads; test target-phone scanning before distributing codes.

## Birthright foundation

`forge/entitlements.py` defines stable `birthright.forge.*` module IDs and versions, entitlement-provider access checks, a signed-token-verifier protocol, verified-grant data, an offline expiry/device policy, and distinct commercial-update eligibility. A future verifier must validate issuer/audience/signatures; V0.4 provides no production signing keys, token endpoint, billing, account/device registration or network transport. The development provider enables all known features and requires no activation. Generated values, media, paths and transcripts never go to a licensing service.

## Upgrade and visual-reference boundaries

Place `FORGE-v0.4.zip` intact in `B:\Forge\Build` and use `B:\Forge\Update FORGE.bat`. New settings are additive, and script/generator presets use separate keys. Source rollback preserves current user data; older V0.3 code simply does not expose V0.4-only definitions. Finish or cancel all work before update/rollback. See the update guide for snapshots and recovery.

The brief referenced a design screenshot, custom Black Scripting icon and review video/transcript that were not attached. Functional work and an original charcoal/gold refinement are implemented, but exact reference matching and the supplied custom icon cannot be verified until those assets are provided. Current screenshots document the implemented interface. Windows 11 native integration, GPU inference and live downloader/site behavior remain target-machine acceptance checks, as detailed in the test report.
