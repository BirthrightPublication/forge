# FORGE V0.2 validation report

**Build:** 0.2.0 · **Date:** 16 October 2026 · **Incremental base:** the supplied FORGE V0.1 ZIP

This report records executed checks and explicitly separates them from untested Windows and optional-engine behavior. It is not a security audit, transcription-accuracy benchmark, Windows certification or guarantee of compatibility with every codec or GPU.

## Results

| Check | Observed result |
|---|---|
| Python unit/API/worker/media/updater tests | **143 passed**, final run **23.69 seconds**, no skipped tests |
| Browser interface/workflow checks | **70 passed**, no uncaught JavaScript errors |
| Real HTTP service lifecycle | Cookie-authorized API access survived an immediate same-port service restart and native-token rotation |
| Full V0.1 upgrade integration | Manifest validation, real V0.2 application/QR execution, preservation and code-only rollback passed on Linux; exact candidate hash in `UPGRADE_INTEGRATION.json` |
| Python compilation | `compileall` passed for application, launcher/server and scripts |
| Browser source syntax | `node --check` passed for both `web/app.js` and `web/v2.js` |
| Uploaded originals | All **8 original files** remained byte-identical to V0.1 during the full upgrade/rollback test |

The Python total comprises 82 retained baseline tests, 34 V0.2 tests (including parametrizations), and 27 updater tests. It does not mean 143 distinct Windows end-user scenarios. Test source and logs are included. No model inference or download success is inferred from a mocked adapter contract.

## Environment and method

Linux x86-64, CPython 3.13.5, FastAPI 0.128.2, Uvicorn 0.48.0, qrcode 8.2, Pillow 12.3.0, psutil 7.2.2, pytest 9.0.2, HTTPX 0.28.1, Playwright 1.57.0, Node 22.16.0, system Chromium and FFmpeg/FFprobe. NumPy supported the baseline signal-analysis tests. Optional speech/speaker models, actual CUDA inference and authenticated remote media downloads were not exercised.

Managed Chromium in this environment rejects normal URL navigation, including localhost, with `ERR_BLOCKED_BY_ADMINISTRATOR`. No policy was changed. The browser harness loads the actual bundled HTML/CSS/JavaScript and bridges its requests to a real loopback HTTP server using a Python cookie jar. This exercises UI/worker/backend behavior through that transport. It **does not test Brave/Chrome/Firefox networking, browser cookie enforcement, native browser notifications or Windows dialogs**. A second page was created without any launcher fragment, runtime-token injection or browser storage and loaded successfully through the remembered-cookie transport. Cookie/CSRF enforcement was independently tested at the API layer.

A separate real HTTP lifecycle check exchanged a one-use launch ticket, authorized with a cookie, stopped the service, immediately restarted on the same port and retained cookie/CSRF authorization despite native-token rotation. This was Python HTTP, not browser networking. A POSIX `TIME_WAIT` restart issue found during final validation was corrected with POSIX-only address reuse; Windows reuse semantics were deliberately not enabled. Native Windows restart behavior remains untested.

## Executed coverage

### Browser authorization and request protection

Tests cover HttpOnly/SameSite=Strict/expiry cookie attributes, server-side hashed session identifiers, cross-client and cross-service persistence, stable CSRF after reopening the launcher, one-use/expired launch tickets, expired/revoked cookies, explicit browser forgetting and exclusion of the native runtime secret from application state. Host/Origin/Fetch-Site and preflight rejection from V0.1 remain covered. Browser writes without the correct CSRF header are rejected.

This is single-user loopback authorization, not a sandbox against malware running as the Windows user, an independent security assessment, or isolation from all other local services.

### Queue and workflows

Tests exercise resource-specific dispatch limits, persistent queued order, selected-job cancellation/retry, actual worker execution of retune followed by metadata reduction, dependency blocking after failure, validated graph creation before any job is inserted, collision-safe output handoff and group retry. Jobs older than the latest 500 history records remain visible while active. V0.2 update maintenance refuses a busy queue, prevents new mutations/jobs and restores the prior pause setting when cancelled.

The browser test ran an actual two-step FFmpeg workflow, verified saved workflow option snapshots survive polling, used group controls, generated an actual QR output, preserved the source image, and saved resource limits. Pause is dispatch pause; no unsupported mid-encoder checkpoint pause/resume is claimed.

### Processing and transcript outputs

Retained real-media tests verify 440-to-432 retuning at 44,100 and 48,000 samples/second within the fixture tolerance, supported metadata reduction, original hash preservation, image handling and non-overwrite output publication. New tests run media tag editing/remuxing and normalization/bit-depth processing on synthetic fixtures.

Transcript tests use constructed recognition segments to check raw companions, clean text behavior, sentence/word layout helpers, subtitle wrapping/timing targets, JSON/CSV/HTML exports, formula-marker neutralization, HTML/script escaping, anonymous-speaker overlap assignment and authenticated media Range access. These validate serialization and integration, **not real recognition, speaker detection or linguistic accuracy**.

faster-whisper and yt-dlp parameter construction is tested using mocks. Playlist ranges, subtitle flags and filename templates are checked without contacting a media website. Model-download consent and cache inventory are tested without inferring that a model download or GPU execution succeeded. Read-only diagnostics explicitly report that inference has not been tested.

### Updater and preservation

The updater tests cover semantic version selection, duplicate-version conflicts, version/manifest mismatch, modified bytes, duplicate JSON/path entries, traversal and protected paths, archive/target symlinks, invalid Python, unlisted files, dry-run behavior, idle checks beyond the displayed history window, backup integrity, automatic rollback after a failed smoke check, interactive rollback without recursive locking, and equality of the BAT's embedded Python with `scripts/update.py`.

A separate harness extracts the actual V0.1 ZIP into a fresh directory, creates prior settings/presets/history and private-environment/tool/configuration markers, and invokes the actual updater main function. It imports the updated application, runs a real queued QR worker, then invokes code rollback and opens the current database through V0.1 again. The old preset and both old/new history entries remain. All eight originals and private markers remain intact. This tests real code replacement and SQLite compatibility on the host OS, **not Windows BAT/PowerShell, NTFS ACLs or the native launcher**.

The exact tested candidate hash appears in `UPGRADE_INTEGRATION.json`. Final packaging adds the integration evidence to documentation; application and test-source bytes are checked against that candidate. A release manifest hashes every shipped application file. These checks detect corruption, not publisher authenticity; the release is not digitally signed.

## Browser layout coverage

The 70 named checks in `BROWSER_CHECKS.json` cover navigation, presets/drafts, favorites, advanced transcript controls, staged browser-file import, original-path paste, custom output fields, actual media/QR jobs, preview access, passwords/no-history, diagnostics, queue controls and overflow at 1920, 1366, 900 and 440 px widths. Screenshots are actual Linux-rendered application views, not design mockups or screenshots from the user's PC. No uncaught JavaScript errors were observed.

## Not executed here / limitations

- Windows BAT bootstrap, PowerShell installers, native C# launcher, Start/taskbar/Send-to integration, Explorer reveal and Tk dialogs.
- Native Brave/Edge/Firefox loopback navigation, browser cookie policies/extensions, private-window behavior and OS desktop notifications.
- NTFS permissions, locked files, antivirus interference, drive letters/UNC paths, power loss and every interrupted-update stage on the user's installation.
- Real speech recognition, forced alignment (not implemented), speaker-model inference, multilingual accuracy, long-form endurance and GPU performance/compatibility.
- Successful real model downloads/optional package installations and live yt-dlp website, playlist or authentication behavior.
- Comprehensive codec/malicious-input fuzzing, formal security audit, reproducible cross-platform dependency lock or signed publisher verification.

Conservative Clean is whitespace cleanup plus optional English um/uh removal, not editorial rewriting or fact-checking. Sentence timing comes from timed words/punctuation, not a separate forced-alignment model. Speaker labeling requires its separate optional environment and complete local Community-1 model; that pass is CPU-only in this release. Media metadata reduction cannot guarantee anonymity or erase identifying content within the image/audio/video. Single-pass normalization is not certified post-encode loudness measurement.

## First Windows acceptance check

Save the intact ZIP in `B:\Forge\Build`, put the standalone updater in `B:\Forge`, finish/cancel queued work and run it. Confirm V0.2 through the launcher, then open the plain bookmark in another normal tab. Test a QR job, a short disposable retune-to-metadata workflow and output reveal. Test CPU transcription on a short clip before trying GPU or speaker labels. Test downloads independently with media you are entitled to save. Keep the backup and report the exact failure text rather than deleting update markers or disabling request protections.
