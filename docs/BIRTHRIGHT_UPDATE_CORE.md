# Birthright Update Core 0.1

The shared package is `forge/birthright_update`. It imports no FORGE services,
web framework, application database, authentication system or UI. Its location
inside FORGE preserves compatibility with already distributed updater allowlists.
Atlas and PathFinder can adopt the package unchanged through a separately versioned
distribution after that repository/package boundary is established. No Atlas or
True Engine code was copied into this project. Atlas's interface is still under
development; this implementation follows the owner's charcoal, warm-white and
restrained-gold requirements rather than claiming to reproduce an inspected Atlas UI.

## Interfaces

- `Asset`, `Release`: immutable publisher metadata, digest, byte size, platform,
  architecture, package contract, release date, channel and authenticity evidence.
- `NetworkPolicy`, `Transport`: explicit HTTPS publishers, separate redirect
  allowlist, timeouts, metadata/asset size ceilings, measured transfer callbacks,
  SHA-256 verification, atomic publication and refusal to overwrite a different file.
- `SignedDiscovery`: background-callable discovery, persistent cache, exponential
  offline backoff and signed-sequence replay protection. No account or service login.
- `InstallationAdapter`: preflight, quiesce, install, validate, rollback, recover
  and restart. `apply_update` requires approval before side effects. A failed
  recovery must not restart an incomplete installation.
- `LocalUpdaterAdapter`: FORGE's implementation delegates application backup,
  protected data boundaries, locking and recovery journals to its existing updater.
  The external maintenance host waits for the exact server process to exit and
  restarts once all selected work has finished.

Each consuming application supplies its own adapter and private data boundaries.
The shared core must never guess which directories contain an application's data.
Model and Python dependency adapters remain FORGE-specific. No dependency resolver
or model publisher can authorize a new application version.

## Signed manifest contract v1

An envelope has exactly `signed` and `signatures` fields. The signed object has:

| Field | Contract |
| --- | --- |
| `schema` | Integer `1` |
| `application` | Stable identifier, e.g. `birthright.forge` |
| `channel` | Application-pinned channel, normally `stable` |
| `version` | Three numeric version components |
| `sequence` | Monotonically increasing positive integer per application/channel |
| `published`, `expires` | UTC `YYYY-MM-DDTHH:MM:SSZ`; maximum validity 90 days |
| `notes` | Concise release notes as plain text |
| `assets` | One to twenty unique, case-insensitive asset names |

Each asset has exactly `name`, `url`, `sha256`, `size`, `platform`, `architecture`
and `format`. Platforms: `windows`, `linux`, `macos`; architectures: `x64`, `arm64`;
formats: `app-zip-v1` (root `app/`) or `source-zip-v1` (application-specific verified
source adapter). URLs must also pass the consuming application's publisher policy.
Filenames permit ASCII letters, digits, dot, underscore and hyphen, with no `..`.

Signatures have `key_id`, `algorithm: "ed25519"`, and a standard-base64 `signature`.
The signed bytes are the UTF-8 prefix `Birthright-Update-Manifest-v1\n` followed by
the canonical signed object: recursively sorted ASCII object keys, no whitespace,
UTF-8 strings without ASCII escaping, and integers within ±(2^53−1). Floats are
forbidden. This is a defined restricted canonical encoding, not general RFC 8785.
Verification uses the reviewed `cryptography` Ed25519 implementation; installations
enabling this transport must include that dependency. The tests pin `50.0.2`.

Public keys are pinned by the application or an independently approved trust
configuration. Manifests cannot introduce their own trusted keys. Rotation requires
an approved application release or a separately reviewed trust-root mechanism;
multiple signatures support an overlap period. Private signing keys never belong
in the source repository, application ZIP, workflow log or browser.

Persist the highest accepted sequence even when the user rolls installed code back.
An expired cached manifest can be displayed offline but must be reverified before
installation. Unknown keys, altered content, an unexpected application/channel,
expired/future metadata and sequence rollback all fail closed. Applications must
serialize concurrent discovery calls and keep their trust state outside code backups.

## Current publication mode

FORGE currently uses the existing public GitHub release plus matching SHA-256
assets. These checks detect corruption; they are **not publisher signatures**.
No production signing key or signed feed has been supplied, so signed discovery is
implemented and tested as an adoption contract, not falsely enabled for current
GitHub releases. Switching a publisher to signed-required mode must never silently
fall back to unsigned metadata when a signature or network request fails.

## Visual integration

The Updates header icon replaces the visible Ctrl+K label; the shortcut continues
to focus search. The gold badge counts real compatible updates/repairs, excluding
optional components that have never been installed. The compact dialog shows
current/available versions, date, changelog, Update and Later. FORGE adds model,
dependency, utility and GPU categories. The existing Environment retains external
update and recovery controls. No new application version installs without approval.
