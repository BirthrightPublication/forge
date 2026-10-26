# Audio Effects renderer

`forge.audio_effects` is an allowlisted FFmpeg renderer shared by previews, voice
processing, segment processing and final exports. Source files are opened read-only;
an existing output or an output resolving to the original path is rejected. The
queue's existing publication step moves the verified staged files to their final
collision-safe destination. The renderer strips source metadata from its new audio
output; audiobook metadata is applied separately by the book exporter.

## Contract

- `schema()` returns actual effect controls, ranges, defaults and processing methods.
- `validate_chain([{type, enabled, params}, ...])` returns a normalized chain.
  Unknown effects, parameters, expressions, non-finite numbers and invalid ranges
  are rejected. No user-authored FFmpeg filter strings or shell commands execute.
- `render_chain(source, output, chain, settings, region=None, bypass=False)` renders
  the ordered chain and returns input/output duration, sample rate, channel counts,
  measured sample peak/RMS levels and the exact normalized parameters.
- `process(source, stage_directory, options, settings)` returns `(files, warnings)`
  for the existing job worker. Options are `chain`, `format`, `bypass`, `region`,
  `name`, `preview` and `write_report`. Format defaults to WAV. It emits an audio file and,
  by default, a JSON processing receipt.
- `waveform(source, settings, max_points=1000)` returns at most 4096 peak bins from
  decoded 8 kHz mono playback audio. It streams into bounded memory and does not
  claim true-peak metering. Sources must have a known duration of at most 24 hours.

Region uses `{start: seconds, end: seconds}` and processes only the selected interval.
Surrounding audio is concatenated unchanged through the processing stream; lossy
exports still encode the full output. Time-changing effects can change the selected
interval's length. Voice/segment/master layers must invoke their assigned chain once;
the renderer does not implicitly apply another layer or a 432 Hz conversion.

With `preview: true`, only the selected interval is privately extracted and rendered
(at most 30 seconds; the first ten seconds when no selection is provided). The
private clip is removed afterward. The receipt records its original selection and
source duration. The same saved effects chain processes preview and export, although
analysis-based effects such as normalization analyze only the preview clip in that
mode. Duration-changing effects and reverb/delay tails can extend or shorten it.

## Actual processing

All effects render at a common 48 kHz processing rate. Supported codecs are WAV
(24-bit PCM), FLAC, MP3, OPUS and M4B (AAC/MP4), when the local FFmpeg build includes
those encoders. The renderer probes the resulting file before returning success.

Pitch/tempo uses FFmpeg's Rubber Band filter in high-quality mode. A build lacking
Rubber Band receives a clear error rather than a sample-rate pitch approximation.
Pitch is independent of tempo. Formants can either follow pitch or be preserved;
separate arbitrary formant shifting is not exposed. Pitch automation uses up to 32
ordered timestamp/semitone points, applied as discrete changes. Volume automation
interpolates its time/dB points linearly. These controls are not claimed to generate
native emotion or train a new voice.

The chain includes gain, 1/2-pole high/low-pass, band-pass, notch/hum reduction,
up to 16 parametric EQ bands, low/high shelves, measured peak normalization,
EBU R128 loudness normalization, fades, volume envelopes, stereo balance/width,
compressor, limiter, gate, downward expansion, de-esser, synthetic room reverb,
delay, chorus, flanger, phaser, tremolo, vibrato, ring modulation, soft/hard clipping
and edge-silence trimming. The graphic EQ view can use the same stored band objects
as parametric EQ. No unimplemented independent formant, impulse-response convolution
or exciter controls are advertised.

Peak normalization measures the signal at its actual position in the effects chain
before calculating gain. Fades similarly measure preceding time-changing effects.
Loudness normalization uses FFmpeg's single-pass EBU R128 filter; requested LUFS and
true-peak limits are targets, not a certification. The receipt's sample peak/RMS
measurements are separate from oversampled true peak. Reverb is a damped six-tap
synthetic room response with genuine delayed audio and an adjustable direct/wet
balance; it is not a measured acoustic impulse response. A/B comparisons should play
the original and separately rendered processed files. No browser approximation is
used for the export-equivalent preview.

## Verification and limitations

`tests/test_audio_effects_v8.py` uses test-owned sine waves, dual tones, dynamic-range
signals and an impulse with the real installed FFmpeg. It verifies octave pitch
movement, 2-second pitch-shift duration within 25 ms, separate tempo changes,
gain, measured normalization following gain, EQ response, compression, order
dependence, reverb tails, streaming edge trimming that preserves interior pauses,
region preservation, global bypass, original hashes,
all advertised filters, all five export formats, bounded waveforms and parameter
rejection. Tests distinguish FFmpeg builds lacking Rubber Band by skipping its
inference cases. Running on Linux does not establish Windows codec/build acceptance;
the same candidate must pass native Windows acceptance separately.

FORGE does not bundle FFmpeg, Rubber Band or codec binaries here. A distributor must
review the license/configuration of its chosen FFmpeg and Rubber Band builds before
redistribution; availability in the local executable does not grant redistribution
rights. User audio and generated receipts remain local.
