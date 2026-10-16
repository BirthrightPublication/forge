# V0.3 scope and development continuity

Target: Windows 11, using a local browser at the existing stable loopback address. No hardware specification is required for this update-focused release. The supplied V0.2 archive is the baseline; V0.1 and the byte-identical standalone V0.2 updater are comparison inputs. The empty Git checkout was populated from V0.2 and committed locally before implementation.

## Inspected baseline

The application already provides Python/FastAPI, SQLite, dependency-free HTML/CSS/JavaScript, remembered browser authorization, native launchers, isolated queue workers, resource limits, chained media workflows and seven production tool pages. Optional ASR, speaker models and downloading engines remain separate installs. The existing ZIP updater already verifies manifest hashes, protects local data, creates backups and supports code-only rollback.

Missing capabilities in the original eight-priority proposal include a secure custom script importer, general folder monitoring/scheduling, publisher-signed distribution, managed dependency rollback and online terminology verification. V0.2's model/transcript/diagnostics features must not be presented as those missing capabilities.

## Selected V0.3 implementation

1. Preserve and test the supplied baseline.
2. Share read-only archive, target and backup preflight between Studio and the native updater.
3. Add a manual local build review panel to the existing Environment page, without redesigning Studio.
4. Offer fixed Windows console actions with explicit consent, idle checks and final confirmation.
5. Repair interrupted-update/rollback handling and persistent queue-pause recovery.
6. Run existing and new regressions, real browser checks and disposable upgrades/rollbacks from both supplied baselines.
7. Package complete source, manifest/checksum, standalone updater, update/recovery guidance and validation limits.

No user media is uploaded. Package inspection does not import or execute the archive's code. Code smoke checks happen only after the approved native installation, with automatic rollback on failure. Integrity hashes do not establish publisher authenticity; digital signatures require a separately designed key/distribution process.

## Risks and acceptance

Code-only rollback preserves the current database, rather than replacing user history with an old snapshot. Newer settings may still need adjustment when running older code. Backup/journal tests reduce update risk without certifying every Windows filesystem, antivirus, locked-file, disk-full or power-loss condition. Actual BAT/PowerShell launch, native console, Explorer and drive-letter behavior need Windows acceptance. No optional GPU/model/website inference result is inferred from this release's tests.

## Smaller subsequent releases

The next recommended increment is a reviewed custom-tool manifest and registry/import flow, followed by constrained queue execution after explicit approval. Dependency/environment planning can then expand around those module requirements. Folder monitoring and scheduling should follow stable module permissions and persistence. Transcription improvements, historical analytics and further interface refinements can ship independently with their own focused validation.

Those are proposals for subsequent work, not features implemented by V0.3. Maintain the existing architecture and original files, retain raw transcripts and user data, avoid unrestricted browser commands, and report executed tests separately from untested Windows/hardware paths. Cloud tasks already have isolated checkouts; use the existing checkout and do not create a Git worktree unless requested.
