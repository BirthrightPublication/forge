# Building the V0.7 candidate

Use the existing commands below with version `0.7.0`. Run the complete Python suite,
all existing browser harnesses, and `tests/browser_v7_check.py --output <new-disposable-folder>`.
The Windows review job additionally tests native process identity/waits, maintenance,
and the exact supplied PowerShell updater. The normal tagged release workflow gates
publication on both Linux/browser tests and the native Windows package checks.

Before committing changed application files, rebuild the source manifest using the
legacy builder and copy its `release-manifest.json` back to the source tree. Build
Windows candidates with `scripts/package_windows_release.py build --source .
--output <outside-source> --tag v0.7.0`; validate against that exact source inventory.
Final release artifacts must be rebuilt from the approved tagged commit. Never move
an existing version tag or replace published asset bytes to make a candidate match.

New assets are `FORGE-v0.7.0-windows-x64.zip`, `.zip.sha256` and validation evidence;
the compatible legacy asset is `FORGE-v0.7.zip`. Production signing remains disabled
until approved publisher keys are configured; see `BIRTHRIGHT_UPDATE_CORE.md`.

## Established build commands and package contracts

# Building and publishing FORGE

Every new release must include a Windows GitHub-updater installation asset named `FORGE-vMAJOR.MINOR.PATCH-windows-x64.zip` plus `.zip.sha256`, using the full version and a root `app/` directory. [WINDOWS_RELEASES.md](WINDOWS_RELEASES.md) documents its builder, tests and automatic publication. This format excludes protected updater entrypoints and local state. Preserve the existing offline package described below for users of the older updater.

Use a tagged source checkout for a release. `VERSION.txt`, `forge/__init__.py`, and the tag must agree: version `0.5.0` uses tag `v0.5.0` and produces `FORGE-v0.5.zip`; patch version `0.5.1` produces `FORGE-v0.5.1.zip`.

The owner approved merging and releasing V0.5.0. Its validation passed 478 Python tests, 324 real Chromium checks and upgrade/code rollback from V0.1, V0.2, V0.3 and V0.4. The release URL is [v0.5.0](https://github.com/BirthrightPublication/forge/releases/tag/v0.5.0). The following commands document the release procedure; they do not assert that the remote tag or assets have already been published. Distribute CI test/source information as `VALIDATION.json` and exact release-asset hashes as `CHECKSUMS.sha256` beside the ZIP. The four local upgrade runs are documented separately in `UPGRADE_INTEGRATION.json`.

The package builder includes the updater's allowlisted application files and a manifest containing their SHA-256 hashes. It excludes environments, tools, model caches, user data, build folders and backups. It validates the resulting ZIP with the real updater before returning success. ZIP timestamps can differ between builds, so retain and verify the checksum accompanying the exact archive you distribute.

## Local validation and build

Use Python 3.12 for the release validation environment. Install FFmpeg/FFprobe and a Chromium browser for real media and browser checks. On Ubuntu 24.04:

```bash
set -euo pipefail
sudo apt-get update
sudo apt-get install --no-install-recommends -y ffmpeg fonts-dejavu-core
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
# V0.5 and later: include the optional local Content Credentials test engine.
if [ -f scripts/requirements-media-privacy.txt ]; then
  .venv/bin/python -m pip install -r scripts/requirements-media-privacy.txt
fi
.venv/bin/python -m pip check
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/python -m pytest -q
.venv/bin/python scripts/package_release.py --output Build
```

For a Windows source checkout with Python and FFmpeg/FFprobe available, the corresponding PowerShell commands are:

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (Test-Path scripts\requirements-media-privacy.txt) {
    & .\.venv\Scripts\python.exe -m pip install -r scripts\requirements-media-privacy.txt
}
& .\.venv\Scripts\python.exe -m pip check
& .\.venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed; do not build a release.' }
& .\.venv\Scripts\python.exe scripts\package_release.py --output 'B:\Forge\Build'
```

The builder prints the archive path, SHA-256 hash, and packaged file count. It writes a matching `.zip.sha256` file. Distribute the source checkout's `Update FORGE.bat` beside the ZIP as a separate download as well; the same updater is already included inside the package. Existing `original_scripts` in an installation are preserved by the updater.

Run browser and upgrade acceptance before tagging a new release. The browser harnesses create or explicitly require disposable services; never point them at production data. The Studio harness takes `--runtime <disposable-runtime.json> --output <evidence-directory> --allow-test-writes`. The Black Scripting harness takes `--output <evidence-directory> --allow-test-writes`, and the generators harness takes `--output <evidence-directory>`. Supply `--chromium <executable>` when Chromium is not on PATH. V0.5's `tests/browser_v5_check.py` also requires `--output`, `--chromium` and `--allow-test-writes`.

Exercise each supported prior release using a new disposable destination:

```bash
.venv/bin/python tests/integration_upgrade.py \
  --base /absolute/path/to/FORGE-v0.4.zip \
  --build /absolute/path/to/FORGE-v0.5.zip \
  --output /tmp/forge-upgrade-v04-to-v05 \
  --allow-test-writes
```

This runs a real upgrade and code rollback, starts the tested application, processes a QR job and V0.5 privacy export, and checks retained data and outputs. Repeat with the V0.1, V0.2 and V0.3 base archives and separate disposable destinations. Use the version-appropriate harness and build. Linux tests do not establish native Windows BAT/PowerShell, Explorer, NTFS, antivirus, physical GPU, model-inference, or live-download acceptance; record those checks and remaining limitations in the release validation report.

## GitHub release automation

[`.github/workflows/release.yml`](../.github/workflows/release.yml) has two explicit publication triggers:

- Push a new `vMAJOR.MINOR.PATCH` tag whose commit contains the workflow. This is the normal path for future releases.
- Push a change to `.github/release-target` on `main`. The file must contain only an existing tag, such as `v0.4.0`. This supports publishing a completed historical release whose tagged commit predates Actions configuration, including the initial publication when the development environment cannot call the GitHub Releases API directly. The runner checks out that exact tag; it does not package the current `main` files.

The initial public history uses a neutral automation author identity and omits machine-specific Windows shortcut metadata. This preparation happens before the first remote publication; it is not a policy of rewriting already-published history, tags or releases. Existing license terms remain unchanged.

Changing the workflow alone does not trigger publication. The target file is a deliberate publication request for a tested historical tag; it does not authorize moving a tag or replacing existing release assets. Keep it in the infrastructure commit when first publishing an older tag. For normal new releases, finish and commit the source, version files, changelog and validation evidence before creating the tag:

```bash
git tag -a v0.5.0 -m 'FORGE v0.5.0'
git push origin main
git push origin v0.5.0
```

Those commands apply to the approved V0.5.0 release after its final source commit and exact package verification. Verify the resulting remote tag and release assets before reporting publication complete. For the historical target path, commit `.github/release-target` containing the intended existing tag and push `main` after that tag is available on the remote.

The workflow:

1. Validates the tag syntax, selects its exact commit, and checks both source version declarations.
2. Installs Python development requirements, FFmpeg, test fonts and Playwright Chromium. When present, it installs the pinned optional media-privacy requirements to exercise real C2PA tests. It does not install transcription models, download integrations, GPU drivers or billing services.
3. Runs the full Python suite and the available real Chromium harnesses against disposable local services. Any failed command blocks packaging and publication.
4. Builds and validates the updater-compatible ZIP, includes the standalone updater, changelog, test results, browser evidence, source commit identity and checksums, then transfers these exact assets to the publisher job.
5. Verifies transferred checksums, creates a draft GitHub Release with all assets, and publishes it only after every upload succeeds.

Test/build jobs have repository read permission. Only the separate publisher job receives `contents: write`, and it does not check out or execute application source. Actions are pinned to reviewed commit IDs. The workflow uses the repository-scoped `GITHUB_TOKEN`; no personal access token is required. GitHub Actions and release writing must be allowed by the repository or organization policy.

Already-existing releases are never overwritten. A failed upload can leave a draft requiring maintainer review; do not move an existing release tag or silently replace its assets. Publish a new patch version when released code needs changing. Failure evidence is retained temporarily as an Actions artifact; successful release assets remain available on the repository's Releases page. SHA-256 detects corruption and mismatched files, not publisher authenticity.

## Install the released build

Download the named `FORGE-v*.zip` **release asset**, not GitHub's automatic “Source code” ZIP. Put the intact archive in `B:\Forge\Build`, put the matching standalone `Update FORGE.bat` in `B:\Forge`, finish or cancel queued work, then run `B:\Forge\Update FORGE.bat`. Keep `B:\Forge\Backups` intact. See [UPDATE_GUIDE.md](UPDATE_GUIDE.md) for review, recovery and rollback behavior.

GitHub normalizes spaces in asset names. The standalone updater asset is `Update.FORGE.bat`; save it locally as `B:\Forge\Update FORGE.bat`. The initial V0.4 release checksum list records its original `Update FORGE.bat` filename; its bytes are identical. Future workflows checksum the normalized public asset name directly.
