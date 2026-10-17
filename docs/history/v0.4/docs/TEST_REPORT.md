# FORGE V0.4 validation report

Target: Windows 11 local browser application. Validation host: Linux x86-64, Python 3.12.14, system Chromium and FFmpeg. Prior V0.3 reports and its superseded browser harness are archived under `docs/history/v0.3`; their counts are not included below.

## Executed results

| Check | Result |
|---|---|
| Final Python unit/API/worker/media/updater suite | **352 passed**, no skips, 24.68 seconds |
| Direct Chromium browser checks | **207 passed**: Studio 98, Black Scripting 26, generators 83; zero uncaught JavaScript errors |
| Real worker behavior | Concurrent QR/FFmpeg work, retune→metadata chain, dependency routing, cancellation/retry, source/output preservation passed |
| Metadata integration | JPEG/PNG/WebP pixel preservation, orientation handling, embedded EXIF thumbnail removal, real FFmpeg subtitle/attached-cover removal and audio preservation passed |
| Independent QR decoding | PNG and rasterized SVG decoded with ZXing for text, URL, contact and Wi-Fi, including Unicode, escaping, colors and quiet zones |
| Upgrade and code rollback | V0.1→V0.4, V0.2→V0.4, V0.3→V0.4 passed with real candidate API and queued QR execution |
| Preservation per upgrade path | All 8 original scripts, 10 private file markers, prior settings/presets/history and outputs preserved; original code bytes restored on rollback |
| V0.4 compatibility after upgrade | Eleven-tool registry, offline grants, legacy-script projection and memory-only generators worked; new script/config-preset records survived rollback |
| Source/dependencies | Python compilation, all seven JS syntax checks, Git whitespace and pip consistency passed |
| Cloud startup | V0.4 authenticated API, eleven-tool registry and real QR output passed in the prepared environment |

One upstream Starlette/AnyIO deprecation warning remains. Core runtime dependency pins are unchanged. `zxing-cpp` is a development-only independent test decoder; it is not a production QR dependency.

## Behavioral coverage

Queue checks cover manual/Auto ceilings, CPU/GPU/network/disk constraints, unknown telemetry, measured VRAM with conservative budgets, ambiguous device mapping, destination-volume space, dependency order, concurrent real workers, restart persistence, failure/cancellation/retry and bulk record removal without physical deletion. Old active and failed records remain visible beyond the 500-record terminal history window. Windows association opening is tested with mocks for media/plain transcripts and rejects executable/active-document/symlink escapes. These are simulated Windows/GPU cases, not evidence of native execution or inference performance.

Black Scripting tests cover initially empty workspaces, versioned nodes/connections, all editing actions, immutable preset snapshots, save/rename/duplicate/import/export, legacy non-mutating projection, malformed/branched/cyclic definitions, preview fingerprints, disabled nodes and real retune→metadata processing. Existing legacy workflow tests still pass.

Generator tests cover bounded policies, required alphabets/exclusions, secure choices, synthetic country formats, reserved-domain emails, formula-safe exports, presets, private value non-persistence and entitlement denial. Browser tests use actual downloads and generated batches. Timer behavior uses Chromium's virtual clock; hidden-tab behavior is an explicitly simulated event. Navigation/search clearing and all four real tool APIs are independently exercised.

Media checks verify actual pixels, audio hashes, metadata structures and independently decoded QR payloads. ASR-duration and downloader-progress regressions use mocked adapters: unknown duration, estimated byte totals and playlist scope do not claim measured whole-job progress. Full ASR, speaker or website execution is not implied.

Direct browser tests load served assets through a real loopback service and consume one-use launcher tickets, then test remembered normal tabs. They cover accessible icon tabs, retained draft values, visible output controls, all tool/navigation views, queue actions/details, real QR/metadata jobs, generator exports/presets, the Black Scripting workbench and responsive widths down to 390 px. Large-history records are explicitly labeled database fixtures in disposable data; those fixtures do not substitute for real worker tests. Screenshots show the implemented interface.

## Reproduction and artifact verification

```text
.venv/bin/python -m pytest -q
.venv/bin/python tests/browser_v4_check.py --runtime <disposable-runtime.json> --output <evidence-directory> --allow-test-writes
.venv/bin/python tests/browser_black_scripting_check.py --output <evidence-directory> --allow-test-writes
.venv/bin/python tests/browser_generators_check.py --output <evidence-directory>
.venv/bin/python tests/integration_upgrade.py --base <prior-zip> --build <candidate-zip> --output <new-disposable-directory> --allow-test-writes
```

The two domain browser harnesses start and clean up their own temporary runtimes. The Studio harness requires an explicitly selected disposable runtime and changes its settings/jobs; never use production data. On Windows substitute the appropriate Python executable. System FFmpeg/FFprobe and Chromium are needed for these development checks.

`BROWSER_CHECKS.json`, `BROWSER_TEST_RESULTS.txt`, `PYTHON_TEST_RESULTS.txt`, `UPGRADE_INTEGRATION.json` and screenshots contain current evidence. The packaged report describes the pre-evidence candidate; the exact final ZIP is independently installed and rolled back again from all three baselines. Final hash/results accompany the ZIP. Final application/test source hashes are compared against the validated source/candidate when assembling delivery. The manifest covers all packaged application files and excludes user data/environments/models/tools.

## Remaining acceptance checks

- Native Windows BAT/PowerShell/bootstrap, Explorer/Tk, shortcuts/Send-to, NTFS locks/ACLs, antivirus and power-loss behavior.
- Windows 11 Edge/Brave/Firefox acceptance; direct browser execution here used Linux Chromium.
- Physical CUDA memory/inference behavior, optional transcription/speaker models, live downloads and target-phone QR scanning.
- Exact visual comparison/custom icon integration: the screenshot, icon and video/transcript referenced in the brief were not attached. Existing branding and a local SVG icon are used provisionally.
- Production billing, account/device activation, signed-license cryptography and signed release distribution are intentionally absent. V0.4 supplies offline development grants and adapter boundaries only.

For Windows acceptance, put the intact V0.4 ZIP in `B:\Forge\Build`, finish/cancel queued work and run `B:\Forge\Update FORGE.bat`. Confirm V0.4, open a second normal browser tab, run a short disposable media chain and QR job, inspect all outputs and check existing presets/history. Keep `Backups` intact for the verified code-rollback path.
