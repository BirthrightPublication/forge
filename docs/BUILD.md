# Building and publishing FORGE

Use a tagged source checkout for a release. `VERSION.txt`, `forge/__init__.py`, and the tag must agree: version `0.4.0` uses tag `v0.4.0` and produces `FORGE-v0.4.zip`; patch version `0.4.1` produces `FORGE-v0.4.1.zip`.

The package builder includes the updater's allowlisted application files and a manifest containing their SHA-256 hashes. It excludes environments, tools, model caches, user data, build folders and backups. It validates the resulting ZIP with the real updater before returning success. ZIP timestamps can differ between builds, so retain and verify the checksum accompanying the exact archive you distribute.

## Local validation and build

Use Python 3.12 for the release validation environment. Install FFmpeg/FFprobe and a Chromium browser for real media and browser checks. On Ubuntu 24.04:

```bash
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
& .\.venv\Scripts\python.exe scripts\package_release.py --output 'B:\Forge\Build'
```

The builder prints the archive path, SHA-256 hash, and packaged file count. It writes a matching `.zip.sha256` file. Distribute the source checkout's `Update FORGE.bat` beside the ZIP as a separate download as well; the same updater is already included inside the package. Existing `original_scripts` in an installation are preserved by the updater.

Run browser and upgrade acceptance before tagging a new release. The browser harnesses create or explicitly require disposable services; never point them at production data. The Studio harness takes `--runtime <disposable-runtime.json> --output <evidence-directory> --allow-test-writes`. The Black Scripting harness takes `--output <evidence-directory> --allow-test-writes`, and the generators harness takes `--output <evidence-directory>`. Supply `--chromium <executable>` when Chromium is not on PATH. V0.5's `tests/browser_v5_check.py` also requires `--output`, `--chromium` and `--allow-test-writes`.

Exercise each supported prior release using a new disposable destination:

```bash
.venv/bin/python tests/integration_upgrade.py \
  --base /absolute/path/to/FORGE-v0.3.zip \
  --build /absolute/path/to/FORGE-v0.4.zip \
  --output /tmp/forge-upgrade-v03-to-v04 \
  --allow-test-writes
```

This runs a real upgrade and code rollback, starts the candidate application, processes a QR job, and checks retained data and outputs. Use the version-appropriate harness and candidate. Linux tests do not establish native Windows BAT/PowerShell, Explorer, NTFS, antivirus, physical GPU, model-inference, or live-download acceptance; record those checks and remaining limitations in the release validation report.

## GitHub release automation

[`.github/workflows/release.yml`](../.github/workflows/release.yml) has two explicit publication triggers:

- Push a new `vMAJOR.MINOR.PATCH` tag whose commit contains the workflow. This is the normal path for future releases.
- Push a change to `.github/release-target` on `main`. The file must contain only an existing tag, such as `v0.4.0`. This supports publishing a completed historical release whose tagged commit predates Actions configuration, including the initial publication when the development environment cannot call the GitHub Releases API directly. The runner checks out that exact tag; it does not package the current `main` files.

Changing the workflow alone does not trigger publication. The target file is a deliberate publication request for a tested historical tag; it does not authorize moving a tag or replacing existing release assets. Keep it in the infrastructure commit when first publishing an older tag. For normal new releases, finish and commit the source, version files, changelog and validation evidence before creating the tag:

```bash
git tag -a v0.5.0 -m 'FORGE v0.5.0'
git push origin main
git push origin v0.5.0
```

Those commands are an example for a completed V0.5 release, not a declaration that an in-progress V0.5 checkout is release-ready. For the historical target path, commit `.github/release-target` containing the intended existing tag and push `main` after that tag is available on the remote.

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
