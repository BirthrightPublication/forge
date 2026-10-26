# Local audiobook import and assembly

`forge.speech_books.import_document(path)` imports UTF-8 TXT, Markdown, HTML,
EPUB, text-bearing PDF, SRT and VTT into editable chapters. HTML is reduced to
text without scripts, frames or external requests. EPUB follows the package
spine order without extracting archive members. Archive sizes, members, XML
entities, paths and external references are checked. PDF extraction uses
`pypdf`; scanned documents need a separate OCR workflow. Source documents remain
unchanged. Subtitle imports preserve text, not a claim of aligned TTS timing.

`process(payload, stage_dir, data)` runs inside the existing queue worker. The
API snapshots project text, options, chapters and speaker assignments when it
queues a job. This module does not construct another main `Store`, which would
incorrectly trigger service-startup interruption recovery in a worker process.

## Incremental generation and interruption

Each parsed spoken segment is synthesized independently through the selected
isolated speech adapter. A content-addressed cache includes the spoken text,
resolved immutable model revision, voice/blend/options, seed and the actual hash
of any permitted voice-reference audio. Segments are normalized to 24 kHz mono
16-bit PCM and verified before atomic cache publication. Reuse checks the audio
hash and valid PCM structure. Editing one sentence reuses unchanged sentences.
Completed verified cache entries survive cancellation, service interruption,
retry and code updates. A partial inference result is never a reusable segment.

The raw synthesis cache precedes acoustic processing. Per-voice, expression and
per-segment processing are applied once before assembly. The master chain is
applied once per exported production. New processing settings can reuse the
original speech audio. A separate `_original.wav` output preserves the complete
unprocessed assembly for A/B comparison when processing is applied. Kokoro expression fallbacks use explicitly labeled DSP;
they are not native emotional synthesis. Unsupported vocal events fail rather
than being silently omitted or reading editor markup aloud.

The existing parser resolves manual and automatic pauses. Silence is created
using exact sample counts. Chapter transitions extend automatic pauses instead
of adding another pause; explicit manual transitions take precedence. These are
generated audio boundaries, not word-level forced alignment.

## Export contract

Supported encoded exports are WAV, FLAC, MP3, OPUS and M4B when the configured
FFmpeg provides their encoders. All outputs are probed for playable audio.
M4B receives chapter markers, and standard title/artist/album/copyright/
description fields are passed as discrete metadata arguments. MP3, FLAC and
M4B support validated local PNG/JPEG covers. Chapter-only or multiple selected
chapter generation preserves document order. Separate chapter files and combined
audio are independent export options.

`assemble_only` exports from verified cached speech and refuses missing or
changed segments without invoking model inference. A duration-changing master
chain is rejected for chapterized M4B, because original chapter offsets would
otherwise be incorrect. Apply tempo changes to individual segments instead.
No automatic 432 Hz retuning is performed.

Outputs first go to the ordinary job staging area; the existing worker publishes
them without overwriting an existing file. A private JSON receipt records model,
project revision, cache identities and the three processing layers. The cache and
project audio are local user data and are excluded from installation assets.

## Validation and scope

`tests/test_speech_books.py` includes 40 checks with real document imports,
actual PDF extraction, safe EPUB order/rejection cases, real FFmpeg exports,
M4B chapter/metadata inspection, exact inserted silence, corruption rejection,
cache-only export, selected chapters, bounded long-prose chunks, speaker assignment,
saved whole-segment selection effects and unchanged sentence reuse.

Those assembly/cache tests inject deterministic audio in place of a speech
model. They establish codec and orchestration behavior; they do not establish
Kokoro/Ming model quality, native Windows model inference or physical GPU
acceptance. Model inference evidence is recorded separately by the engine tests.

Current limitations: cache-only export still requires the selected model's
verification receipt to resolve the immutable identity; keep that model
registered when exporting. PDF visual layout, OCR, inferred character identity,
word alignment, and simultaneous separated speaker tracks are not inferred by
this module. Different speakers are generated in order and combined into the
production track.
