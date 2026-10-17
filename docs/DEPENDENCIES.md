# Dependency provenance

FORGE's own source, tests, documentation and raster icon are included. No third-party package binaries, model weights, Windows font files, CUDA libraries, FFmpeg builds or JavaScript runtimes are redistributed in this ZIP. Setup resolves/install dependencies separately and their own licensing remains applicable. Do not infer that local processing means every operating-system component is open source.

## Direct core packages used for validation

| Package | Version |
|---|---|
| FastAPI | 0.128.2 |
| Uvicorn | 0.48.0 |
| qrcode | 8.2 |
| Pillow | 12.3.0 |
| psutil | 7.2.2 |

Core requirements pin these direct dependencies, not every transitive dependency or wheel hash. `installed-versions.txt`, written by the user's explicit installer run, records that computer's actual resolved environment. The build's tested direct versions are not necessarily the newest available releases.

Optional ASR requirement: faster-whisper 1.2.1, with its runtime dependencies resolved by pip. Model weights and CUDA are separate. Optional download requirement: stable `yt-dlp[default]` in the 2026 release series, selected on an explicit installer run. No optional ASR/download package was installed in the build environment; adapter contracts were tested with substitutes, not represented as full inference/live-download tests.

Primary project information:
- https://pypi.org/project/fastapi/
- https://pypi.org/project/uvicorn/
- https://pypi.org/project/qrcode/
- https://pypi.org/project/Pillow/
- https://pypi.org/project/psutil/
- https://pypi.org/project/faster-whisper/
- https://pypi.org/project/yt-dlp/

FFmpeg's build-specific license depends on its compiled components. Obtain builds through sources linked by the FFmpeg project and retain their license information. System typography references Yu Mincho by CSS font-family only; no font files are included.
