# Dependencies and reference documentation

FORGE's own application source is included. Dependency code and model weights are not bundled. Their licenses remain those of their publishers; optional GPU driver/runtime components are not represented as open-source FORGE code.

## Core

`requirements-core.txt` retains the direct pins used for V0.1/V0.2 validation. These are tested pins, not a claim to be the newest release. Python 3.11–3.13 (64-bit) with Tcl/Tk is the Windows setup target. Tcl/Tk supplies the native file dialogs. The backend uses FastAPI/Uvicorn, Pillow, qrcode and psutil, plus the standard library. Transitive dependencies are resolved by pip rather than fully hash-locked across platforms.

## Optional engines

- Transcription: `requirements-transcription.txt`, installed explicitly into `.venv`. faster-whisper supplies recognition/timing and PyAV audio decoding. Cached models use FORGE's local model directory. CPU is supported; CUDA selection needs compatible external drivers/runtime libraries. Runtime enumeration is not an inference test.
- Downloads: `requirements-downloads.txt`, installed explicitly into `.venv`. FFmpeg/FFprobe and an appropriate JS runtime may be needed. No website compatibility or account access is guaranteed. User browser cookies are not automatically harvested.
- Speaker labels: `requirements-diarization.txt`, installed into separate `.diarization-venv`. The complete local Community-1 model must be obtained from its publisher. No account token is requested by FORGE. This release's speaker runner is CPU/offline and disables pyannote metrics.
- Media transforms: existing `ffmpeg.exe`/`ffprobe.exe` on PATH or explicit Environment paths. The updater preserves user-supplied tools and does not download executables.
- Development: pytest, HTTPX and Playwright in the development requirements. Some signal tests use NumPy; this is not required for the core Studio except where an optional engine supplies it.

The optional Windows installers contact PyPI only after confirmation. They do not silently run on ordinary startup. Application ZIP updates do not install or upgrade packages. Broad managed dependency rollback and signed automatic updates remain future work.

## Publisher documentation consulted

Technical implementation was checked against these primary/reference documents rather than a recommendation list of third-party applications:

```text
https://docs.python.org/3/library/socket.html
https://learn.microsoft.com/en-us/windows/win32/winsock/using-so-reuseaddr-and-so-exclusiveaddruse
https://starlette.dev/responses/
https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie
https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
https://github.com/SYSTRAN/faster-whisper
https://github.com/yt-dlp/yt-dlp
https://github.com/pyannote/pyannote-audio
https://huggingface.co/pyannote/speaker-diarization-community-1
```

See the model publisher for current access and licensing conditions. FORGE neither circumvents those conditions nor bundles model credentials. The test report distinguishes source/API-contract checks from actual optional engine execution.
