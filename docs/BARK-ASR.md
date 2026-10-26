# Underdog Bark recognition in FORGE V0.8

This adapter is for Conway Research's **Underdog Bark 3.0 speech recognition**
family. It does not use Suno Bark text-to-audio. Whisper and its existing model
environment remain independent.

## Model catalog and capabilities

| Model ID | Publisher repository | Output |
|---|---|---|
| `bark-3.0` | `ConwayResearch/Underdog-Bark-3.0` | English text |
| `bark-3.0-plus` | `ConwayResearch/Underdog-Bark-3.0-Plus` | English text; quality evaluation pending |
| `bark-3.0-pro` | `ConwayResearch/Underdog-Bark-3.0-Pro` | English text; quality evaluation pending |
| `bark-3.0-clean` | `ConwayResearch/Underdog-Bark-3.0-Clean` | Explicitly non-verbatim English text |

The adapter supports a separate CPU or CUDA 12.8 PyTorch installation. A supported
code path is not a claim that native Windows/CUDA inference has been tested.
Actual RAM and VRAM usage are measured by the runtime where available; compressed
download bytes are not memory requirements. GPU memory allocation is not overall
GPU utilization.

Translation, Whisper vocabulary prompts, speaker labeling and timing output are
rejected for Bark. TXT and JSON are offered. JSON has an empty `segments` list,
`timing: unavailable` and separately labeled `audio_window` boundaries. These are
chunking boundaries, not recognized speech/subtitle times. Use Whisper for timed,
translated or speaker-labeled transcripts. No optional alignment engine has been
validated and none is implied by this adapter.

## Installation and offline execution

Installation requires explicit consent. Models are never all downloaded at startup.
The selected model repository is resolved to a full immutable publisher commit.
Only approved data filenames (`model.bark`, safetensors, JSON/tokenizer assets,
LICENSE/NOTICE) are permitted; downloaded Python, pickle and binary extensions are
excluded. Every downloaded file is checked against the immutable publisher LFS
SHA-256 or Git blob SHA-1 and receives a local SHA-256 receipt. A selected model
receipt is written only after model, processor and environment validation.

The stock NVIDIA processor is independently pinned to
`541d1f99c6b0c3cd0b11a95167540bb8edefd82b`. Its files are downloaded and checked
with the same rules. Inference uses local paths with offline flags and
`trust_remote_code=False`; it makes no model/processor fetch at execution time.
The installed runtime is under `data/environments/bark/<unique-id>` and models
under `data/models/bark/<model>/<revision>`. Existing FORGE/faster-whisper
environments are never modified. Old model revisions and runtime directories are
retained when a different selection is installed.

Dependencies are pinned in `scripts/requirements-bark.txt`; PyTorch 2.8.0 is
installed separately from the approved CPU or CUDA 12.8 wheel index. Pip resolves
transitive dependencies and checks compatibility. This is not a fully hash-locked
wheel supply chain. License review of those transitive distributions remains
necessary before bundling their wheels into a public release. FORGE's source ZIP
does not contain model weights, environments or model caches.

## Long audio, exports and benchmarks

FFmpeg decodes to disk-backed mono 16 kHz PCM. Bounded 5–30 second chunks cut near
the quietest 100 ms frame in the last three seconds. Zero overlap is the default
and preserves exact ordered PCM coverage. Optional overlap up to two seconds uses
exact multiword suffix/prefix deduplication only. Original per-chunk recognition
is retained in JSON for review; this heuristic is not guaranteed alignment and
can merge an intentionally repeated phrase. Single/two-word boundaries are never
automatically deleted.

No output is published if a chunk is missing or out of sequence. Clean output is
named `_non-verbatim` and labeled in JSON. Input media is preserved. Temporary PCM
is removed when the worker exits normally; process-kill cleanup follows FORGE's
existing staging lifecycle. Runtime failure details remain in local private logs.

`word_error_rate(reference, hypothesis)` implements case/punctuation-sensitive
Levenshtein WER. References must be verified; an empty reference yields no useful
nonempty-hypothesis score. Exports report processing duration, peak process memory,
actual selected device and peak CUDA-allocated memory when available. Comparable
accuracy/speed conclusions require the same recordings, normalization and device
settings for Whisper and Bark. No owner-machine comparison has been measured.

## Validation status

The development environment blocks the official Hugging Face model download host
and Underdog site with HTTP 403. No Bark weights were downloaded, and no actual
model inference, quality ranking, native Windows model run or physical GPU run is
claimed. Contract tests exercise synthetic artifact verification/corruption,
strict capability failures, ordering, deduplication and WER. Real FFmpeg tests
verify long-audio coverage, exact input preservation and declared overlap windows.
These tests do not substitute for required V0.8 model/Windows acceptance.

## Attribution and license

Original model and compression format: Conway Research / Underdog,
<https://huggingface.co/ConwayResearch/Underdog-Bark-3.0> and family repositories.
The family derives from NVIDIA Parakeet TDT 0.6B v3, © NVIDIA Corporation,
and Moondream Parakeet Ultra, © Moondream. Their published integrations identify
the model/runtime as Creative Commons Attribution 4.0:
<https://creativecommons.org/licenses/by/4.0/>.

The numeric decompression logic in `scripts/bark_runtime.py` is adapted from the
published Conway Research Bark loader through VoiceFlow's public implementation:
<https://github.com/arunmauryaz/VoiceFlow/blob/main/VoiceFlow/LocalRuntime/bark_loader.py>.
That implementation identifies its source as Underdog-Bark-3.0-Clean revision
`6d34ed8639b2b9a33e764acf06748519bbb950cf`. FORGE changes: bounded headers, tensor
sizes and LZMA decompression; strict format failures; application-owned offline
runtime; separately pinned local processor; no downloaded Python execution; no
pickle loading or decoded pickle cache. These adapted portions retain the CC BY
4.0 attribution obligation; FORGE's MIT notice does not relicense model weights
or the adapted loader portions. No endorsement is implied.

Retain the exact selected publisher licensing documents when installing or
redistributing models. A native Windows publisher probe confirmed that all four
official repositories publish a revision-verified `README.md` model card and
`NOTICE`, with model metadata declaring `cc-by-4.0`; they currently do not publish
a standalone `LICENSE` or `LICENSE.txt`. Their NOTICE explicitly references the
CC BY 4.0 URL and retains the NVIDIA/Moondream attribution and Conway changes.

The installer preserves any provided LICENSE file. For the official NOTICE-only
layout, it requires metadata declaring exactly `cc-by-4.0`, the exact
`https://creativecommons.org/licenses/by/4.0/` URL in the verified NOTICE, and a
verified model card. Artifact hashes and the actual licensing source are recorded
in the installation receipt. No publisher LICENSE file or license grant is
fabricated. A missing/modified notice or changed metadata license stops
installation for review. This evidence concerns the selected model's published
declaration; review the separate runtime/dependency notices as well before
describing a redistributed bundle as approved.
