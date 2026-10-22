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

GitHub run [39860918101](https://github.com/BirthrightPublication/forge/actions/runs/39860918101)
passed all jobs for implementation commit `7643ba001ed4ad4fff1dd81b5a831c4dbb39fae3`:
complete Linux Python suite, native Windows maintenance/shared-core tests, Windows
package construction, and the unchanged PowerShell updater's installation, data
preservation and failure-path acceptance. The earlier package-byte and dependency
preparation failures are resolved. CI retained both Linux and Windows evidence
artifacts; this cloud proxy blocks downloading their Azure-backed contents (HTTP 403),
so native success is verified through GitHub's completed job/step status.

The final implementation's V0.5 upgrade/rollback was repeated successfully, including
real QR and privacy export jobs, current data reopened by V0.5, seven original scripts
and fourteen protected file sentinels. The V0.6 Chromium checks were also repeated:
all 26 passed. Local Windows package validation verifies 253 source files and retains
164 application files plus the manifest under `app/`; its matching checksum is built
alongside it. Candidate hashes are recorded in PR #1 and checksum companion files.

A subsequent batch-order regression test ensures model jobs wait for environment
maintenance in a combined update. The affected integration/maintenance suite passed
31 tests, with two Windows-only skips; GitHub repeats the full suite on that commit.
The report-only predecessor also passed native Windows acceptance. Current PR checks
identify the latest tested source. Merge, tag and public release remain pending
explicit approval. Tagged release
workflows repeat their required gates and publish only successfully tested assets.
A candidate ZIP or local commit is not a published release.

The prior version's report is preserved at `history/v0.5/TEST_REPORT.md` and in Git history.
