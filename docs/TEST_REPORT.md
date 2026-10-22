# FORGE V0.6.0 candidate validation report

Baseline: `a1dcaef95428886d57074cea23fe4d4ec04a3f5c`, approved V0.5 source and Windows packaging fixes.
Branch: `development/v0.6-automation`. V0.6 merge, tag and publication remain pending.
The restored cloud execution environment successfully fetched GitHub and ran the
baseline suite: 573 tests passed. Earlier environment initialization failures were
infrastructure failures, not application failures.

## Verified in this Linux environment

- Full candidate suite: **647 passed, 2 Windows-only tests skipped** on Linux,
  with background thread exceptions treated as errors. The earlier discovery
  shutdown race is fixed; the complete rerun passed in 90.82 seconds.
- New tests cover model integrity, interrupted downloads, cache locking and offline
  reuse; secret-filtered persistent preferences; measured timing; CUDA readiness
  evidence and CPU scheduling; approved update gates; manifest signatures, replay
  and expiration; safe verified downloads; environment backup/rollback; and external
  updater preservation.
- Durable maintenance recovery tests cover interrupted replacement, restoration
  from the journal, refusal to race an active installer, incomplete backup handling
  and the service launch guard. Cancelling a request cannot revive an older waiter.
- Real Chromium checks passed: Studio 127, Media Privacy 88, Black Scripting 26,
  generators 83, and Birthright Updates/V0.6 26 (350 checks total). Real media and QR
  jobs ran, originals remained unchanged, and no JavaScript errors were reported.
- Workflow validation with actionlint, Python compilation, JavaScript syntax and
  `pip check` passed. The exact supplied PowerShell updater fixture remains unchanged.

## Evidence boundaries

Model publisher responses and model weights in unit tests are explicitly synthetic.
The cloud network proxy denies Hugging Face metadata requests, including after
permission escalation. An actual first model download/inference is therefore not
claimed from this environment. A physical RTX 5080 and Windows CUDA libraries are
not attached to this Linux runner. Enumeration, runtime loading and successful
inference remain separate evidence in both UI and tests.

Two native Windows process-handle tests are intentionally skipped on Linux and run
in the Windows development/release jobs. Native Windows updater acceptance must
pass separately before publication; it exercises the unchanged supplied PS1 against
an isolated installation and preserves settings, presets, history and local builds.

The signed manifest verifier uses ephemeral test keys. Production publisher keys
have not been configured. Current GitHub releases use the existing HTTPS/SHA-256
contract, not a claimed publisher signature. FFmpeg/FFprobe/ExifTool and GPU drivers
remain user-managed; no arbitrary executable installer is downloaded or run.

## Final candidate gates

Incremental upgrade and code rollback passed from V0.1, V0.2, V0.3, V0.4 and V0.5
using the candidate built from commit `daadc5289f46f5f4754789e0dc545a72095f4019`.
User settings, presets, scripts, history, model/environment sentinels and exports were
preserved. The source-only credential-pattern scan found no matches across 261 paths.
The branch was pushed and verified; draft PR #1 is open.

Initial GitHub run `39855665421`: native Windows maintenance/shared-core tests passed.
Windows source packaging failed before updater acceptance; checkout byte conversion
is now disabled and package failure details are emitted as annotations. Linux CI
hit its time limit while preparing dependencies, before tests started. Dependency
setup now reuses installed media tools and has bounded retries/timeouts. These CI
failures do not replace the passing local tests; the next run must still pass.

Final source/package hashes and native Windows updater acceptance remain pending.
A candidate ZIP or local commit is not a published release. Review and explicit
approval are required before merging, tagging and publishing V0.6.0.

The prior version's report is preserved at `history/v0.5/TEST_REPORT.md` and in Git history.
