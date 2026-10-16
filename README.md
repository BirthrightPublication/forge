# FORGE — Local Production Studio

**V0.4.0 · Studio, Queue 3.0 and Black Scripting · Windows 11 · 16 October 2026**

A Windows-local production studio opened in your default browser. V0.4 adds compact tool workspaces, a resource-aware queue, the Black Scripting visual workbench and four local generators while retaining V0.3 authorization, processing and update recovery. Application source is included; your original batch files are kept for reference and are not executed by the web service.

## Update the existing installation

Save `FORGE-v0.4.zip` in **`B:\Forge\Build`**, without extracting it. Replace the standalone **`Update FORGE.bat`** in **`B:\Forge`** with the V0.4 copy supplied alongside this release. This also brings the external recovery updater up to date. Finish or cancel active/queued work, then double-click the BAT and press Enter to update.

The updater finds an installation directly in that directory or one extracted `FORGE*` child folder. It refuses to guess between multiple installations. It preserves the active folder and virtual environment rather than moving or recreating them. **Do not extract a second active copy beside the first one.**

The update is local: no dependency or model installation is performed. Backups are placed in `B:\Forge\Backups`. An idle service is stopped before application replacement. V0.2 and later can also freeze job admission during maintenance; avoid using the old V0.1 tabs while performing the first upgrade. See `START HERE.txt` and `docs/UPDATE_GUIDE.md`.

For a new installation only, extract the ZIP, run `Setup FORGE.bat`, then `Open FORGE.bat`. Setup uses installed 64-bit Python 3.11–3.13 with Tcl/Tk and creates a private `.venv`. It asks before downloading core dependencies. Start-menu/taskbar/Send-to integration uses the existing Windows launcher setup.

## What changes in V0.4

- Compact charcoal/gold tool pages with centered inputs, accessible icon tabs, retained per-tool settings, presets and visible Start/Add to Queue controls.
- Queue 3.0 has Active & Queued, Failed and History tabs; selection, dispatch pause/resume, cancellation, retry, order changes, output details and record removal that retains physical files. Auto or manual limits admit at most five workers, constrained by CPU, GPU, network and disk budgets.
- **Black Scripting** opens from the global gold header control, outside the normal sidebar. Start with an empty horizontal chain, add known media operations, edit their settings or preset snapshots, disable/reorder/duplicate nodes, choose an output and preview dependencies before submitting. Linear versioned JSON scripts support save, duplicate, rename, import and export.
- **Identity Studio**, **Obfuscation Studio**, **Random Address Generator** and **Random String Generator** share bounded generation, configuration presets and explicit TXT/CSV/JSON export. Names/addresses are fictional or synthetic. Generated values stay out of job history and presets; secure strings use OS entropy. Supported region vocabularies and country formats are listed in each tool.
- **Metadata Hyper Cleaner** adds Standard/Maximum privacy reduction, actual embedded-preview inspection where supported and opt-in before/after or remaining-data reports. **QR Studio** supports text/URL/contact/Wi-Fi, color/size/quiet zone, PNG/SVG previews, bulk ZIP export and local content presets. Wi-Fi credentials are memory-only and cannot be queued or saved in presets.
- Stable Birthright module IDs, entitlement/provider interfaces, offline policies and signed-license adapter boundaries are present. The default development provider grants every feature offline. No billing, activation, identity collection or remote license transport is implemented.

V0.3's local Smart Update Manager and recovery protections are retained. Environment → Application and package updates → Check local build folder reads local ZIPs, verifies compatibility and shows rollback availability. Update/rollback console actions retain final native confirmation.

Read [the V0.4 feature and compatibility notes](docs/V04_RELEASE.md), [commercial dependency review](docs/COMMERCIAL_DEPENDENCIES.md), [update guide](docs/UPDATE_GUIDE.md), and [validation report](docs/TEST_REPORT.md).

## Bookmark and browser authorization

Use `http://127.0.0.1:8765` as the bookmark. Open FORGE once through its launcher in the desired browser profile. V0.2 replaces tab-scoped credential storage with a remembered, HttpOnly, SameSite=Strict browser cookie valid for up to 90 days. The runtime's long-lived native token is not written into new browser URLs or browser storage; the launcher supplies a short-lived, single-use ticket instead.

New normal tabs and ordinary service restarts retain browser access. Clearing site data, using a private window, another browser profile, expiration, or selecting **Forget this browser authorization** requires another launcher opening. Existing tabs retain their request token when you reopen the launcher.

A bookmark is not a Windows process launcher. After a Windows restart or an explicit service stop, use the taskbar/Start launcher first. Closing only the browser does not stop queued work. The default port is stable; the server no longer silently chooses an alternate port. An intentionally changed `FORGE_PORT` requires a corresponding new bookmark.

This remains a trusted, single-user local application. It binds to IPv4 loopback, checks Host/Origin/Fetch-Site, rejects cross-origin preflights, and requires a per-session CSRF header on browser mutations. Other local software running with your user privileges is outside this isolation boundary. Cookies are host-scoped, not a guarantee of isolation from every other local service. There is no remote/LAN mode, cloud login, tracking code, or third-party frontend resource.

## Implemented tools and controls

| Area | Retained production capabilities |
|---|---|
| Transcribe | Original-language/English-translation task, selectable approved Whisper models, CPU/CUDA/Auto, vocabulary hints, segment/sentence/word timestamp layouts, raw or conservative cleaned copies, review flags and subtitle layout targets |
| Transcript outputs | TXT, SRT, VTT, JSON, CSV and standalone HTML; JSON enables an in-Studio searchable viewer with timestamp-linked source playback |
| Optional speaker labels | Separate CPU pyannote environment and trusted local Community-1 model; anonymous speaker labels assigned by greatest temporal overlap, not identity recognition |
| Retuning | Detected source sample rate, entered source/target tuning, optional tempo preservation, quality/MP3 bitrate/WAV bit depth, audio or copied-primary-video outputs, optional single-pass loudness normalization |
| Metadata Hyper Cleaner | Read-only inspector, embedded-preview inventory, Standard/Maximum reduction, selective media tag removal/editing and optional PRIVATE reports |
| Downloads | One job per URL; audio/video format and quality controls, bounded playlist ranges, available human/automatic subtitle sidecars, three safe filename layouts, optional explicitly supplied cookies file |
| QR / generators | Local QR PNG/SVG/contact/Wi-Fi/bulk preview; passwords/tokens/aliases/fictional addresses with explicit export and no generated-value history |
| Queue | Total and resource-specific limits, persistent order, selected-job actions, dependency blocking, workflow group cancellation/retry, stage/speed/ETA when measurable |
| Black Scripting | Empty visual workbench, up to 24 versioned linear nodes, preset snapshots, previewed dependency plan, retune/metadata followed by optional transcription |
| Environment | Guided optional installers, isolated read-only GPU diagnostics, model cache inventory, consented model-download jobs, release metadata checks and export settings |
| Navigation | Optional pinned tools and a scrollable navigation region that keeps Environment accessible in shorter windows |

The source still uses dependency-free browser JavaScript plus FastAPI and SQLite; it does not require a React/Node frontend build chain.

## Transcription details

Install the optional engine from Environment or `Install Transcription.bat`. The first model download needs explicit consent. Without consent the worker uses locally cached model files only. Environment can queue downloads for six approved model identifiers; it does not automatically discover, switch to, or execute arbitrary internet models.

**Raw recognition is not a guaranteed verbatim record.** Speech recognition and timestamps are estimates. Sentence layout is derived from timed words and punctuation rather than a separate linguistic alignment engine. Word timing is faster-whisper's timing, not WhisperX forced alignment or frame-perfect editing data. Review flags are heuristics based on recognition scores, not calibrated accuracy probabilities.

**Clean** means whitespace normalization and optionally removing English “um/uh” fillers. It does not paraphrase, fact-check, censor, invent words, or perform an LLM editorial rewrite. A clean job always saves additional `_raw.txt` and `_raw.json` companions using original ASR segment boundaries, even when only another clean format was selected.

Subtitle width, line count and duration are targets. A very long single word, a long speech interval, an overlap or unusual script can require manual adjustment. Rare captions without word timing use proportional interpolation for text splits. Audio/video browser playback depends on the browser supporting that codec/container; successful transcription does not imply every source format will play in the embedded player.

Standalone HTML has no remote assets or network calls. Select the matching local media file inside the viewer to enable seeking. It does not bundle a duplicate of a potentially huge video. The Studio viewer streams a registered job source through an authenticated Range-capable endpoint. CSV exports neutralize leading spreadsheet-formula markers in spoken text.

### Speaker labels

`Install Speaker Labels.bat` creates `.diarization-venv`, separate from the main environment. It downloads Python packages only after confirmation. You must obtain a complete offline **Community-1** model directory from its publisher, satisfying any access conditions; a bare Git pointer checkout or only `config.yaml` is insufficient. FORGE does not request or save a Hugging Face account token.

Select that directory, enable speaker labels, and set plausible minimum/maximum speakers. Model use is offline; pyannote metrics and Hugging Face telemetry are disabled in the runner. This release runs the separate speaker pass on CPU, regardless of ASR GPU selection. Speaker turns are estimates; overlaps, brief utterances and background speech may require manual correction. Actual optional model inference was not validated in the build environment.

## Universal queue and Black Scripting

Auto adjusts the configured ceiling (up to five workers) to measured CPU/system-memory pressure. Manual uses the configured ceiling. CPU, GPU/Auto-transcription, network and disk caps further constrain dispatch. NVIDIA free VRAM is measured through nvidia-smi where available and compared with explicitly estimated model budgets plus a reserve. Missing GPU telemetry serializes GPU work; it does not display invented memory values. Matching GPU models are serialized rather than loaded concurrently. Auto transcription conservatively reserves a GPU slot even if it later falls back to CPU. Model downloads do not run alongside speech inference using the shared cache. Limits take effect for new dispatch; changing them does not forcibly terminate current work.

Move queued jobs up/down. Priority does not break dependencies or preempt a running task. **Pause dispatch** stops new starts, not an in-progress encoder. **Retry** starts new work from the beginning and never claims checkpoint-level continuation. Failed, cancelled or interrupted parents block their dependent steps.

Black Scripting creates an independent chain for each source. They pass the parent's actual published media output, including any collision-safe filename, to the next step. Each step uses the selected export destination and keeps completed intermediate files. Transcription must be last because its outputs are text. Current Black Scripting nodes accept Standard/Maximum wipe and supported media selective/edit options. The legacy workflow API retains its old restriction for compatibility. Download-to-workflow chaining, visual branches and arbitrary shell/script steps are not included.

Saved scripts retain their own option snapshots, even if an underlying tool preset changes later. Retrying an entire group creates new linked jobs and new non-overwriting outputs. Retry a group rather than separately retrying a child whose parent did not complete.

The UI lists every active or failed job plus the latest 500 records; older finished records remain in SQLite. Browser desktop notifications are optional and need explicit permission plus an open authorized tab. They are not a separate Windows notification service. No ETA is invented where the adapter cannot measure progress; playlist progress messages describe the current item rather than an accurate whole-playlist completion forecast.

## Files, destinations and safety

Native browse, pasted absolute paths and Explorer Send to preserve the real source path. Browser drops do not reliably expose that path; FORGE creates a clearly labeled local staging copy and changes output selection to a default/custom export location. Use native-path input for reliable same-folder export and to avoid copying large media unnecessarily.

Output modes are source folder, custom directory, last custom directory and default export folder, with an optional simple subfolder. File names receive descriptive suffixes and collision-safe numbered alternatives. There is no replace-original mode. Processing happens in job-owned staging directories; outputs are verified before publication. If a source changes after queuing, the job requires review/retry rather than silently processing a different input.

Media metadata editing remuxes compatible streams without intentionally re-encoding. Images support wipe only; JPEG/PNG/WebP compressed image data is retained where supported and no orientation transform is required. Orientation correction can re-encode. Standard preserves compatible color profiles/subtitles; Maximum removes them, which can affect color appearance. Capability reports disclose each format boundary. Metadata reduction is not an anonymity guarantee: visible content, steganography, watermarks, filenames, filesystem history and necessary container/codec tags can remain. Orientation is applied before image metadata removal; stripping a color profile may change appearance. PDF, Office and animated-image sanitization remain unsupported.

Retuning assumes the entered original tuning; it cannot establish how the recording was originally tuned. Lossless output encoding cannot recover information lost in a lossy input. Normalization is single-pass; requested LUFS/true-peak targets are not a certified post-encode measurement.

Download only media you are entitled to save. Website restrictions, authentication, codec availability, JS runtimes and upstream changes can still prevent a download. Playlists are limited to 500 selected entries per URL. Complete entries from a partly failed playlist can be retained with a warning. There is no automatic browser-cookie extraction, arbitrary yt-dlp config execution or remote component execution.

## Data, updates and recovery

Persistent data defaults to `%LOCALAPPDATA%\FORGE`; `FORGE_DATA_DIR` is an explicit override. This includes the database, local model cache, browser staging, private worker records and runtime status. Do not share this directory or update backups casually: paths, history, settings and opted-in reports may be private. Generated passwords are excluded from database history, but copying a password places it on your normal OS clipboard.

Code updates preserve local environments, tools, data and models. The release manifest covers application file hashes and rejects path traversal, duplicate paths, links, unexpected files and protected directories. **These hashes detect corruption, not publisher authenticity; releases are not digitally signed.** Use only a build you trust. An import/create-app smoke check follows copying; it is not a complete GPU, installer or codec certification.

Failure recovery restores backed-up code. Manual code rollback retains current user data and the separate database snapshot; it does not silently restore an older database. Newer saved options may need adjustment in older code. Backup/journal handling reduces update risk but is not a power-loss or filesystem-failure guarantee. See the update guide.

Release checks contact PyPI for version metadata only. Optional startup checks are cached for 24 hours; no packages/models are installed merely by opening FORGE. Guided engine installation is a separate, explicitly approved action. Signed release distribution, comprehensive dependency rollback, arbitrary code/script execution, scheduling, online terminology research and additional transcription intelligence remain later work. Black Scripting imports validated definitions of known operations only.

## Tests and build source

See `docs/TEST_REPORT.md`, `docs/PYTHON_TEST_RESULTS.txt`, `docs/BROWSER_CHECKS.json` and `docs/UPGRADE_INTEGRATION.json`. Automated checks ran on Linux. Native Windows BAT/PowerShell/Explorer integration, Edge/Brave/Firefox acceptance, actual speech/speaker inference, GPU performance and live website downloads remain local acceptance checks.

`python -m pytest` runs unit/API/media/updater tests after installing development requirements. The browser and full-upgrade harnesses require explicit disposable directories and test-write consent; never point them at production data. `scripts/package_release.py` creates a manifest-verified source release in a selected output directory and excludes data/environments/tools. Existing Windows users should use the supplied updater, not run the packaging script.
