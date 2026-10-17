# FORGE V0.1 architecture

## Runtime path

Windows Start/taskbar/Send to → locally compiled `FORGE.exe` or `Open FORGE.bat` → `launcher.py` → a single `server.py` process → default browser at a loopback URL. The server selects an available port starting at 8765, records its runtime locally, and obtains a per-user file lock to prevent duplicate instances. The token is initially in the URL fragment, removed from the displayed URL and retained in that tab's session storage where allowed. A new launch authorizes a new tab after restart.

## Layout

- `forge/core.py`: SQLite store, path validation, safe naming, dependency detection and cryptographic passwords.
- `forge/models.py`: strict request/option schemas and allowed values.
- `forge/app.py`: guarded HTTP API, source registration/staging, output authorization, settings, preset handling, release metadata checks and native-pick dispatch.
- `forge/queue.py`: persistent dispatcher, process isolation, progress collection, cancellation tree, retry and shutdown behavior.
- `forge/worker.py`: fixed tool adapters, media inspection, FFmpeg, QR, optional speech/download engines, verified output publication and parent-process watchdog.
- `scripts/native_picker.py`: short-lived Tk process that returns real paths to the local backend.
- `scripts/ForgeLauncher.cs`: tiny Windows GUI launcher, compiled from included source by setup, with explicit Windows argument quoting.
- `web/`: complete front end; no build service, CDN, npm or remotely loaded UI assets.
- `tests/`: backend, integration and browser workflow checks.

The initial proposal mentioned React; the delivered V0.1 uses dependency-free browser JavaScript to avoid requiring a web build chain for this local tool collection. The HTTP contract keeps future UI replacement separate from processing logic.

## Job state machine

`queued → running → finalizing → completed`

Alternative terminal states: `cancelled`, `failed`, `interrupted`.

Cancel is available for queued/running jobs; running jobs pass through `cancelling`. Finalizing closes cancellation while file publication occurs. Retry is a new job with current source stat/settings snapshots. Dispatcher pause is not operating-system process suspension. Only one ASR worker is dispatched simultaneously; other tool workers can share the configured slots.

A job writes temporary results inside a unique `.forge-<id>.work` folder in its chosen destination, keeping publication on the same filesystem. Outputs are published only after tool-level checks. Windows rename and POSIX hard-link creation fail on an existing final name, allowing collision numbering without replacement. Multi-file transcript publication is per-file, not an all-or-nothing multi-file transaction. A manifest records each successfully published file so it can be reported after a partial failure.

## Security boundaries

The authenticated browser is trusted to request operations on files accessible to the same OS user. The app is not safe to expose to a LAN/internet, reverse proxy, untrusted local user or shared browser session. Custom FFmpeg/FFprobe paths are trusted native executables selected by the user, not sandboxed. Host and origin checking prevents ordinary foreign web pages from driving the service; tokens do not defend against compromised local software or privileged browser extensions.

No endpoint runs an arbitrary command string or user-supplied batch file. File input IDs reference sources explicitly selected/staged through the authorized API. The outputs endpoint serves only recorded results, not an arbitrary path parameter. Original batch files are reference material and remain outside the tool registry.

File path validation, extension allowlists and non-destructive naming are independent of codec safety. Media decoding/ASR libraries still parse untrusted bytes. Dependency updates require review. This build has no installer signing, model-signature verifier, comprehensive reproducible dependency hash lock, OS-level worker sandbox or formal external penetration test.

## Network behavior

- Core local tools: loopback browser traffic only after dependencies/models are present.
- Release discovery: allowlisted package metadata from PyPI; no media or filenames in requests; enabled only manually or by an opt-in daily-on-start check.
- Installers: explicit pip package requests to PyPI; direct dependency pins for core, optional version policies in their requirements files, installed snapshot written to disk.
- First ASR model: explicit user consent to Hugging Face download; otherwise local-only model loading.
- Media downloads: only jobs the user submits; extractor-dependent networking and optional copied cookie file.

## Future seams

New tools require an allowlisted schema, input format contract, preflight checks, isolated worker implementation, destination behavior and failure tests. Arbitrary script import is intentionally deferred until a review/permission model exists. New ASR engines, alignment/diarization and editorial outputs should remain separate passes, with original machine-recognized text retained and corrected content clearly distinguished.
