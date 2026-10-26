# FORGE — Scripting Environment Engine

**V0.9.0 development candidate · Media Downloads 2.0 · Windows 11**

FORGE runs local production tools in your browser. V0.9 adds persistent download planning, priority scheduling, selected playlists and compact Channel Watch cards. It retains the complete V0.8 speech workspace, voice profiles and Kokoro blends, Ming adapters, audio effects, audiobook assembly and optional Bark recognition. Whisper, privacy tools, Black Scripting and verified updates remain available.

This branch depends on the unmerged V0.8 candidate `e2d66c7281107283bc81d3fcd6ebaa0a13118a64` ([PR #8](https://github.com/BirthrightPublication/forge/pull/8)); it preserves that branch and its history. V0.8 passed native Windows CPU model acceptance in run [40402290962](https://github.com/BirthrightPublication/forge/actions/runs/40402290962). CUDA hardware validation and its release decision remain outstanding. The published release remains [v0.7.0](https://github.com/BirthrightPublication/forge/releases/tag/v0.7.0). V0.9 is a development candidate requiring owner review before merge, tag or publication. See [V0.9 architecture and verification](docs/V0.9-MEDIA-DOWNLOADS.md) and the existing [speech engines](docs/V0.8-SPEECH-ENGINES.md), [effects](docs/AUDIO-EFFECTS.md), [audiobook](docs/SPEECH-BOOKS.md), and [Bark](docs/BARK-ASR.md) guides.

## Source and releases

The authoritative repository is [BirthrightPublication/forge](https://github.com/BirthrightPublication/forge). Published Windows releases contain exactly one `FORGE-vMAJOR.MINOR.PATCH-windows-x64.zip` and its `.zip.sha256` companion, with application files under `app/`. A legacy offline ZIP is retained separately. Release packages are GitHub assets, never source files. The V0.9 candidate uses `FORGE-v0.9.0-windows-x64.zip` and legacy `FORGE-v0.9.zip`; these become release assets only after final approval and passing release gates.

Read [AGENTS.md](AGENTS.md) for the permanent development and publication rules, [build instructions](docs/BUILD.md), and [the initial publication audit](docs/PUBLICATION.md). The existing MIT license is retained with the owner's approval; third-party dependencies and user files retain their own terms.

## Update the existing installation

**If `B:\Forge\Update-Application.ps1` is installed:** finish queued work, close FORGE, and run your existing `B:\Forge\Update FORGE.bat` using the [temporary-path instructions](docs/WINDOWS_RELEASES.md) to avoid the supplied updater's shortened-path bug. The script downloads the single Windows ZIP from the latest release, verifies its SHA-256 and stages `app/`; type `UPDATE` only after reviewing its plan. Keep both installed updater files. This package excludes them, settings, presets, history, user media, models, environments and original scripts. Backups and logs use `Update-Backups` and `Update-Logs`.

**For the older offline updater:** the following procedure continues to use the legacy package. Do not replace a working GitHub updater with the legacy standalone BAT.

Save the intact legacy ZIP for the approved release (currently `FORGE-v0.7.zip`) in **`B:\Forge\Build`**, without extracting it. Use the accompanying standalone **`Update FORGE.bat`** in **`B:\Forge`**. Finish or cancel active/queued work, then double-click the BAT and press Enter to update. Use the named release asset and its checksums, rather than GitHub's automatically generated source ZIP.

The updater finds an installation directly in that directory or one extracted `FORGE*` child folder. It refuses to guess between multiple installations. It preserves the active folder and virtual environment rather than moving or recreating them. **Do not extract a second active copy beside the first one.**

The update is local: no dependency or model installation is performed. Backups are placed in `B:\Forge\Backups`. An idle service is stopped before application replacement. V0.2 and later can also freeze job admission during maintenance; avoid using the old V0.1 tabs while performing the first upgrade. See `START HERE.txt` and `docs/UPDATE_GUIDE.md`.

For a new installation only, extract the ZIP, run `Setup FORGE.bat`, then `Open FORGE.bat`. Setup uses installed 64-bit Python 3.11–3.13 with Tcl/Tk and creates a private `.venv`. It asks before downloading core dependencies. Start-menu/taskbar/Send-to integration uses the existing Windows launcher setup.

## What changes in V0.7

- Download Studio retains the existing downloader and presets, with Single Download, Channel Watch and Download History views. Destination favorites and per-source/type rules persist locally.
- YouTube channel/playlist discovery uses stable IDs, optional historical backfill, bounded polling/backoff, independent media records and atomic queue admission. Other platform adapters are not advertised as implemented.
- Updates opens a compact right-side panel. Application upgrades require their own approval; compatible component batches exclude them. Pending maintenance can be cancelled, and completion requires fresh imports plus process/root/version/health verification.
- Icon-first navigation includes Settings, tooltips and anchored Local Engine status. Queue access remains in the header and bottom dock. Black Scripting restores the previous workspace when exited.
- Synthetic addresses have separate fields, per-field copy/regeneration/locks, full-address copying and options-only presets. Geographic samples remain coherent; generated addresses are not deliverability checks.

## Existing Media Privacy tools

- **Media Privacy** groups seven workspaces behind one sidebar entry, with internal tool navigation, existing compact settings tabs, source-specific processing plans and local evidence views.
- **Watermark Studio** encodes text/logo marks and audible ownership clips; it supports placements, repeat patterns, configurable movement/timing/fades, presets, batches and actual encoded previews. NVIDIA encoding is attempted when available, with CPU fallback.
- **Audio Fingerprint Lab** and **Video Fingerprint Lab** measure local descriptor agreement, provide spectral/frame evidence, and compare selected media with explicitly retained local references. Scores describe the selected method; they are not anonymity scores or recognition probabilities.
- **Media Tracking Auditor** and **Content Credentials Inspector** report parser-visible metadata, stream characteristics and available C2PA evidence. Signature integrity, issuer trust and media binding remain distinct findings.
- **Media Privacy Exporter** provides Standard, Advanced and Custom policies. **Privacy Verification Lab** compares the actual original/output files, remaining metadata and optional signal/frame similarity. Reports have explicit retention controls.
- **Black Scripting** accepts the seven new operations. Inspection nodes pass the same media to the next operation; verification can compare the chain's original with the actual transformed output. Existing saved V0.4 scripts remain usable.

The compact charcoal/gold Studio, Queue 3.0 controls, four local generators, Metadata Hyper Cleaner, QR Studio and offline entitlement foundation from V0.4 are retained. The default development provider grants features offline; no new billing, activation, media upload, or remote fingerprint index is introduced.

V0.3's local Smart Update Manager and recovery protections are retained. Environment → Application and package updates → Check local build folder reads local ZIPs, verifies compatibility and shows rollback availability. Update/rollback console actions retain final native confirmation.

Read [the V0.5 release and compatibility notes](docs/V05_RELEASE.md), [commercial dependency review](docs/COMMERCIAL_DEPENDENCIES.md), and [update guide](docs/UPDATE_GUIDE.md). Check the version stated in [the validation report](docs/TEST_REPORT.md); archived V0.4 evidence does not validate V0.5.

## Use the Media Privacy tools

Open **Media Privacy**, choose its internal tool tab, and add local sources using Native browse or pasted absolute paths. Browser staging remains available with its existing copy/destination disclosure. Configure the compact settings sections, choose an output directory and review **Processing plan** before **Start** or **Add to Queue**. The source files are preserved. Completed jobs expose saved outputs and, when JSON reporting is enabled, **View evidence**.

| Tool | Practical starting point |
|---|---|
| Watermark Studio | Choose Video, Audio or Both. Enter text or select a local PNG/JPEG/WebP logo; choose a font preset or local TTF/OTF. For audible identifiers, select a recorded announcement or owned audio clip. Set placement, timing and output, then use **Encode preview** before queuing. |
| Audio Fingerprint Lab | Add the baseline recording first, followed by comparison recordings. Choose the analyzed initial window and report formats. The lab compares each subsequent input with the first and shows spectral evidence; Chromaprint is used only when the installed FFmpeg supports it. |
| Video Fingerprint Lab | Add the baseline video first and other versions next. Choose sample count, initial time window or explicit frame times. Inspect sampled frames, descriptor agreement and the sampled comparison timeline. |
| Media Tracking Auditor | Select media and choose whether to include provenance or fingerprints. Review categorized metadata, streams, attachments/previews and method-specific findings. Inspection leaves the source unchanged. |
| Media Privacy Exporter | Choose Standard to remove supported descriptive metadata while retaining compatible extras, Advanced to remove subtitles/artwork/attachments/chapters as well, or Custom to choose individual policies. Keep re-encoding off for compatible stream copy; review the actual source-specific plan. |
| Content Credentials Inspector | Install the optional local C2PA reader if needed, then inspect embedded manifests, assertions, signature integrity, issuer trust and media binding. Unsupported and unknown results remain explicit. |
| Privacy Verification Lab | Add the original first and its processed copy second. Enable optional audio/video comparisons and report saving as needed. Inspect actual remaining fields, stream/codec differences and supported comparison evidence. |

The watermark preview is a real encoded excerpt of the first five seconds, not a player-only overlay. Later scheduled marks appear in the full export. Preview playback is temporary and clears on navigation or after five minutes; queue the full job to retain an output. Visible overlays require a new 8-bit H.264 encode, which can alter HDR/color fidelity. Audio-only watermarking can copy the original primary video while encoding the audio mix. Audible insertions must not overlap; up to 100 insertions and clips up to five minutes are supported. There is no automatic speech synthesis or claim of robust inaudible watermarking.

Fingerprint reports default to JSON and HTML; reusable raw references are **off** until **Retain reference** is selected. To compare a saved reference later, use **Compare retained local references** and browse/paste its `.forge-fingerprint.json` path. FORGE does not build a hidden media index or contact an acoustic lookup service. Imported references are user-supplied descriptors, not authenticated proof of a recording's origin. A comparison accepts at most 20 media/reference records.

Audit and provenance reports default to enabled. Privacy Exporter and Privacy Verification Lab default to **not saving** private reports; verification still runs. Disabling reports does not erase the normal queue record, source paths, selected options or brief completion findings. Reports and frame previews may expose original metadata, filenames and recognizable content. Select retention deliberately and review files before sharing; see [the detailed retention table](docs/V05_RELEASE.md#report-and-reference-retention).

### Optional Content Credentials setup

FFmpeg/FFprobe and the existing Pillow dependency provide the core watermark, metadata and fingerprint operations. The optional reader is `c2pa-python==0.38.0`. Stop FORGE, run **`scripts\Install Media Privacy.bat`**, review the package-download prompt, approve it if desired, then reopen FORGE and check **Local inspection capabilities**. The installer contacts PyPI to obtain packages; media inspection itself stays local. It does not download a separate GPU runtime or change model caches.

C2PA inspection disables remote manifest retrieval, online revocation checks, remote identity resolution and trust-list refresh. It reads the explicitly selected media, not automatically discovered sibling sidecars. A valid signature or signed assertion is not independent proof of every asserted fact; an untrusted issuer and invalid signature are different results. Missing or unsupported credentials do not prove anonymity. FORGE does not create, sign or forge Content Credentials.

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
| Black Scripting | Empty visual workbench, up to 24 versioned linear nodes, preset snapshots, previewed dependency plan, retune/metadata plus all seven Media Privacy operations, followed by optional final transcription |
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

Visible watermark jobs using Auto/NVIDIA also reserve a GPU lane with a labeled encoder-memory estimate, plus CPU and disk capacity for filtering and fallback. Audio-only watermarking and inspection jobs use their CPU/disk limits. Detection of an NVIDIA device and encoder does not guarantee a particular encode will succeed; CPU fallback is reported in the completed job.

Move queued jobs up/down. Priority does not break dependencies or preempt a running task. **Pause dispatch** stops new starts, not an in-progress encoder. **Retry** starts new work from the beginning and never claims checkpoint-level continuation. Failed, cancelled or interrupted parents block their dependent steps.

Black Scripting creates an independent chain for each source. Transform steps pass their actual published media output, including any collision-safe filename, to the next step. Inspection/fingerprint/provenance nodes leave the media unchanged and pass it onward, even when reports are disabled. Verification nodes compare the chain's original with the current actual media; a JSON report is never substituted for the next step's media input. Each step uses the selected export destination and keeps completed intermediate files. Transcription must be last because its outputs are text. Current nodes accept Standard/Maximum wipe and supported media selective/edit options. The legacy workflow API retains its old restriction for compatibility. Download-to-workflow chaining, visual branches and arbitrary shell/script steps are not included.

Saved scripts retain their own option snapshots, even if an underlying tool preset changes later. Retrying an entire group creates new linked jobs and new non-overwriting outputs. Retry a group rather than separately retrying a child whose parent did not complete.

The UI lists every active or failed job plus the latest 500 records; older finished records remain in SQLite. Browser desktop notifications are optional and need explicit permission plus an open authorized tab. They are not a separate Windows notification service. No ETA is invented where the adapter cannot measure progress; playlist progress messages describe the current item rather than an accurate whole-playlist completion forecast.

## Files, destinations and safety

Native browse, pasted absolute paths and Explorer Send to preserve the real source path. Browser drops do not reliably expose that path; FORGE creates a clearly labeled local staging copy and changes output selection to a default/custom export location. Use native-path input for reliable same-folder export and to avoid copying large media unnecessarily.

Output modes are source folder, custom directory, last custom directory and default export folder, with an optional simple subfolder. File names receive descriptive suffixes and collision-safe numbered alternatives. There is no replace-original mode. Processing happens in job-owned staging directories; outputs are verified before publication. If a source changes after queuing, the job requires review/retry rather than silently processing a different input. V0.5 also snapshots selected comparison sources, retained references and watermark assets, and rechecks them before use.

Media metadata editing remuxes compatible streams without intentionally re-encoding. Images support wipe only; JPEG/PNG/WebP compressed image data is retained where supported and no orientation transform is required. Orientation correction can re-encode. Standard preserves compatible color profiles/subtitles; Maximum removes them, which can affect color appearance. Capability reports disclose each format boundary. Metadata reduction is not an anonymity guarantee: visible content, steganography, watermarks, filenames, filesystem history and necessary container/codec tags can remain. Orientation is applied before image metadata removal; stripping a color profile may change appearance. PDF, Office and animated-image sanitization remain unsupported.

Retuning assumes the entered original tuning; it cannot establish how the recording was originally tuned. Lossless output encoding cannot recover information lost in a lossy input. Normalization is single-pass; requested LUFS/true-peak targets are not a certified post-encode measurement.

Download only media you are entitled to save. Website restrictions, authentication, codec availability, JS runtimes and upstream changes can still prevent a download. Playlists are limited to 500 selected entries per URL. Complete entries from a partly failed playlist can be retained with a warning. There is no automatic browser-cookie extraction, arbitrary yt-dlp config execution or remote component execution.

## Data, updates and recovery

Persistent data defaults to `%LOCALAPPDATA%\FORGE`; `FORGE_DATA_DIR` is an explicit override. This includes the database, local model cache, browser staging, private worker records and runtime status. Do not share this directory or update backups casually: paths, history, settings and opted-in reports may be private. Generated passwords are excluded from database history, but copying a password places it on your normal OS clipboard.

Code updates preserve local environments, tools, data and models. The release manifest covers application file hashes and rejects path traversal, duplicate paths, links, unexpected files and protected directories. **These hashes detect corruption, not publisher authenticity; releases are not digitally signed.** Use only a build you trust. An import/create-app smoke check follows copying; it is not a complete GPU, installer or codec certification.

Failure recovery restores backed-up code. Manual code rollback retains current user data and the separate database snapshot; it does not silently restore an older database. Newer saved options may need adjustment in older code. Backup/journal handling reduces update risk but is not a power-loss or filesystem-failure guarantee. See the update guide.

Release checks contact PyPI for version metadata only. Optional startup checks are cached for 24 hours; no packages/models are installed merely by opening FORGE. Guided engine installation is a separate, explicitly approved action. Signed release distribution, comprehensive dependency rollback, arbitrary code/script execution, scheduling, online terminology research and additional transcription intelligence remain later work. Black Scripting imports validated definitions of known operations only.

## Tests and build source

V0.5 validation passed **478 Python tests**, **324 real Chromium checks**, and incremental upgrade/code rollback from **V0.1, V0.2, V0.3 and V0.4**. See `docs/TEST_REPORT.md`, `docs/PYTHON_TEST_RESULTS.txt`, `docs/BROWSER_CHECKS.json` and `docs/UPGRADE_INTEGRATION.json` for source and local upgrade evidence. The accompanying `VALIDATION.json` identifies CI tests/source, while `CHECKSUMS.sha256` identifies exact release-asset bytes. The exact V0.4 evidence is archived under `docs/history/v0.4`; its results do not certify V0.5. Automated media tests use local Linux fixtures. Native Windows BAT/PowerShell/Explorer integration, Edge/Brave/Firefox acceptance, actual speech/speaker inference, NVIDIA hardware/performance and live website downloads remain local acceptance checks.

`python -m pytest` runs unit/API/media/updater tests after installing development requirements. The browser and full-upgrade harnesses require explicit disposable directories and test-write consent; never point them at production data. `scripts/package_release.py` creates a manifest-verified source release in a selected output directory and excludes data/environments/tools. Existing Windows users should use the supplied updater, not run the packaging script.
