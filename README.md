# FORGE — Local Production Studio

**V0.1.0 · first functional development release · 16 October 2026**

A Windows-oriented, local browser studio that replaces repeated visits to desktop batch files. Start the launcher, select a tool, configure its settings and add work to a shared persistent queue. Processing is performed on your computer by a Python service bound to `127.0.0.1`.

The interface follows the approved Birthright-inspired direction: charcoal panels, restrained gold accents, serif branding, compact sidebar navigation and two-column settings. It is a new independent implementation, not a copy of Birthright's source or assets. Yu Mincho is used only when already installed; no fonts are distributed. The front end is plain HTML/CSS/JavaScript, with no npm build or remote CDN required.

## Start on Windows

1. Extract the **whole folder** to a permanent, writable local location, such as `C:\Users\YOUR_NAME\Apps\FORGE-v0.1`. Avoid ZIP previews, Program Files, network shares and cloud-synchronized application folders.
2. Run **Setup FORGE.bat** and approve its package installation prompt. A 64-bit Python 3.11–3.13 installation with Tcl/Tk must already be available. Setup creates `.venv` inside FORGE, installs pinned direct core dependencies from PyPI, checks them, and records the resolved environment in `installed-versions.txt`.
3. Run **Open FORGE.bat**, or search Windows for **FORGE Local Production Studio**. Setup attempts to compile the included C# launcher into `FORGE.exe` using the local .NET Framework compiler and create a Start shortcut. If compilation is unavailable, the batch launcher remains usable. Nothing starts automatically at Windows login.
4. Open **Environment** in the studio. Configure FFmpeg/FFprobe if not detected. Install optional engines below for transcription or downloading.

No administrator access is requested. Setup uses an execution-policy override for that PowerShell process only; it does not change the machine's permanent policy. A managed computer can still prohibit local scripts or compilation. Do not disable system security controls to force installation; review the policy with the device administrator instead.

To pin FORGE, use Windows' **Pin to taskbar** option on the generated executable or Start shortcut where available. Setup can also create an optional **Explorer → Send to → FORGE** shortcut. It does not automatically rearrange the taskbar, install a Windows service or add firewall rules. `Repair Start Shortcut.bat` recreates shortcuts after a folder move, although an existing Python virtual environment should be recreated after moving the app.

## What is implemented

| Area | V0.1 behavior |
|---|---|
| Local studio | Default-browser launcher, overview, seven tool pages, environment checks, search, responsive/collapsible navigation |
| Universal queue | Persistent SQLite history, per-job status and progress, 1–3 worker slots, dispatch pause, cancel, retry, crash recovery, output details/reveal |
| File handling | Multi-file native selection, pasted absolute paths, browser staging, non-recursive folder import (up to 500 files), source/custom/last-custom/default destinations, optional subfolder |
| Output protection | Originals are not overwritten; completed outputs use tool-specific names and collision numbers; job-owned temporary work is separate |
| Transcription adapter | Select model, CPU/auto/CUDA, language, original-language transcription or translation to English, vocabulary hints, VAD, search width, timestamp detail and TXT/SRT/VTT/JSON exports |
| Retuning | Assumed source/target reference tuning, 440→432 default, actual input sample-rate detection, optional tempo compensation, MP3/FLAC/WAV/M4A and primary-video MP4/MKV output |
| Downloads adapters | Separate audio/video pages; URL queues; format, audio quality, video height limit, optional metadata and optional Netscape cookie-file path |
| Metadata reduction | JPEG/PNG/WebP re-encoding; supported audio/video remuxing; optional private before/after audit report |
| QR codes | Local PNG/SVG creation, correction level, module size, quiet-zone width, output naming and actual preview |
| Passwords | Cryptographic random selection, length/count/character policies, ambiguous-character exclusion, optional Unicode, copy/clear; never added to the queue |
| Preferences | Local tool presets and export preferences; cookie paths excluded from saved download presets |
| Updates | Manual or opt-in daily-on-start PyPI release discovery. Displays installed/latest versions; does not install code or models |

All media operations use dedicated adapters rather than executing uploaded batch files. Your eight supplied files, including the duplicate transcription script and `.lnk`, are preserved byte-for-byte in `original_scripts/`. FORGE never invokes them. Their original destructive behavior still exists if you run those archived scripts yourself.

The YouTube `.lnk` references `C:\yt-dlp\yt-dlp-download.bat`; that target was not supplied. The Video Download page is a new yt-dlp integration, not a recreation or audit of unseen script contents.

## Optional engines and external tools

**Stop the local service before installing packages**, not just the browser tab. Use `Stop FORGE.bat`, then run the relevant installer and reopen FORGE.

| Component | Action |
|---|---|
| Local transcription | Run **Install Transcription.bat**. It installs faster-whisper 1.2.1 and dependencies into FORGE's own environment. Model weights are not bundled. |
| First model | Select a model in Transcribe and explicitly enable **Allow first model download from Hugging Face**. Subsequent jobs can use its cache with that permission off. Start with a short clip and `base`/CPU to check your installation. |
| GPU | The CUDA choice requires a compatible NVIDIA driver, CTranslate2 and GPU runtime libraries. FORGE does not install these. Auto can retry recognized CUDA failures on CPU. Availability detection does not prove your GPU is working. |
| Audio/video download | Run **Install Downloads.bat**. An explicit installer run resolves the latest stable 2026 yt-dlp release permitted by its requirements file and records installed versions. Normal launches do not install or update it. |
| FFmpeg + FFprobe | Reuse trusted local copies. Blank settings check the app's `tools` locations, common `C:\ffmpeg\bin`/`C:\yt-dlp` locations and PATH. Or specify the exact executable/bin folder in Environment. Keep FFprobe beside FFmpeg for download post-processing. |
| YouTube JavaScript runtime | The download adapter detects Deno or Node on PATH. Full current YouTube support also uses yt-dlp's standard EJS package extras. No runtime is downloaded silently, and remote-component fetching is disabled. |

Direct core dependencies are pinned to the versions used in validation; these are **not a promise that every dependency is the newest release**. Transitive dependencies are resolved by pip, not comprehensively hash-locked. Automatic compatibility migrations, signed release delivery and rollback are future work. V0.1 has no hosted FORGE update server. Review changes before editing requirements or rerunning installers.

Upstream references consulted for the implementation:
- Python: https://www.python.org/downloads/windows/
- faster-whisper API and GPU prerequisites: https://github.com/SYSTRAN/faster-whisper
- yt-dlp embedding/dependencies: https://github.com/yt-dlp/yt-dlp
- FFmpeg builds/download information: https://ffmpeg.org/download.html
- FFmpeg audio filters: https://ffmpeg.org/ffmpeg-filters.html
- Python cryptographic randomness: https://docs.python.org/3/library/secrets.html
- Browser file-input behavior: https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/input/file

## Drag-and-drop and destinations

**Use Native browse or Paste paths for large files and same-folder export.** These pass the original absolute path to the service without copying the media into a browser upload. Explorer's optional Send to integration does the same; image-only selections open Metadata, other selections open Transcribe.

Ordinary browser drops and browser file inputs intentionally hide the real source path. FORGE cannot reverse that browser restriction. Those files are streamed into local staging storage and marked **STAGED**. Their output mode switches to the configured default folder, with a visible notice. You can choose the original folder explicitly as a custom destination, or reselect the file with Native browse. The app refuses a misleading same-source destination for a staging copy.

Output modes are **same folder as each original**, **custom folder**, **last custom folder**, and **default export folder**. A custom path can be typed even before that directory exists; a native folder picker selects existing folders. Optional subfolder names must be a simple single folder name. UNC network paths are not enabled in V0.1; mapped/local drives still need to be available when the job runs.

Names such as `Interview_transcript.srt`, `Track_432hz.mp3`, and `Photo_metadata-reduced.png` are chosen by the tool. Existing outputs receive `(1)`, `(2)`, etc. There is no replace-original or silent-overwrite switch. Custom filename-template editing is not yet implemented.

## Queue behavior

The default allows two independent worker processes, configurable to one or three. Only one transcription job is dispatched at a time to limit memory contention. Large jobs can still consume substantial CPU, GPU memory and disk space; choose fewer workers when necessary.

Closing the browser does not stop processing. **Pause dispatch** prevents queued jobs from starting; it does not suspend an active encoder or preserve its memory state. **Cancel** terminates an active worker and its child processes. Cancellation closes during final publication so completed files can be committed safely.

**Retry** creates a new job and starts processing from the beginning. There is no claimed checkpoint-level transcription resume. After a crash or deliberate service stop, unfinished running jobs are marked interrupted, pending jobs remain stored, and dispatch is paused for review. Files already published remain present. A source whose size or modification time changed after it was queued is refused until retried/reselected. This is a change check, not a cryptographic source-integrity guarantee.

The dashboard displays the most recent 500 jobs. History is not encrypted or automatically purged. Job work directories and staging copies can use significant disk space. Environment's cleanup action removes only unused browser staging copies, not original source files or exports. Rare crash leftovers may remain as `.forge-<job-id>.work` folders beside outputs; inspect and remove only clearly abandoned job-owned folders after stopping the service.

## Tool boundaries

**Transcription:** The output is unedited speech-recognition text, not a guaranteed verbatim record. No editorial rewrite, censorship pass, fact replacement or internet terminology search occurs. Vocabulary is a recognition hint, not evidence that a word was spoken. TXT can omit timestamps or show segment/word timing; JSON preserves estimated words when requested. SRT/VTT use segment cues even when word timing is selected. Silence yields a no-speech TXT note and an empty SRT when requested. Speaker labels, forced alignment, interactive video-linked transcripts and clean/editorial secondary outputs are not implemented yet. CPU/CUDA model inference has not been executed in this build environment.

**432 Hz:** This changes pitch by target reference divided by assumed source reference. It does not detect whether the source is actually tuned to 440 Hz, and it does not make every sound a 432 Hz tone. No health effect is claimed. The pipeline uses the measured sample rate and optional tempo compensation; file-boundary timing can vary slightly. WAV output is 24-bit, FLAC is lossless encoding of the processed signal, and MP3/AAC are re-encoded. Retuning itself changes the signal. Video exports copy the primary video and retune the first audio track, omitting extra tracks/subtitles/attachments. Compatible original tags may be copied when selected, but embedded cover art is not preserved by this adapter.

**Metadata:** Supported images are JPEG, PNG and WebP; animated images are refused. Re-encoding applies orientation and removes carried EXIF/text/ICC fields; ICC removal can change appearance and JPEG is recompressed. Palette colors and PNG alpha are preserved in tests. Supported media containers are MP3, WAV, FLAC, M4A, MP4, MKV, MOV, WebM, OGG and Opus. Audio/video streams are copied while chapters, covers, attachments, subtitle and data streams are excluded. Containers may regenerate structural/technical tags. This is metadata reduction, not secure erasure or forensic anonymization. PDF, Office files and arbitrary binary sanitization are not supported. The optional `PRIVATE-audit.json` includes ORIGINAL metadata; keep it private and do not publish it with the cleaned file.

**Downloads:** Use media you are authorized to obtain. Website availability, authentication and extractor compatibility can change. One URL is one job; playlist expansion and live capture are not enabled. The adapter uses yt-dlp's Python API, not your global yt-dlp config, plugins or shell hooks. Supplied cookie files are copied into private job work for use, leaving the original cookie file unchanged. Paths and URLs are stored in local job history; cookie contents are not deliberately logged but remain sensitive wherever you store the source file. Live downloads have not been tested in this environment.

**Passwords:** Values are returned only to the authorized browser page and are not saved in presets, SQLite history or log messages. The display clears after 60 seconds or navigation away. Copying puts the value in the system clipboard; FORGE does not clear or control clipboard history, synchronization, screenshots, extensions, swap or browser memory. This is a generator, not a password vault or an assured memory-erasure tool.

## Privacy and security

FORGE binds only to the loopback address, uses a per-start authorization token, checks Host/Origin/Fetch-Site headers, denies cross-origin API access, has no permissive CORS, and exposes only allowlisted static files and job outputs. Processing uses validated argument lists rather than arbitrary browser-supplied shell commands. These are practical protections, not an independent security audit or a hardened multi-user sandbox.

No account, telemetry client, remote font or analytics dependency is included. QR generation does not contact an external QR service. The local tools are usable offline once their dependencies and models are present. Optional package installs, explicit model downloads, requested media downloads and opt-in PyPI checks contact the respective services; normal network metadata can be visible to those services and the network. Remote media is untrusted input to decoders; keep the media tools updated.

Local app data defaults to `%LOCALAPPDATA%\FORGE`: settings/history database, staging, model cache, work manifests, service log and a runtime authorization file. This directory contains local file paths, URLs, QR content and job settings, unencrypted. Do not publish or sync it casually. The app directory holds the private Python environment and source. Browser extensions and other software running as your Windows user remain within the local trust boundary. Generated passwords are deliberately excluded from persisted application records.

## Troubleshooting

- **Nothing opens:** run `Setup FORGE.bat` first, then `Open FORGE.bat`; inspect `%LOCALAPPDATA%\FORGE\service.log`. Run launchers from the extracted folder, not from ZIP preview. Do not open `web/index.html` directly.
- **Authorization error:** reopen with the launcher. The secret is refreshed each service start; stale browser tabs need a newly authorized launch.
- **Native browse fails:** check that Python includes Tcl/Tk. Use Paste paths as a fallback. Native dialogs can appear behind other windows; the app tries to bring them to the front.
- **FFmpeg missing:** enter its exact local path in Environment. A pip package named ffmpeg is not the executable. FFprobe should be beside it.
- **Transcription says model unavailable:** explicitly enable the first model download once. Afterwards use cached-model mode. Model names are an allowlist; newly published engines are not automatically imported.
- **CUDA error:** test a short clip on CPU. GPU compatibility depends on the installed model runtime, driver and CUDA/cuDNN combination. Do not assume a visible GPU means an end-to-end successful test.
- **Download fails:** inspect the job's error, check the installed download engine/runtime and release discovery, and test an authorized public video URL. V0.1 deliberately does not silently fetch execution components or your browser cookies.
- **Queued jobs do not start:** inspect the dispatch pause control. Intentional service shutdown/recovery leaves the queue paused. Confirm a worker slot is available.
- **Move or remove FORGE:** stop it first. Remove its Start/Send to shortcuts manually if uninstalling. App data and exports are separate and are not automatically deleted; keep or remove them deliberately. Moving `.venv` is unreliable; recreate it in the new app folder using setup.

## Validation and continuing development

See `docs/TEST_REPORT.md` for exact executed tests and untested paths. This is a Windows-targeted development release validated on Linux, not a claim that Windows/GPU integration has already been verified on your PC.

The Python backend tests are in `tests/test_forge.py`. Install `requirements-dev.txt` into a development environment and run `python -m pytest -q --tb=short`. Real FFmpeg integration tests skip when FFmpeg/FFprobe are absent. A browser transport-bridge harness is included under `tests/browser_bridge_check.py`; it can be run explicitly against a disposable local test runtime and does not run during ordinary pytest.

The original full roadmap remains the direction, not a list of completed features. V0.2/V0.3 work includes speaker diarization/alignment, cleaned/editorial companion transcripts, richer publication metadata, interactive media-linked transcripts, template editing, more format coverage, controlled updates and rollback, optional terminology research and an approved custom-script importer. V0.1 contains no fake working switches for those features.
