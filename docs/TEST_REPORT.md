# FORGE V0.6.0 candidate validation report

Baseline: `a1dcaef95428886d57074cea23fe4d4ec04a3f5c`, approved V0.5 source and Windows packaging fixes.
Branch: `development/v0.6-automation`. V0.6 merge, tag and publication remain pending.
The restored cloud execution environment successfully fetched GitHub and ran the
baseline suite: 573 tests passed. Earlier environment initialization failures were
infrastructure failures, not application failures.

## Verified in this Linux environment

- Full candidate suite: **642 passed, 2 Windows-only tests skipped** on Linux.
  Background discovery exposed a shutdown race in that run; it was fixed and the
  affected 104-test API/worker suite passed with thread exceptions treated as errors.
- New tests cover model integrity, interrupted downloads, cache locking and offline
  reuse; secret-filtered persistent preferences; measured timing; CUDA readiness
  evidence and CPU scheduling; approved update gates; manifest signatures, replay
  and expiration; safe verified downloads; environment backup/rollback; and external
  updater preservation.
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

Full Python suite, all supported-version upgrade/rollback runs, final package hashes,
source/privacy inventory, pushed commit and Windows CI results are pending recording.
A candidate ZIP or local commit is not a published release. Review and explicit
approval are required before merging, tagging and publishing V0.6.0.

The prior version's report is preserved at `history/v0.5/TEST_REPORT.md` and in Git history.
