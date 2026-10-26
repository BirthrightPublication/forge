# FORGE validation history

V0.8 implementation and pending model acceptance are recorded in [V0.8-VALIDATION.md](V0.8-VALIDATION.md).

The V0.7.1 startup repair is documented in [V0.7.1-STARTUP-REPAIR.md](V0.7.1-STARTUP-REPAIR.md).
V0.7.0 was subsequently merged and published at `bab3876878b20da5c2906b85177876844d3279f1`.
Its tagged release workflow passed 692 Python tests, 387 browser checks, 98 packaging
tests and 730 native Windows acceptance checks. The published release contains the
validation receipts. The following records describe its earlier development checks.

## Historical V0.7 candidate validation

Baseline: main `ddd49eb2e10f290708a99bf74de7dc088eff329f`.
Branch: `development/v0.7-studio-navigation`.
At this development checkpoint, V0.7 was unmerged and untagged, pending owner review.

## Local evidence

- Full Python suite: **691 passed, 3 native Windows tests skipped**, with background-thread exceptions treated as failures. This includes authentication/thumbnail/rule regressions. The added read-only maintenance status bridge passed its targeted tests; the complete final commit runs again in CI.
- Real yt-dlp + FFmpeg: MP4, MKV, MP3 and FLAC downloads from a harness-owned HTTP media source, partial-file handling, validated outputs and collision-safe publication passed. Source bytes remained unchanged.
- Browser regressions: 127 Studio, 26 Black Scripting, 83 generator, 88 Media Privacy and 26 V0.6 update/settings checks passed. V0.7 adds 37 checks for Download Studio, Channel Watch fixtures, navigation, copying/locks, mode transitions and the compact update drawer.
- Source-watch tests cover concurrent deduplication, source rename identity, restart persistence, optional backfill, live/replay transitions, maintenance/pause deferral, offline backoff, explicit authentication failures, priorities, retry association and archived-file hash checks.
- Address tests cover coherent geographic groups across six formats, locks, incompatible constraints, single-field regeneration and no generated-value persistence.
- Updater tests cover existing integrity/rollback contracts, fresh process/root/version checks and refusing stale/foreign runtime reuse. Native process/restart tests run on the Windows review job.

The first live YouTube metadata probe returned a sanitized network-connectivity failure. No live YouTube download or account-authenticated extraction is claimed. Physical GPU acceptance and the user's B: installation are not available here. Synthetic source metadata and generated local media are identified explicitly in tests.

## Release checks

The published V0.6 legacy ZIP was downloaded and its release SHA-256 verified. A disposable V0.6 → V0.7 upgrade and code rollback passed, preserving baseline data and new V0.7 saved destinations/watch history. Native Windows restart/stale-bytecode and unchanged-updater acceptance passed for commit `c5e7edc` in [review run 40038646418](https://github.com/BirthrightPublication/forge/actions/runs/40038646418). The final status-bridge commit is validated again by the [latest PR checks](https://github.com/BirthrightPublication/forge/pull/6/checks). The Windows ZIP must be `FORGE-v0.7.0-windows-x64.zip` with its `.zip.sha256` companion. Final release assets will be rebuilt from the approved tag; a development package is not a published release.

See [operations and implementation](V0.7-OPERATIONS.md), [build instructions](BUILD.md), [Windows package contract](WINDOWS_RELEASES.md), and [screenshots](screenshots/v0.7/).
