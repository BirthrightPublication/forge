# FORGE V0.5 Windows updates

## Installed GitHub updater

If your installation contains `Update-Application.ps1`, keep that script and its `Update FORGE.bat` wrapper. Finish queued work and close FORGE before starting the wrapper. It selects the latest published GitHub release's single `FORGE-vMAJOR.MINOR.PATCH-windows-x64.zip`, verifies the matching `.zip.sha256`, and installs only the contents of `app/` after you type `UPDATE`.

Use the [normal temporary-path command](WINDOWS_RELEASES.md) when starting the wrapper. Native testing found that the supplied updater can calculate incorrect destinations when TEMP contains an 8.3 path alias; setting this process's TEMP to `B:\Forge\Build` avoids that condition without changing either installed updater file.

For V0.5.0, the matching assets are `FORGE-v0.5.0-windows-x64.zip` and `FORGE-v0.5.0-windows-x64.zip.sha256`. The package omits settings, presets, queue/history data, media, environments, models, original scripts, and the installed updater entrypoints. This updater keeps overwritten application files and a rollback manifest in `B:\Forge\Update-Backups` and logs in `B:\Forge\Update-Logs`. It does not automatically stop the application, migrate data or perform a health check after copying. See [WINDOWS_RELEASES.md](WINDOWS_RELEASES.md) for the tested contract.

The remaining sections describe the **older offline build updater** and its different package, backup location and confirmation prompts. They do not require replacing your installed GitHub updater.

## Your folder layout

```text
B:\Forge\
    Update FORGE.bat        ← replace with the compatible standalone updater
    Build\
        FORGE-v0.5.zip      ← leave new release ZIPs here, not extracted
    Backups\               ← updater-created code backups and database snapshots
    ... active application files, or ONE existing extracted FORGE folder ...
```

The updater supports application files directly under the chosen root, or exactly one extracted `FORGE*` child with `server.py` and `forge/__init__.py`. It does not move that folder, relocate `.venv`, rebuild shortcuts, or guess between multiple active copies. A nested folder may retain its original `FORGE-v0.1` name after its code becomes V0.5; that is intentional, to avoid breaking paths.

For the first upgrade, place the standalone `Update FORGE.bat` in `B:\Forge`. It contains the updater source, so it works even though V0.1 had no updater. You do not need to extract files out of the release ZIP manually. Future compatible releases use the same action; the package also includes an updater beside its application launcher.

## Studio review and external recovery

Replace the standalone updater in the main `B:\Forge` folder with the supplied compatible file (same verified updater source). Keeping only the newer copy inside a nested application folder would leave the external recovery command on its old version. V0.1, V0.2, V0.3 and V0.4 installations can upgrade directly through this bootstrap.

Once V0.5 is running, **Environment → Application and package updates → Application updates → Check local build folder** shows local package verification, compatibility, planned changes and verified rollback status. Scanning is read-only and offline. **Review update in console** and **Review rollback in console** open the fixed local updater, with final confirmation there. The backend remains available until that confirmation. A busy queue prevents opening the console and is checked again before shutdown. Refresh a prior busy scan after jobs finish.

The Studio's read-only inventory also works on Linux for development; native console launch requires Windows. An interrupted installation cannot use Studio for recovery: run the external BAT instead. No browser-supplied commands, internet releases, driver updates or package upgrades are executed by this feature.

## Normal procedure

Finish or cancel all queued/running work and stop submitting jobs. Double-click the updater. It validates available `FORGE-v*.zip` packages in `Build`, chooses the highest newer semantic version, and displays that choice. A later file modification date is not treated as a newer version. Two different archives claiming the same newest version cause a refusal; identical duplicate downloads are allowed.

Press Enter to update, Q to cancel, or R for the available code rollback. No build is downloaded by the updater and no dependency installation is performed. Keep the console open until it finishes.

The updater verifies every manifest-listed file, rejects unlisted/missing/unsafe entries, checks embedded source compilation, backs up files that will change, snapshots the SQLite database through SQLite's backup API, then replaces application files. Existing original scripts are skipped even if the published reference copy differs. Local environments, tools, data, Build, Backups and model directories are protected.

If FORGE is running, the updater refuses to stop it while any queued, active or finalizing job exists. Its check includes older records outside the UI's 500-row history window. V0.2+ can freeze new job admission atomically while preparing maintenance. The initial V0.1 transition lacks that endpoint, so leave the old browser tabs alone during the update. The OS service lock must be available before copying; no process is forcibly killed.

Package and local target preflight happens before shutdown, including explicit ZIP and dry-run commands. Generated bytecode for replaced or restored Python sources is invalidated across interpreter tags; linked cache paths are refused. After copying and byte verification, the private Python environment imports the application, checks that its reported version matches the source, and constructs it with disposable data. Failure triggers restoration of the prior code. Success retains backups, restores the prior queue-pause preference, and invokes the launcher. **This smoke check does not prove Windows native dialogs, CUDA inference, all codecs, model compatibility or live downloads.**

Close/reload old tabs and confirm the V0.5 badge. Open from the launcher once in your browser profile, then test the plain bookmark in another tab. A stopped server still needs the launcher; a bookmark cannot create a Windows process.

## Rollback and interruption

R restores the latest registered code backup. Only changed application files are restored; files introduced by that update are removed. Current user data and completed outputs are not replaced by an older snapshot. The database snapshot is retained under the backup directory for deliberate recovery rather than being applied automatically.

New V0.2 workflow settings or tool options may not be understood by V0.1 after a code-only rollback. Older history and baseline preferences were checked in the integration test; arbitrary newer combinations are not a promise of downgrade compatibility. Keep the separate snapshot, and do not overwrite the current database casually to repair a UI preference.

An unfinished update or manual rollback records `.forge-update-state.json`. A durable `.forge-updating` marker also retains the original queue pause preference where available. V0.3 maintenance stores that preference in SQLite before changing it. Opening FORGE is blocked while a journal/maintenance marker indicates an incomplete update. Running the updater again offers code recovery after verifying the saved backup. Successful recovery restores the pause preference and clears both blocking markers. An orphan marker with no pending code journal can be recovered under the service lock. `--dry-run` reports the required recovery without writing files or stopping a service. It does not bypass file hashes or remove unrelated locks. Keep backup/journal files intact while recovering.

The updater cannot guarantee recovery from every power-loss, disk-full, antivirus lock, damaged filesystem or manually altered backup condition. If it reports an integrity/permissions conflict, stop and read the console rather than repeatedly replacing files. Backups contain private path/history information and should be treated as private.

## Security and release trust

SHA-256 comparisons detect accidental corruption and mismatched content. They are **not** a digital signature: an attacker who supplies a whole replacement ZIP can supply replacement hashes. Trust the source of the build, not the presence of a manifest alone. The updater validates file structure but ultimately installs Python/JavaScript code you have chosen to run.

No background network updater, unsigned code download at launch, registry edit, Windows security-policy weakening, driver change or admin elevation is performed by this updater. Local setup scripts separately use their existing per-process PowerShell execution policy option; their dependency installation prompts remain explicit.

The supplied BAT is a standard-library Python bootstrap and uses the existing private interpreter when available, with an installed Python launcher/interpreter fallback. Its command block is parsed before Python runs so replacing the BAT's on-disk file does not require reading the new tail mid-command. This Windows-specific behavior has been reviewed in source, **not execution-tested on Windows in the build environment**.

## V0.4 → V0.5 compatibility

The same updater and Build/Backups locations apply. The seven Media Privacy tools add options and known Black Scripting operations without replacing existing scripts, presets, settings or history. Source, output and asset snapshots preserve pipeline input identity. Rollback retains newer records and generated outputs, but V0.4 cannot execute V0.5-only tools. Finish or cancel all queued work before changing versions.

Core dependencies and the updater are unchanged. Content Credentials inspection is optional: install the pinned reader separately through Environment or `scripts\Install Media Privacy.bat`. No media, model or dependency upload/download is part of the code update. GitHub names the standalone asset `Update.FORGE.bat`; save it as `B:\Forge\Update FORGE.bat`.

## V0.3 → V0.4 compatibility

Use the same Build folder and updater. Core dependency pins and updater code are unchanged; no reinstall is needed. V0.4 adds settings defaults and separate `black_scripting`/`generator_presets` SQLite key-value entries, and projects legacy `workflows` without changing them. Old valid worker caps become manual limits when the old settings lack a concurrency mode. All effective limits are bounded to five. New installs default to Auto with a ceiling of five.

Code rollback keeps current data and files. V0.3 ignores the new separate script/generator stores; it continues to see its original workflows. New QR types, metadata profiles and Black Scripting runs use V0.4 options, so finish/cancel all work before rollback and use the original legacy definitions in older code. Code rollback does not promise to execute newer jobs or expose new presets. The updater's separate SQLite snapshot is available for deliberate recovery; it is never silently substituted for current user data.
