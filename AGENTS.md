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

## Releases and completion reporting

- Tag tested release commits. Publish the updater-compatible `FORGE-vX.Y.zip` package as a release asset, with its manifest, checksums, and useful release/update instructions. Do not substitute GitHub's automatic source ZIP for the tested installation package, and do not commit release ZIPs to Git.
- Preserve compatibility with `B:\Forge`, `B:\Forge\Build`, `B:\Forge\Backups`, and `Update FORGE.bat`. Test incremental upgrades and rollback against supported earlier releases before describing them as verified.
- Verify the final packaged artifact, release tag/commit association, asset checksum, and public or authorized release/asset URLs before claiming the release is available. A locally created ZIP or tag is not a published GitHub release.
- The final release report must identify the repository, branch, commit SHA, push status, PR and merge status, tag and publication status, package location/checksum, tests actually executed, update instructions, and known limitations. State explicitly which remote actions or target-platform checks remain pending.
