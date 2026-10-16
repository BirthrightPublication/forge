# FORGE V0.3 validation report

Target product: Windows 11 local browser Studio. Validation host: Linux x86-64, Python 3.12.14. This release retains the supplied V0.2 baseline and focuses on local update review and recovery. V0.2 historical evidence is retained under `docs/history/` and is not counted as current execution.

## Executed results

| Check | Result |
|---|---|
| Supplied V0.2 baseline regression suite | 143 passed before implementation |
| Final Python unit/API/worker/media/updater suite | **185 passed**, no skipped tests, 16.28 seconds |
| Browser checks | **100 passed**: 89 transport-bridge checks and 11 direct Chromium checks; no uncaught JavaScript errors |
| V0.1 → V0.3 → code rollback | Passed with real candidate API and queued QR worker |
| V0.2 → V0.3 → code rollback | Passed, including retained workflows/favorites |
| Preservation in each upgrade path | All 8 original scripts, 10 private file markers, settings, presets, history, inputs and outputs retained; baseline source bytes restored on rollback |
| Real HTTP service restart | Same-port restart, runtime-token rotation, remembered cookie and CSRF persistence passed |
| Source validation | Python compileall, all three JavaScript syntax checks and Git whitespace check with native CRLF BAT handling passed |
| Cloud setup repeatability | Saved installation commands rerun successfully; pip check passed; authenticated API and real QR output verified after startup |

The Python suite has one upstream Starlette/AnyIO deprecation warning. It is not a test failure. Direct dependency pins remain unchanged; no optional engine/model is required for the update workflow.

## V0.3 coverage

New checks exercise manual local inventory, compatible semantic-version selection, all newest-version duplicate conflicts, corrupt/malformed packages, linked ZIPs, local target conflicts, backup integrity and rollback status. Browser controls require explicit consent and CSRF, accept fixed action names only, refuse extra command/path fields and busy queues, and prevent duplicate console openings. Actual Windows Popen arguments are inspected with a test double; Windows console execution is not inferred from that test.

Recovery tests cover pending and orphan maintenance markers, dry-run immutability, pause-preference persistence, tampered backups, interrupted manual rollback, and marker retention when recovery fails. A full integration run exposed same-second/same-size Python bytecode reuse; the fix invalidates only matching caches for changed/restored sources across interpreter tags, refuses linked cache paths and verifies the imported version. A deterministic subprocess regression and both real archive upgrade paths passed after correction.

Browser checks retain the original 70 navigation/workflow/media controls and add 19 update-panel checks plus 11 direct browser checks. Synthetic inventory fixtures are explicitly used for markup escaping and blocked-state rendering; real requests independently validate the installed version, build path, absence of automatic scans and local scan responses. Direct Chromium loads the served app, consumes a real launcher ticket, authorizes another normal tab via its cookie, completes a real queued QR job and retrieves its authenticated PNG preview. Chromium loopback networking works in this environment; no networking policy bypass was used. The bridge portion separately exercises actual FFmpeg workflow/metadata/QR workers and layouts down to 440 px.

## Evidence and delivered artifact

`PYTHON_TEST_RESULTS.txt`, `BROWSER_CHECKS.json`, `BROWSER_TEST_RESULTS.txt`, `HTTP_RESTART_RESULTS.json` and `UPGRADE_INTEGRATION.json` contain current results. The documented candidate was tested before final evidence assembly; the final delivered ZIP is independently installed and rolled back again from both baselines. Its exact hash and final artifact reports are supplied alongside the ZIP. The manifest covers every packaged application file; application/test-source bytes are checked against the candidate when assembling the final artifact.

## Not executed here

- Native Windows BAT/PowerShell/bootstrap, console windows, Explorer/Tk/shortcut/Send-to integration, NTFS locks/ACLs, antivirus or power-loss behavior.
- Windows Edge/Brave/Firefox acceptance. Direct browser execution here used Linux Chromium.
- Actual transcription, speaker-model inference, GPU/CUDA performance, model downloads or live website downloads.
- Digital publisher signatures, managed dependency rollback, arbitrary custom script execution, folder scheduling or online terminology verification. Those are outside V0.3 scope.

## Windows acceptance procedure

Replace `B:\Forge\Update FORGE.bat` with the standalone V0.3 updater; put the intact `FORGE-v0.3.zip` in `B:\Forge\Build`. Finish/cancel work, run the updater and review its selection before confirming. Confirm V0.3 in Studio, open a second plain bookmark tab, run a disposable QR job and inspect its output. In Environment, check the actual build paths and verified rollback status. Review rollback in the console only if you intend to return to the earlier code. Keep Backups intact; never delete markers or weaken verification to bypass a failure.
