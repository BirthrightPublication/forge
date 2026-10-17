# Exact updater fixtures

These two files preserve the user-supplied bytes for native Windows acceptance:

| File | SHA-256 |
| --- | --- |
| `Update-Application.ps1` | `9d499a9122f179e5ae2d1f19393be84fefeac2d013f5d70fcf7ed3b5b59895db` |
| `Update FORGE.bat` | `8eabdd2b4ebff057dfccdbdf646899ce5504d37bc021cb937867ff7c3645cfef` |

The BAT upload contains literal `\r\n` text, rather than physical line endings.
It is preserved as an unchanged installation sentinel; the acceptance harness
executes the exact PowerShell updater directly and does not claim to test this
BAT launcher.

Run `tests/windows_release_acceptance.ps1` using Windows PowerShell 5.1. Supply
`-Archive`, `-Checksum`, a new `-Output` folder beneath `TEMP` or `RUNNER_TEMP`,
and optionally `-PythonExecutable`. Install the selected release's
`requirements-core.txt` and `httpx==0.28.1` into that Python interpreter first.
The output is `windows-acceptance.json`, per-case logs, and an installed-app
smoke report. Python imports and the real QR worker use the application files
installed by the updater and fresh disposable user data.

Only GitHub release metadata, asset downloads and confirmation input are mocked.
The updater's asset selection, SHA-256 verification, extraction, protected-path
and junction checks, backups and copies run unchanged. The harness never reads
or changes an owner's installation. These test fixtures must not be included in
the Windows installation payload.

Each child process receives its own canonical `TEMP` and `TMP` directory beneath
the disposable scenario folder. The report includes file probes for both the
inherited TEMP and the isolated TEMP, using the supplied updater's exact
relative-path calculation. It records redacted path forms, normalization lengths
and the resulting relative filename, and requires the isolated result to match.

This condition matters: the supplied updater derives relative paths by removing
`$source.Length` characters from an enumerated filename. When TEMP uses a short
8.3 alias and Windows expands that alias during enumeration, the differing path
lengths can leave an extra `app` component. Native CI observed the updater report
success while placing all 143 payload files under `installation/app/`, leaving
the old root application in place. The probe exposes the inherited path behavior;
isolating TEMP does not fix the original updater. Acceptance proves installation
with canonical TEMP and does not establish compatibility with short TEMP aliases.
Every payload file must still exist at the expected root location and match its
archive SHA-256; no extra directory or missing file is accepted.
