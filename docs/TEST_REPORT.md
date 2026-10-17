# FORGE V0.5 validation report

Validated 16 October 2026 (America/Los_Angeles) in the Linux cloud environment. V0.5 is a release candidate on the development branch; review/merge and a stable V0.5 GitHub release remain pending. V0.4 is the published stable release.

## Results

- **478 Python tests passed**, including all retained V0.4 regressions and 126 added media-privacy/API/pipeline checks. One upstream Starlette/AnyIO deprecation warning; no test failures.
- **34 watermark tests passed** again after the measured-progress callback integration was finalized; this is part of the 478-test suite, not 34 additional distinct tests.
- **324 real Chromium checks passed**: Studio 127, Media Privacy 88, Black Scripting 26, generators 83. No JavaScript errors. Desktop, keyboard and 390-pixel mobile layouts exercised; 22 reviewed screenshots are in `screenshots/v0.5/`.
- Real incremental upgrades and code rollback passed from **V0.1, V0.2, V0.3 and V0.4**. Existing environments, tools, originals, models, settings, preset data and history remain preserved. V0.4 Black Scripting definitions, generator presets and Auto concurrency settings survive. The candidate performs a real privacy export, checks source and pixel preservation, verifies metadata/report output, and retains job history and outputs through rollback.
- JavaScript syntax and Python test-harness compilation passed. Source/package manifests are checked with the actual updater.

The recorded source-candidate upgrades are in `UPGRADE_INTEGRATION.json`. Documentation is finalized after those runs; the final ZIP's exact SHA-256 and repeated artifact upgrade results are supplied as `FORGE-v0.5-VALIDATION.json` beside the ZIP. An archive cannot include a checksum of itself.

## What was exercised

Watermark tests encode and decode real video/audio: text and logo marks, repeated placements, timing/fades/motion, actual five-second previews, audible inserted clips, source preservation, audio-only video-packet copying, and CPU fallback. The NVIDIA path is capability/fallback tested; physical NVIDIA encoding is not claimed. Progress uses FFmpeg output rather than invented percentages.

Fingerprint tests calculate actual Chromaprint where available, local spectral profiles, video hashes, frame/spectrogram previews, portable references, batch comparisons and changed/silent inputs. Similarity is descriptor agreement, not a probability or proprietary-service recognition prediction. The tests exercise bounded local decoding and deny external media protocols.

Tracking and privacy tests inspect and remove real metadata, preserve or remove selected streams/subtitles/attachments/artwork/chapters, compare actual before/after fields, verify collision-safe report paths, and retain source bytes. Reports are optional, HTML is escaped, disabled reports are not silently retained, and inspection-only script nodes pass unchanged media forward while retaining the immutable original identity.

C2PA uses the optional pinned `c2pa-python==0.38.0` reader on actual plain, signed and tampered media. Tests distinguish signature validity, media binding and issuer trust, and verify that a remote-manifest fixture makes no HTTP request. Missing credentials and unsupported formats are separate outcomes.

Queue and Black Scripting checks exercise all seven new operations, immutable source/asset snapshots, dependencies, cancellation/retry, reportless chains, original-source verification and existing resource admission. CPU/disk work and GPU encoder estimates remain distinct; concurrency never exceeds five.

## Public-source review

Historical and current source plus documentation images were scanned and inspected for credentials, identifying metadata and private files. An unused machine-identifying Windows shortcut was removed from unpublished history and rebuilt release packages. Personal historical Git contact metadata was replaced with a neutral development identity; exact originals are privately archived. The existing MIT terms were retained with owner approval. No model weights, media libraries, private inputs, databases or runtime tokens are distributed.

## Limits and outstanding acceptance

- No native Windows 11 BAT/PowerShell/Explorer/NTFS/antivirus acceptance was run in this Linux environment. The updater's Python path and preservation/rollback behavior were exercised.
- No physical NVIDIA/CUDA encode or actual optional speech/speaker model inference was run. No performance promise follows from hardware detection.
- Browser playback depends on installed browser codecs. Edge, Brave and Firefox are not separately certified.
- No external recognition database, inaudible watermarking, arbitrary steganography detection, watermark-removal guarantee or anonymity guarantee is implemented.
- Offline provenance inspection does not fetch manifests, issuer trust lists or revocation status. Missing/untrusted/unsupported states require interpretation.
- Metadata deletion can leave required technical tags; re-encoding can change quality, color and provenance. Standard stream-copy exports reject incompatible retained streams instead of silently losing them.
- Windows shortcut omission affects only new source packages; installed user-owned original scripts and shortcuts remain preserved.

See `V05_RELEASE.md` for exact feature boundaries, `COMMERCIAL_DEPENDENCIES.md` for dependency terms, `BUILD.md` for reproducible commands and `UPDATE_GUIDE.md` for installation/recovery.
