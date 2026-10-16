# FORGE V0.2 local build updates

## Your folder layout

```text
B:\Forge\
    Update FORGE.bat        ← download this bootstrap once for the V0.1 transition
    Build\
        FORGE-v0.2.zip      ← leave new release ZIPs here, not extracted
    Backups\               ← updater-created code backups and database snapshots
    ... active application files, or ONE existing extracted FORGE folder ...
```

The updater supports application files directly under the chosen root, or exactly one extracted `FORGE*` child with `server.py` and `forge/__init__.py`. It does not move that folder, relocate `.venv`, rebuild shortcuts, or guess between multiple active copies. A nested folder may retain its original `FORGE-v0.1` name after its code becomes V0.2; that is intentional, to avoid breaking paths.

For the first upgrade, place the standalone `Update FORGE.bat` in `B:\Forge`. It contains the updater source, so it works even though V0.1 had no updater. You do not need to extract files out of the release ZIP manually. Future compatible releases use the same action; the package also includes an updater beside its application launcher.

## Normal procedure

Finish or cancel all queued/running work and stop submitting jobs. Double-click the updater. It validates available `FORGE-v*.zip` packages in `Build`, chooses the highest newer semantic version, and displays that choice. A later file modification date is not treated as a newer version. Two different archives claiming the same newest version cause a refusal; identical duplicate downloads are allowed.

Press Enter to update, Q to cancel, or R for the available code rollback. No build is downloaded by the updater and no dependency installation is performed. Keep the console open until it finishes.

The updater verifies every manifest-listed file, rejects unlisted/missing/unsafe entries, checks embedded source compilation, backs up files that will change, snapshots the SQLite database through SQLite's backup API, then replaces application files. Existing original scripts are skipped even if the published reference copy differs. Local environments, tools, data, Build, Backups and model directories are protected.

If FORGE is running, the updater refuses to stop it while any queued, active or finalizing job exists. Its check includes older records outside the UI's 500-row history window. V0.2+ can freeze new job admission atomically while preparing maintenance. The initial V0.1 transition lacks that endpoint, so leave the old browser tabs alone during the update. The OS service lock must be available before copying; no process is forcibly killed.

After copying and byte verification, the private Python environment imports the application and constructs it with disposable data. Failure triggers restoration of the prior code. Success retains backups, restores the prior queue-pause preference, and invokes the launcher. **This smoke check does not prove Windows native dialogs, CUDA inference, all codecs, model compatibility or live downloads.**

Close/reload old V0.1 tabs and confirm the V0.2 badge. Open from the launcher once in your browser profile, then test the plain bookmark in another tab. A stopped server still needs the launcher; a bookmark cannot create a Windows process.

## Rollback and interruption

R restores the latest registered code backup. Only changed application files are restored; files introduced by that update are removed. Current user data and completed outputs are not replaced by an older snapshot. The database snapshot is retained under the backup directory for deliberate recovery rather than being applied automatically.

New V0.2 workflow settings or tool options may not be understood by V0.1 after a code-only rollback. Older history and baseline preferences were checked in the integration test; arbitrary newer combinations are not a promise of downgrade compatibility. Keep the separate snapshot, and do not overwrite the current database casually to repair a UI preference.

An unfinished update records `.forge-update-state.json`. Opening FORGE is blocked while a journal/maintenance marker indicates an incomplete update. Running the updater again offers code recovery after verifying the saved backup. It does not bypass file hashes or remove unrelated locks. Keep backup/journal files intact while recovering.

The updater cannot guarantee recovery from every power-loss, disk-full, antivirus lock, damaged filesystem or manually altered backup condition. If it reports an integrity/permissions conflict, stop and read the console rather than repeatedly replacing files. Backups contain private path/history information and should be treated as private.

## Security and release trust

SHA-256 comparisons detect accidental corruption and mismatched content. They are **not** a digital signature: an attacker who supplies a whole replacement ZIP can supply replacement hashes. Trust the source of the build, not the presence of a manifest alone. The updater validates file structure but ultimately installs Python/JavaScript code you have chosen to run.

No background network updater, unsigned code download at launch, registry edit, Windows security-policy weakening, driver change or admin elevation is performed by this updater. Local setup scripts separately use their existing per-process PowerShell execution policy option; their dependency installation prompts remain explicit.

The supplied BAT is a standard-library Python bootstrap and uses the existing private interpreter when available, with an installed Python launcher/interpreter fallback. Its command block is parsed before Python runs so replacing the BAT's on-disk file does not require reading the new tail mid-command. This Windows-specific behavior has been reviewed in source, **not execution-tested on Windows in the build environment**.
