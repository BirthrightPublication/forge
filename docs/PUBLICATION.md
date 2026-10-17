# Initial public source publication

The authoritative FORGE repository is https://github.com/BirthrightPublication/forge. V0.4.0 is the latest completed application version at initial publication. The source comes from the complete working Git checkout and its existing commits, not reconstruction from an older ZIP. V0.5 work is preserved on a separate development branch.

## Privacy and history

The repository was empty before publication. Before publishing any commit, the complete original Git history was saved in a verified private Git bundle outside the source tree. Source versions and their chronological patches are retained in the public history, with one file exclusion: `original_scripts/YT Download.lnk`. That unused Windows shortcut contains machine-identifying shell-link metadata. It is excluded from every publicly reachable historical commit and from rebuilt public release packages. The application never executes it. Installed user copies remain untouched by the updater.

The original commit author and committer metadata contained a personal contact address. Public commits use a neutral FORGE Development identity with a non-deliverable `.invalid` email; exact authorship remains in the private archive. The obsolete shortcut hash entry and release-manifest entry were also removed.

This sanitization changes the unpublished commit IDs for V0.2, V0.3 and V0.4. It does not rewrite any previously published remote history. Old private release ZIPs are retained privately and must not be uploaded. Public packages are rebuilt from sanitized version tags, so their checksums differ from the original private packages. Historical validation documents describe their original artifacts; each new public package has its own checksum and CI/test results.

Tracked history, current source, original reference scripts and documentation images were reviewed for credentials, private data and externally sourced assets. Pattern scanning is supplemented by inspection of binary metadata and screenshots; it is not a proof that every possible secret pattern can be detected. No runtime databases, environments, model weights, downloaded media or credentials belong in this repository. Test credentials and media are synthetic fixtures.

The owner explicitly approved retaining the existing MIT license and history. No additional software license grant is introduced by publication. Third-party dependency terms are documented in `COMMERCIAL_DEPENDENCIES.md`; packages and model weights are not bundled.

## Publication and verification

A successful local commit, package build or dry-run push does not prove publication. Verify remote branch/tag SHAs with `git ls-remote`, then verify the GitHub Release and its attached ZIP/checksum/test results. Direct API access may be unavailable in a development environment even when Git push succeeds. The release workflow supports an explicitly selected historical tag through `.github/release-target`, runs tests first, and publishes using GitHub Actions' repository-scoped token. A pending, failed or inaccessible Actions run must be reported as unconfirmed rather than a published release.

V0.4 validation: 352 Python tests passed before publication. The clean public package is separately checked through the real updater and code rollback. Native Windows BAT/PowerShell/Explorer behavior and physical NVIDIA/optional speech engines require target-machine acceptance; Linux validation is not a claim of Windows or GPU certification.

## Verified initial publication

- Public tag `v0.4.0`: `34f5558af1d4001a16ad2c2d88e8d67f8c9e0b02`.
- [Release and assets](https://github.com/BirthrightPublication/forge/releases/tag/v0.4.0).
- [Successful test/build/publish run](https://github.com/BirthrightPublication/forge/actions/runs/38988862748): 352 Python tests and the three historical Chromium suites passed.
- Downloaded public `FORGE-v0.4.zip` SHA-256: `57df6b500bb10d1ec04c9c2a612c2cb6b4688be7df765eafddf0b6a71976d97b`.
- All downloaded release asset hashes match the published checksum list. GitHub normalized `Update FORGE.bat` to `Update.FORGE.bat`; rename it when saving to the Windows install root. The initial checksum entry uses the original spaced name; file bytes match.
- The public ZIP contains no Windows shortcut. Its embedded updater manifest validates, and the downloaded artifact is exercised through an independent V0.3 upgrade and code rollback.

The initial source/automation push was verified at `eb2436828d73fc5508eaa19a219c4208539da30b`; later documentation fixes are ordinary new commits, not moved version tags.
