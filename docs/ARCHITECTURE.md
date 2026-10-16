# FORGE V0.3 architecture

The working V0.1 architecture is retained: Python/FastAPI on exact IPv4 loopback, dependency-free browser JavaScript, SQLite persistence, isolated worker processes and a Windows launcher. There is no React migration or frontend build requirement.

## V0.3 additions

`forge/update_manager.py` exposes authenticated, local-only inventory and fixed Windows console launch endpoints. Inventory shares archive, target and backup validation with `scripts/update.py`. It reads but never imports or executes release contents. No archive/executable path is accepted from the browser. Opening a console requires CSRF and explicit consent; active jobs, maintenance, interrupted updates and duplicate consoles are refused. The console supplies final confirmation and revalidates disk/queue state.

`web/v3.js` extends the existing Environment page; package scanning is manual and separate from the existing PyPI metadata feature. No frontend framework, dependency migration, new external service or background scanner was introduced.

Native maintenance preparation persists the original queue pause preference in SQLite before freezing admission. The updater also writes a durable, installation/data-specific marker before stopping older runtimes. Pending journals protect code replacement and manual rollback; recovery verifies backups, takes the service lock, restores code/pause state, and removes markers only after recovery succeeds. Dry runs avoid even lock-file writes. These measures reduce crash risk; they do not certify power-loss behavior on NTFS.

## Components

- `launcher.py`, `server.py`: single-user runtime, stable port, native-process launch, short-lived browser tickets and startup/update interlocks.
- `forge/auth.py`: persistent browser authorization. Cookie values are random; only their SHA-256 digests are stored. CSRF secrets are independent. Tickets are one-use, in-memory, 120 seconds. Browser records expire after 90 days; at most 20 are retained without browser fingerprinting.
- `forge/core.py`: paths, registration, job/history store, preferences, known tools and dependency discovery. The V0.1 `jobs` table remains unchanged; additive `job_meta` stores order, batch/dependency/resource/timing data.
- `forge/app.py`, `forge/v2_api.py`: authenticated fixed-function API, native file selection, browser staging, output serving, presets/workflows, environment actions and maintenance freezing. No route accepts an unrestricted shell command.
- `forge/queue.py`: total/resource caps, dependency resolution, worker lifecycle, cancellation, new-job retry, graph retry and queue order. Running work is not preempted by reprioritization.
- `forge/worker.py`: job-owned staging, safe argument-list native calls, original-source stat checks, actual media processing, output validation/publication and progress events.
- `forge/transcripts.py`: raw/clean exports, review flags, sentence/caption derivation, formula-safe CSV and standalone offline HTML.
- `forge/speaker_labels.py`, `scripts/diarize.py`: optional separate CPU speaker pipeline with a local trusted model and explicit offline/telemetry settings.
- `scripts/diagnostics.py`: isolated read-only capability probe; no driver/package changes.
- `scripts/update.py`, `Update FORGE.bat`: offline ZIP updater with a protected-path allowlist, file hashes, code backup, SQLite snapshot, journal, process locks and import smoke test. The BAT embeds the same standard-library source for the V0.1 transition.
- `web/app.js`, `web/v2.js`, `web/style.css`: existing Studio rendering and V0.2 controls. No external scripts, fonts, analytics, CSS or image resources are required.

## Security boundaries

Only `127.0.0.1` is bound. Host and Origin must match the configured origin; cross-site fetches/preflights are denied. Cookie-authorized mutations require a matching CSRF header. Native launcher/updater calls use the private runtime token; browser JavaScript does not persist it. Public static files are allowlisted. Source-media streaming accepts a known job ID, not an arbitrary path parameter.

The local HTTP cookie intentionally does not set Secure, because this application uses HTTP loopback rather than a managed local HTTPS certificate. HttpOnly and SameSite are not isolation from other software executing as the same user or every local HTTP service. Loopback API defense is not a claim of resistance to a compromised Windows account.

The local build ZIP is executable application code. Embedded hashes are integrity checks, not publisher signatures. Backup data can include private source paths and processing history. Atomic replacement and a recovery journal reduce failure exposure but are not a proof of crash/power-loss safety on every filesystem.

## Preservation and compatibility

Release updates replace allowlisted application files only. Model caches, Python environments, user tools, persistent data, original inputs and completed outputs are not in release manifests. Existing archived original scripts are never replaced. Queue worker payloads from V0.1 receive V0.2 defaults on execution. The unchanged jobs table permits older code to read baseline history after code rollback; newer option combinations still may need adjustment in an older UI.

Browser drops are explicitly staging copies; native paths remain direct references. Workflow dependencies route actual successful outputs. A child never silently falls back to the original input when its parent fails. Completed intermediate files are retained rather than deleted behind the user's back.

## Remaining boundaries

No arbitrary script importer, cloud speech service, online terminology rewriting, semantic editorial model, WhisperX forced alignment, remote/LAN Studio mode, automatic driver setup or signed internet update channel is implemented. Package/version checks and explicit installers are distinct from offline application ZIP updates. Native Windows integration and real optional ASR/speaker/GPU/site execution still need acceptance on the target PC.
