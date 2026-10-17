# Commercial dependency review — V0.4

Reviewed 16 October 2026 against installed distribution metadata/license files and the linked publisher sources. This source release does not bundle Python packages, FFmpeg binaries, GPU runtimes or model weights. The updater does not install them. A future binary distribution must inventory its exact resolved dependencies and retain the applicable notices; broad optional version ranges can resolve differently later.

| Component reviewed | License / commercial distribution implications |
|---|---|
| FORGE application source | MIT (`LICENSE.txt`); retain its copyright/license notice. Uploaded original scripts and third-party assets are outside this grant. |
| Python / Tcl/Tk | PSF and Tcl/Tk terms apply to redistributed interpreters/dialog runtimes; retain their notices. Python 3.11–3.13 is supported. |
| FastAPI 0.128.2 | MIT. |
| Uvicorn 0.48.0; Starlette 0.50.0 | BSD-3-Clause; preserve notices and non-endorsement condition. |
| Pillow 12.3.0 | MIT-CMU expression; its wheel's bundled image libraries have separate notices. Preserve the complete distribution notices. |
| qrcode 8.2 | Installed LICENSE includes BSD-3-Clause plus inherited MIT QR algorithm notices. QR Code is a DENSO WAVE trademark. Do not imply publisher endorsement. |
| psutil 7.2.2 | BSD-3-Clause. |
| Pydantic 2.14.1 / pydantic-core 2.50.1, AnyIO 4.15.1, h11 0.16.0, annotated-types 0.8.0, annotated-doc 0.0.5 | MIT metadata reviewed. Actual future versions and bundled Rust components need a resolved-package inventory. |
| Click 8.5.0; typing_extensions 4.16.0 | BSD-3-Clause; PSF-2.0 respectively. |
| faster-whisper 1.2.1 / CTranslate2 4.8.3 | MIT code licenses. Optional PyAV/FFmpeg and accelerator dependencies have their own terms; model-weight terms are separate. |
| yt-dlp 2026.8.19 | Source Unlicense. Some packaged executables combine dependencies with different terms, including GPL-covered components; do not assume an arbitrary binary is entirely Unlicense. Website/account/content rights are separate. |
| pyannote.audio 4.0.7 | Publisher source MIT. Optional torch, torchaudio, NumPy, SciPy and other resolved dependencies require their own notices. Community-1 weights and access conditions are separate; FORGE does not bundle or activate them. |
| FFmpeg / FFprobe | Build-dependent LGPL/GPL/nonfree licensing. The Linux **validation host** uses 7.1.5 with `--enable-gpl` and x264/x265, reporting GPLv2-or-later. This does not identify a user's Windows binary. Before bundling, inspect the exact build configuration, supply required notices/corresponding source or relinking materials as applicable, and exclude non-redistributable `--enable-nonfree` builds. Calling a user-installed executable does not grant rights to redistribute it. Codec patent obligations are separate. |
| NVIDIA CUDA/cuDNN/drivers | Proprietary vendor terms and specific redistribution conditions; not covered by FORGE's MIT license. No vendor binaries are included here. |
| Whisper / converted Whisper weights / speaker models | Inspect the model card and exact weight repository/revision independently of inference-library licenses. No weights are bundled. Do not infer universal commercial permission from a library's MIT license. |
| Development-only tools | pytest (MIT), HTTPX (BSD-3-Clause), Playwright (Apache-2.0), NumPy (BSD-3-Clause plus included notices), zxing-cpp (Apache-2.0) and Chromium's multiple licenses. They are not needed for the production core. Chromium and the QR decoder are validation tools, not shipped application binaries. |

The current permissive core dependencies permit commercial use subject to their license conditions. GPL media binaries, proprietary GPU components, exact model-weight licenses and third-party asset ownership need particular attention when choosing a future bundled product. The uploaded reference screenshot/custom icon were not available in this session; the interface uses existing FORGE branding and local drawn SVG icons, with no external font/image download.

## Verified publisher references

- [qrcode v8.2 license](https://github.com/lincolnloop/python-qrcode/blob/v8.2/LICENSE)
- [Pillow license](https://github.com/python-pillow/Pillow/blob/12.3.0/LICENSE)
- [faster-whisper v1.2.1 license](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/LICENSE)
- [CTranslate2 license](https://github.com/OpenNMT/CTranslate2/blob/master/LICENSE)
- [yt-dlp 2026.08.19 license](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/LICENSE) and [licensing notes](https://github.com/yt-dlp/yt-dlp#license)
- [pyannote.audio 4.0.7 license](https://github.com/pyannote/pyannote-audio/blob/4.0.7/LICENSE)
- [Community-1 model card](https://huggingface.co/pyannote/speaker-diarization-community-1)
- [FFmpeg legal and redistribution checklist](https://ffmpeg.org/legal.html)
- [CUDA EULA](https://docs.nvidia.com/cuda/eula/index.html)

No license-server request is made by V0.4. Dependency metadata discovery, optional engine installation and model acquisition remain separate explicit functionality.
