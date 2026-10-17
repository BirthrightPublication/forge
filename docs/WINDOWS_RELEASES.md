# Windows GitHub updater packages

The supplied `B:\Forge\Update-Application.ps1` selects exactly one release asset whose name contains `win`, `windows`, `x64` or `amd64` and ends in `.zip`. The canonical FORGE name is **`FORGE-v0.5.0-windows-x64.zip`**, with a checksum asset named **`FORGE-v0.5.0-windows-x64.zip.sha256`**. Future releases keep the full three-part version.

The checksum contains the archive's SHA-256 followed by two spaces and its exact filename. The updater checks the first token before extracting. It then requires this layout:

```text
app/
    VERSION.txt
    launcher.py
    server.py
    Open FORGE.bat
    Setup FORGE.bat
    requirements-core.txt
    release-manifest.json
    forge/
    web/
    scripts/
    docs/
    ... other application files ...
```

There is no enclosing `FORGE-v.../` directory. The updater copies `app/`'s contents into its installation root; it does not create a second application installation under `app/`.

## What is included and preserved

The payload contains the complete Python application, browser assets, launch/setup scripts, dependency definitions, license and applicable documentation. It uses the existing Windows x64 Python environment and installed optional media tools. It does not bundle a virtual environment, Python distribution, model weights or FFmpeg binaries. A new installation requires supported 64-bit Python and the existing `Setup FORGE.bat` procedure; this is an installation source payload, not a self-contained executable distribution.

Packaging checks the exact tag's source manifest, preserves included application bytes, and writes a package-specific manifest for the filtered payload. It omits every protected path component from the supplied updater, including `Update FORGE.bat`, `Update-Application.ps1` and `docs/history`. Tests, original scripts, user settings, databases, presets, queue history, media, environments, models and installed tools are also excluded. Original script reference copies and historical evidence remain preserved in Git and the legacy assets.

Keep your existing updater files. Finish queued jobs and close FORGE before running `B:\Forge\Update FORGE.bat`; the PowerShell updater prompts for `UPDATE` before copying. Its backup and log directories are `Update-Backups` and `Update-Logs`. It does not migrate data or automatically run an application health check. Release acceptance runs health checks separately on disposable installations.

The legacy `FORGE-v0.5.zip` retains its original layout for `scripts/update.py` and the older standalone BAT. The two package formats have different updater contracts. Existing assets and tags are never renamed, replaced or deleted to correct missing Windows assets.

## Build and validate

Use a clean checkout of the exact application tag as `--source`; run the packaging tool from the reviewed release-tooling commit. Do not package a working user installation.

```bash
python scripts/package_windows_release.py build \
  --source /path/to/exact-tag-checkout \
  --output /path/to/assets --tag v0.5.0

python scripts/package_windows_release.py validate \
  /path/to/assets/FORGE-v0.5.0-windows-x64.zip \
  --tag v0.5.0 \
  --checksum /path/to/assets/FORGE-v0.5.0-windows-x64.zip.sha256

python -m pytest tests/test_windows_package.py -q
```

The builder verifies source versions and inventory, required runtime files, path safety, per-file hashes and protected-path exclusions. Before tagging future application releases, regenerate the source manifest using the ordinary package builder and commit that manifest together with the intended source. A stale or modified source manifest must fail Windows packaging.

## Automatic release assets

The normal tag-triggered release workflow calls `.github/workflows/windows-release.yml` as a required build. It waits for the Windows tests and includes the tested Windows assets before publishing the new release. The Windows workflow also handles manually published releases, explicit workflow dispatches and the `.github/windows-release-target` backfill file. The reusable call covers releases created by `GITHUB_TOKEN`, whose release event does not start another workflow.

The workflow tests the exact tagged application and packaging tool, creates the archive, and exercises the unchanged user-supplied PowerShell updater on a Windows runner. All installations and state fixtures are under newly created temporary directories. Successful install, backups, application startup, checksum refusal, cancellation and protected-file preservation gate publication. Source and tooling commits are recorded separately in the Windows validation asset.

Only the publisher job has release-write permission. It waits for the intended published release, checks the existing asset inventory and adds the missing Windows assets. If an asset already exists, its bytes must match; it is never uploaded with overwrite/clobber enabled. Publishing a second Windows-named evidence ZIP would break the updater's single-asset rule, so validation evidence uses JSON rather than another matching ZIP.

The V0.5.0 packaging correction adds assets to the existing release while keeping its source tag at `803f2231f5e27c4f95857cc4c13078552da9b7ff`. Exact installation results belong in the attached Windows validation report. Windows installer acceptance does not establish NVIDIA encoding, optional speech inference, browser codec or model-performance coverage.
