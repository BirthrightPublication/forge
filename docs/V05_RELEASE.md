# FORGE V0.5 — Media Privacy & Watermark Studio

**Release candidate status:** source validation passed; final artifact checks, merge review and publication are recorded separately. The Python and browser regression work has produced passing suites; final release-package and upgrade/rollback evidence must be recorded before calling V0.5 released. No stable V0.5 GitHub Release, native Windows acceptance or NVIDIA hardware validation is asserted by this document.

V0.5 extends the existing local production Studio with seven tools. The aim is to expose measurable identifying characteristics and give the user explicit control over processing and retained evidence. Removing selected metadata, changing pixels, or changing a similarity score does not prove that a file is anonymous or unrecognizable to other systems.

## Start with a local source

1. Open FORGE through the Windows launcher, then choose **Media Privacy** in the sidebar. Use its internal tool navigation to switch workspaces.
2. Add existing files with Native browse or pasted absolute paths. Browser drops use a disclosed local staging copy and retain the existing output-destination rules.
3. Set the tool's options and report preferences in the compact settings tabs. Choose the output directory and inspect **Processing plan** for source-specific changes.
4. Use **Start** to submit and enable dispatch, or **Add to Queue** to retain the current dispatch state. Job cancellation, retry, dependencies, worker limits and output collision handling use the existing queue.
5. Open the completed job's outputs. **View evidence** renders retained JSON reports; standalone HTML reports can be opened locally. No analysis report can be reopened after completion if its retention was disabled.

All transformations write new outputs. Inspection does not modify the source. Media, comparison files, retained references and watermark assets are checked against their queued file-size/mtime snapshots before execution. Queue records still contain their usual filenames, paths, settings and brief outcomes even when private report files are disabled.

## The seven workspaces

### Watermark Studio

Choose **Video**, **Audio**, or **Both**. Visible marks accept custom text or a local still image/logo. Text controls include available local sans/serif/mono fonts, local TTF/OTF files, size, color, opacity, scale, rotation, alignment and margins. Full-Frame Diagonal repeats the mark across the frame; Perimeter repeats around its edges; Bottom Center and Custom position a single mark. Horizontal, vertical or bouncing movement is configurable for Bottom Center/Custom, with speed and normalized boundaries. Repeated full-frame/perimeter patterns are stationary. Start/end times, appearance intervals, visible durations and opacity transitions are optional.

For an audible ownership identifier, select a recorded announcement or another audio clip you own or are authorized to use. Set the insertion timestamps, optional repeat interval, volume and fades. Clips are limited to five minutes, insertion count to 100, and insertions must not overlap. Longer timestamps use streamed silence and clip concatenation rather than buffers proportional to the delay. No text-to-speech engine is installed by this tool. Robust inaudible watermarking remains outside this implementation.

**Encode preview** produces an actual encoded excerpt from the beginning, capped at five seconds. It uses the export pipeline, so a visible mark is present in the preview's pixels rather than applied only by the browser player. A still-preview API extracts a frame from that encoded result. Later scheduled marks may fall outside the excerpt. Temporary preview playback clears on navigation or after five minutes; a full queued export is required for retained media.

Visible watermarks export MP4/MKV with H.264, 8-bit YUV420 video. The conversion can alter HDR/color fidelity and requires re-encoding. Compatible NVIDIA hardware encoding is attempted when detected; a failed or unavailable NVIDIA encoder falls back to CPU and the job records that choice. Hardware detection is not a guarantee of encoding success or performance. Audio-only watermarking can keep the original primary video packets in MP4/MKV while encoding the new audio mix. WAV, FLAC, MP3 and M4A are available for audio output. The mix uses a peak limiter. Only primary video/audio are retained; alternate streams, subtitles, attachments, descriptive tags and chapters are excluded. FFprobe checks the exported stream inventory and duration before publication.

Video inputs are limited to a known duration of at most 24 hours and dimensions no larger than 33.2 million pixels. Fonts, logo images and audio assets have size/type limits; arbitrary FFmpeg filters and shell commands are not accepted.

### Audio Fingerprint Lab

Add the baseline first and other recordings next. Each subsequent recording or selected local reference is compared with that first input; the lab does not silently query an online music-recognition service. It analyzes the first audio stream and the configured initial window: 120 seconds by default, configurable from 15 to 600 seconds.

The built-in FORGE spectral profile uses an 8 kHz mono decode, time-varying spectral descriptors and decoded-signal evidence. Reports include frequency/spectrogram views and codec, sample-rate, channel and duration information. Its spectral views cover 0–4 kHz and do not represent higher frequencies or stereo spatial information. If the local FFmpeg build exposes its Chromaprint muxer, the lab also calculates Chromaprint algorithm 1; otherwise that capability is explicitly unavailable and the built-in method remains usable.

Similarity measures agreement between these descriptors. It is not a probability that recordings are identical, a calibrated confidence interval, or a forecast for a proprietary recognition system. Silence, tones, repeated patterns, timing shifts, edits and pitch changes can confound results. Fingerprints derive from the signal itself; metadata removal does not erase those characteristics.

### Video Fingerprint Lab

Add a baseline video first, followed by comparison versions. The default analysis samples 12 frames over the initial 300 seconds. Select 2–48 frames and an initial window of 1–1,800 seconds, or supply up to 48 explicit timestamps within that range. The decoder uses the nearest available frame at or after each requested position.

Reports show sampled frame previews, pHash/dHash and color-based descriptor agreement, sampled timeline measurements and near-matching sampled sections. Resolution, aspect ratio, frame rate and available color/pixel-format fields are included. An overall comparison pairs relative sample positions; additional best sampled-frame matches aid shifted-section review. Unsampled frames are not analyzed, and a matching frame does not establish an entire-video match. Simple, dark, static or repeated scenes can produce coincidental agreement.

### Media Tracking Auditor

Inspect supported local audio/video and JPEG/PNG/WebP media without changing it. The inspector combines existing metadata capabilities with categorized findings for location, device, software, timestamps, author, comments and other descriptive fields. It inventories available audio/video streams, subtitles, attachments, chapters, supported embedded previews and technical characteristics. Bounded embedded XMP parsing rejects declarations/entities.

Provenance inspection is enabled by default when supported; fingerprint inclusion is optional. Findings identify their method and evidence. Unsupported or failed inspections remain **unsupported** or **unknown**, rather than being reported as clean. Parser-visible fields do not cover every hidden payload, invisible watermark, filesystem record or identifying feature in the visible/audible content.

### Media Privacy Exporter

| Policy | Supported descriptive metadata | Subtitles, artwork, attachments, chapters | Data streams |
|---|---|---|---|
| Standard Privacy | Remove | Retain where the chosen container supports them | Remove |
| Advanced Privacy | Remove | Remove | Remove |
| Custom | Remove, retain, or remove named media tags | Individual retention controls | Explicit retention control |

The source-specific plan lists selected/removed streams, output container, processing operations and warnings. With re-encoding off, compatible media streams are copied. Unsupported stream/container combinations fail with an explanation rather than being silently discarded or represented as a successful lossless operation. Explicit re-encoding supports MKV, MP4, FLAC and WAV; video becomes H.264 and audio uses the selected container's supported codec. Review the plan for quality or codec changes.

Still images retain their source format. Animated image exports and selective image-tag editing are unsupported. Orientation may need to be applied to pixels, potentially re-encoding an image. Advanced image processing also removes supported ICC/color-profile metadata and can change appearance; Standard/Custom reduction retains compatible color-rendering fields.

Every export undergoes actual output inspection even when private report saving is off. Container editing can remove or invalidate signed provenance. Review exported credentials and remaining metadata rather than inferring their state from the selected preset. Visible content, audible speech, filenames and recognizable signal/frame characteristics remain unless an implemented operation actually changes them.

### Content Credentials Inspector

The optional local reader inspects embedded C2PA manifests and available assertions. It reports **signature integrity**, **issuer trust**, and **binding to the selected media** separately. Positive claims use explicit SDK validation evidence; an empty error list alone is not treated as proof of validity.

The supported dependency is `c2pa-python==0.38.0`. Missing or different SDK versions yield an explicit unsupported capability. Remote manifest retrieval, online revocation checks, remote identity resolution and trust-list refresh are disabled. The parser uses bundled/local trust configuration, so online changes are not reflected. It reads the explicitly selected media rather than automatically loading a sibling `.c2pa` sidecar. Large results and parser execution have bounds.

No manifest detection does not prove absence of identifying data or tracking. A valid signature does not establish issuer trust, and a signed claim is not independent verification of its content. This tool does not create, sign or forge credentials, nor certify the truth of asserted identities.

### Privacy Verification Lab

Select the original first and processed copies after it. Verification compares actual media metadata, stream/codec inventories, dimensions and available provenance evidence. Optional audio/video comparisons describe remaining similarity under the supported local methods. Reports distinguish removed parser-visible fields, retained fields and new/changed output fields, plus inspection errors and unsupported checks.

In Black Scripting, a verification node compares the chain's original with the current transformed media. Recorded upstream operations describe FORGE dependency history, not an independently inferred forensic history. Report paths are updated to the actual final output filename after collision-safe publication. Equal codec names do not establish unchanged quality, and a field absent from one parser's output does not establish universal removal.

## Report and reference retention

| Tool | Default retained evidence | How to change it |
|---|---|---|
| Watermark Studio | Encoded media; job warnings and outcome | Temporary previews are separate and cleaned up; full exports are explicit jobs. |
| Audio / Video Fingerprint Lab | JSON and HTML analysis reports | Disable either report format; **Retain reference** is separately off by default. |
| Media Tracking Auditor / Content Credentials Inspector | JSON and HTML inspection reports | Disable report saving or choose report formats. |
| Media Privacy Exporter | New media; private report saving is off | Enable reports and select JSON/HTML; verification runs regardless. |
| Privacy Verification Lab | Brief job outcome; private report saving is off | Enable JSON/HTML reports to retain detailed comparison evidence. |

Reports can include original metadata, filenames, local output locations, spectral evidence and recognizable frame previews. JSON/HTML report generation does not upload those values. With all report/reference options off, detailed analysis files are not retained; the normal queue history still records the job and brief findings. Removing a queue record does not delete already published output files.

Reusable references are explicitly exported as `.forge-fingerprint.json` files, separate from ordinary reports. To use one, open an Audio/Video Fingerprint Lab and expand **Compare retained local references**, then browse or paste the local JSON path. Only selected references are read. A comparison accepts at most 20 media/reference records in total; references must use the supported FORGE schema and matching media kind. The browser/API accepts reference files up to 2 MB. There is no automatic local fingerprint index, hidden reference database, remote lookup or background media scan. Imported descriptors are not cryptographically authenticated associations with a particular recording.

## Optional installation and local operation

Core media functions use existing FFmpeg/FFprobe and Pillow. They do not require an NVIDIA GPU or the optional C2PA reader. Chromaprint is used only when already compiled into the selected FFmpeg; FORGE does not silently replace that binary or download a fingerprint service.

For the optional C2PA reader:

1. Finish/cancel active work and stop FORGE using `Stop FORGE.bat`.
2. Run `scripts\Install Media Privacy.bat` from the existing installation.
3. Read the native confirmation. Approval installs the pinned package into FORGE's existing `.venv` from PyPI, with core dependency constraints and a dependency check. No media is sent.
4. Reopen FORGE and inspect **Local inspection capabilities**. A failed/unsupported optional install does not disable the rest of the Studio.

`scripts/requirements-media-privacy.txt` pins the supported SDK. The updater itself installs no dependencies, binaries or model weights. Package acquisition needs internet only when explicitly approved; supported media analysis remains local. Local decoders restrict input protocols and do not accept supplied shell/filter commands. This remains a trusted single-user loopback application, not a remote service or a sandbox against other applications with the user's privileges.

See [the commercial dependency review](COMMERCIAL_DEPENDENCIES.md) for the optional SDK, Chromaprint, FFmpeg build variants and redistribution boundaries. FORGE's existing MIT grant is unchanged; it does not grant ownership or redistribution rights to selected media, fonts, logos, third-party binaries or model weights.

## Queue, Black Scripting and compatibility

All seven tools use the established queue, settings presets, output destinations and status handling. Visible Auto/NVIDIA watermark jobs reserve the GPU lane and a labeled encoder-memory estimate, alongside CPU/disk capacity; CPU fallback remains available. Analysis and privacy processing use their corresponding CPU/disk budgets. No fabricated memory, speed or completion estimate is displayed when an adapter lacks the measurement. Retry starts again from the beginning.

Black Scripting remains a linear, versioned workbench with up to 24 nodes. Example chains are:

```text
Source → Tracking Audit → Metadata Hyper Cleaner → Privacy Export → Verification
Source → Watermark Studio → Privacy Verification → Exported media
```

Inspection nodes pass their input media onward without copying it into a report-shaped substitute. Transform nodes pass the actual published media path; reports are side outputs. Verification uses the original/current media relationship. Transcription remains a final operation because its outputs are text. Branches, arbitrary scripts, shell steps and remote processing are not added.

Existing V0.4 tools and saved scripts remain compatible. New tool records and option snapshots use existing persistent storage; existing presets, history, model caches, local environments, outputs and originals are preserved. A code rollback keeps current user data; V0.5-specific scripts/options may require a V0.5 application to run and are not silently rewritten into older definitions.

## Windows update layout

The installation layout remains **`B:\Forge`**, with intact release ZIPs in **`B:\Forge\Build`**, backups in **`B:\Forge\Backups`**, and the standalone **`B:\Forge\Update FORGE.bat`**. When a validated `FORGE-v0.5.zip` is supplied, use that existing updater flow. Finish/cancel queued work first; keep one active installation and do not extract a second copy beside it. The updater preserves the active environment and user data, validates package hashes/compatibility and retains rollback recovery. Package hashes detect corruption; they do not constitute a publisher signature.

See [the update guide](UPDATE_GUIDE.md) for the established procedure. Promote the candidate status at the top of this document only when the corresponding validation, review and publication steps are complete.

## Validation and remaining acceptance

V0.5 final artifact checks are pending. The focused suite includes real local media processing, decoded pixel/audio checks, fingerprint comparisons, metadata/provenance fixtures, report retention, pipeline handoff and resource-boundary tests. Final results belong in `TEST_REPORT.md`, `PYTHON_TEST_RESULTS.txt`, `BROWSER_CHECKS.json` and `UPGRADE_INTEGRATION.json`, with an explicit V0.5 version. Do not reuse V0.4 result counts as V0.5 evidence.

The exact previous README, changelog, dependency review, V0.4 release notes, root validation reports and V0.4 screenshots are copied under [`history/v0.4`](history/v0.4/README.md). `ARCHIVE_MANIFEST.json` identifies their source commit and SHA-256 hashes. Earlier history remains intact.

Native Windows 11 launcher/installer/Explorer behavior, additional browser acceptance, actual NVIDIA encode/fallback performance, optional speech/speaker models and live website downloads remain target-machine acceptance checks unless separately documented. Exact matching to previously referenced visual assets cannot be claimed when those assets are absent. Unknown forensic watermark detection, guaranteed platform-matching evasion, complete anonymity, robust inaudible watermarking, online credential trust refresh and production billing/licensing are outside the implemented capabilities.
