# FORGE V0.1 validation report

**Build:** 0.1.0 · **Date:** 16 October 2026

This report distinguishes executed behavior from proposed or untested integration. It is not an external security audit, a Windows certification or a guarantee of transcription accuracy.

## Results

| Check | Observed result |
|---|---|
| Python unit/API/process/media tests | **82 passed**, final run 13.46 seconds; no skipped tests in that run |
| Browser workflow/layout checks | **47 passed**, no uncaught JavaScript errors |
| Python compilation | Passed for backend, scripts and launch entry points |
| Browser JavaScript syntax | `node --check web/app.js` passed |
| Preservation of uploaded originals | **8/8 byte-identical** to the supplied attachments; hashes in `ORIGINAL_SCRIPT_SHA256.txt` |

Python test output and browser check results are included beside this report. The `pytest` count includes parametrized tests and unit tests; it does not mean 82 separate Windows end-user scenarios.

## Build/test environment

Linux x86-64, CPython 3.13.5, FastAPI 0.128.2, Uvicorn 0.48.0, qrcode 8.2, Pillow 12.3.0, psutil 7.2.2, pytest 9.0.2, HTTPX 0.28.1, Playwright 1.57.0, system FFmpeg/FFprobe and system Chromium. NumPy was used only for signal-analysis tests. No faster-whisper package/model or yt-dlp package was available for real speech inference or remote downloading.

The sandbox's managed Chromium blocks all URL navigation, including localhost. No browser policy was changed. Browser checks rendered the actual bundled HTML/CSS/JavaScript and bridged `fetch` through Playwright to the real running loopback HTTP API. This validates UI behavior and backend interaction through that transport; **it is not a normal browser-to-loopback networking test**. HTTP authorization/security behavior was tested separately with FastAPI's test client. Screenshots are Linux-rendered views of the implemented application, not screenshots from the user's Windows PC.

## Executed Python coverage

- Safe filename handling, reserved Windows names, path rejection and URL validation.
- Cryptographic password policy, configurable groups, invalid inputs and exclusion from persistence.
- API token enforcement, hostile Host/Origin/Fetch-Site handling, denied preflight and static-file allowlisting.
- Registration, browser staging, unsupported/empty uploads and refusal of same-source output for staging copies.
- Settings/presets validation, stale pause-state protection, exclusion of cookie paths from saved presets.
- Queue state persistence, dispatch pause, cancellation, retry, recovery of interrupted jobs and child-process termination.
- Real isolated-worker QR creation in PNG and SVG, concurrent same-name outputs and non-overwrite publication.
- Transcript serialization to TXT/SRT/VTT/JSON, timestamp rounding, segment/word layouts and silence outputs.
- Real FFmpeg retuning of synthetic 440 Hz audio at **44,100 and 48,000 samples/second**. Exported PCM spectral peaks were within the test's 1 Hz tolerance of the 432 Hz target. Source hashes remained unchanged.
- Real audio metadata reduction and original-file hash preservation.
- Real image metadata reduction, PNG palette/color/transparency preservation and original-file hash preservation.
- Rejection of unsupported PDF/animated-image cases and sources changed after queuing.
- Staging cleanup excluding active native/source jobs, private-audit opt-in behavior.
- **Mocked adapter contracts only** for faster-whisper CPU/CUDA selection/fallback and yt-dlp parameter assembly. These are not real model inference or remote site tests.

A validation failure was found during development in the automatic Rubber Band path: a short synthetic tone showed measurable pitch drift for one sample rate/build. The delivered implementation instead uses the detected input sample rate plus optional tempo compensation, and the two sample-rate regression tests pass. No claim of universal psychoacoustic/audio-quality superiority is made.

## Executed browser coverage

47 named checks are in `BROWSER_CHECKS.json`, including navigation across all pages; retained settings drafts; preset save/reload; real browser-file staging; staged-source destination changes; pasted original paths; custom destination controls; real queued metadata/QR work; authorized QR preview; password generation/no-history/clear-on-navigation; and responsive overflow checks at desktop, 1366, 900 and 440 px widths. The desktop viewport was 1920 × 1080. No uncaught JavaScript errors were observed.

## Not executed here

- Windows PowerShell setup, local C# compilation, Start/taskbar/Send to integration and native Explorer reveal.
- Native Tk dialogs on Windows, Windows permissions/long paths/network-drive edge cases and Windows file publication semantics.
- Browser networking in the user's normal browser session; Firefox/Edge/Safari compatibility.
- Real speech-model loading, language accuracy, long-form transcripts, diarization (not implemented) or CPU/GPU inference performance.
- CUDA/cuDNN/driver compatibility, especially on the user's actual graphics card.
- Real yt-dlp downloads, website authentication/cookies, EJS/runtime compatibility, playlists or live media (last two are disabled).
- Successful live PyPI release discovery/package installation from this container; the external-network path was not validated here.
- Exhaustive container/codec coverage, damaged/malicious media fuzzing, hour-long resource endurance or power-loss testing on NTFS.
- Formal security review, installer signing, deterministic cross-platform dependency resolution, or rollback (not implemented).

## Suggested first Windows acceptance pass

Use a permanent local folder and run setup. Check that the launcher opens the default browser, Environment shows the expected tools, and Native browse returns an original path. Run one local QR job and reveal its result. Test retuning and metadata reduction on short disposable copies, including names with spaces/non-ASCII characters and pre-existing output names. Close the browser while a job runs, reopen to inspect history, then deliberately stop/restart the service to confirm paused recovery. Test CPU transcription on a short clip before selecting CUDA. Test downloads separately with a media URL you are authorized to use.

Do not interpret a green dependency-presence indicator as proof of successful inference, live website support or production readiness on that machine.
