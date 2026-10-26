# FORGE development and release instructions

## Authoritative source and collaboration

- The authoritative FORGE repository is https://github.com/BirthrightPublication/forge. Work against its current source and history; a downloaded release ZIP is an installation artifact, not a substitute repository.
- Before development, inspect the current branch, remotes, working tree, and applicable instructions. Fetch the authoritative remote and read the latest relevant branch and changes before choosing a baseline. Preserve all existing uncommitted work. If fetching is unavailable, record the limitation and do not claim the local checkout is current.
- Use a dedicated, descriptively named feature branch. Make meaningful commits that explain the resulting behavior. Include appropriate tests and documentation. Coordinate ownership when several agents share the checkout; do not overwrite another contributor's work.
- Push completed source changes to the authoritative repository when authorized by the user's standing development request. Open a pull request where the available GitHub workflow supports it. Merge only with user approval. Do not silently treat permission to push or open a PR as permission to merge.
- Never force-push stable branches, discard unrelated changes, or rewrite published history to make a release appear clean. Preserve the project's history and reviewable changes.
- Verify the remote branch commit SHA after pushing. Verify PR, tag, and release URLs and their actual state before reporting them as published or merged. If an operation is blocked, report the exact incomplete action rather than claiming success.

## Source privacy and repository hygiene

- Track application source, dependency definitions, tests, intentional synthetic fixtures, documentation, and reviewed screenshots. Do not blanket-ignore media, images, JSON, HTML, or other extensions needed by these files.
- Keep environments, dependency caches, installed tools and models, credentials, private media, application data and history, generated exports, build/install artifacts, backups, and release ZIPs out of Git. Use the root runtime directories and ignored artifact locations; do not store private inputs beside source.
- Before staging and publishing, inspect the exact tracked and staged inventory, including binaries and documentation. Search for credentials, private paths, machine/user identifiers, private media, runtime tokens, cookies, and accidental export/reference records. Review screenshots for private content and metadata. A clean `.gitignore` does not prove existing tracked files are safe.
- Windows `.lnk` shortcut files can embed machine identifiers and private paths. Omit them from published source and release packages; generate needed shortcuts locally using the maintained setup scripts. The ignore rule does not remove already tracked shortcuts. Coordinate any removal from the source tree and preserve user-owned installed shortcuts.
- Test ignore changes with representative runtime artifacts and legitimate future source/fixture paths. Do not use broad exclusions that silently hide new application code or release documentation.

## Implementation and dependencies

- Preserve existing FORGE data, settings, presets, Black Scripting definitions, queue history, original scripts, models, environments, and exports during incremental upgrades and rollback. Use the existing manifest and updater contracts.
- Keep processing local by default, preserve original media, and distinguish measured results from unsupported or untested claims. Do not add telemetry, media uploads, or mandatory online activation without explicit authorization.
- Run tests appropriate to the change, including real media and integration checks where practical. Record what actually ran and distinguish Windows-native, GPU, codec, and optional dependency coverage that remains untested.
- Do not introduce a new license grant or relicense code without user approval. The existing FORGE MIT license has the user's approval; preserve its scope and notices. Review third-party code, binaries, model weights, and distribution terms separately before bundling them.
- Do not silently copy or duplicate TrueEngine into FORGE. If a cross-repository integration is authorized, use an explicit reviewed dependency boundary, pin the exact upstream version or commit, and document compatibility and licensing.
- Keep Birthright Update Core application-independent. FORGE's cache, queue, environment and installer behavior belongs in adapters, not in the shared package. Atlas is still being developed; do not claim its implementation was inspected or silently duplicate another application's updater.
- Discovery never authorizes a new application version. Require explicit approval, preserve the installed external updater, verify packages before replacement, and retain rollback/recovery journals. Compatible repairs may reuse prior approval for an explicitly registered dependency class.
- Signed manifests must use application-pinned publisher keys and fail closed on signature, application/channel, expiration or sequence errors. Do not label SHA-256 checksums as signatures or fabricate production signing keys. Document the current unsigned GitHub mode until a real signing policy is approved and configured.

## Releases and completion reporting

- Tag tested release commits. Every release must publish exactly one Windows installation ZIP named `FORGE-vMAJOR.MINOR.PATCH-windows-x64.zip`, its identically named `.zip.sha256` companion, and validation evidence. Its root must contain `app/` with the complete application payload. Preserve legacy `FORGE-vX.Y.zip` assets for the offline updater. Do not substitute GitHub's automatic source ZIP or commit release ZIPs to Git.
- Validate the Windows payload against the supplied `Update-Application.ps1` contract, including every protected path component. Exclude installed updater entrypoints, user state, environments, models, original scripts, test fixtures and historical documentation directories. Never add a second Windows-named ZIP for evidence: the updater requires exactly one matching ZIP. Exercise the unchanged PowerShell updater in a disposable Windows installation before publication, including settings/presets/history preservation and failed-validation paths.
- Build application files from the exact release tag and record the packaging-tool commit separately. A packaging correction may add missing assets to an existing release without moving its tag or replacing existing assets. Existing names must match the expected bytes or publication must stop. Future tagged and manually published releases must run the Windows packaging workflow and its test gates.
- Preserve compatibility with `B:\Forge`, `B:\Forge\Build`, `B:\Forge\Backups`, and `Update FORGE.bat`. Test incremental upgrades and rollback against supported earlier releases before describing them as verified.
- Verify the final packaged artifact, release tag/commit association, asset checksum, and public or authorized release/asset URLs before claiming the release is available. A locally created ZIP or tag is not a published GitHub release.
- The final release report must identify the repository, branch, commit SHA, push status, PR and merge status, tag and publication status, package location/checksum, tests actually executed, update instructions, and known limitations. State explicitly which remote actions or target-platform checks remain pending.

## V0.7 persistence and validation

- Download Studio must reuse the existing download worker. Source adapters declare actual capabilities; keep network discovery outside scheduler/database locks and sanitize extractor failures before exposing them.
- Channel Watch tables are additive. Preserve stable platform/media identities across source renames, queue retries and restarts. Commit automatic queue jobs and media associations atomically. Historical backfill is opt-in; explicit authentication failures never trigger retry loops.
- Keep source authentication paths local and omit them from public API snapshots and ordinary presets. Never package cookies or resume caches. Generated addresses remain memory-only; options-only locks/presets must not store generated values.
- A managed update is not complete when files are copied. Verify a fresh import and the new process identity, installation root, runtime version and health version before recording completion. Keep old-version/foreign-root launcher guards and native stale-bytecode/restart tests.
- Run `tests/browser_v7_check.py`, the existing browser regression harnesses, local HTTP downloader tests, upgrade/rollback and native Windows acceptance before proposing a V0.7 release. Distinguish offline source fixtures from live platform tests. Owner review remains required before merge/tag/publication.

## V0.8 speech production

- Keep original Kokoro voice IDs and tensors, including am_onyx; never substitute a different voice under the Onyx name. Native embedding blending requires compatible engine, language and revision.
- Treat model-native emotions, natural-language delivery, reference synthesis and acoustic DSP as distinct capabilities. Reject unsupported task combinations and never speak internal document markers.
- Snapshot project/preset/model/runtime revisions and permitted reference hashes for queued work. Preserve original documents/audio and content-addressed completed segments across retries and upgrades.
- Optional model installs require explicit approval and isolated immutable environments. Never install PyTorch over the existing Whisper environment or execute downloaded model-host Python/pickle payloads.
- Preserve text-only Bark output until alignment is actually validated. Do not invent timestamps or advertise English-only recognition as multilingual.
- Run the speech API/browser/DSP/import/store/adapter tests alongside regressions. Distinguish synthetic speech orchestration fixtures from actual neural inference and GPU/Windows acceptance. V0.8 remains blocked for final release while those acceptance checks and model/license review are pending.
