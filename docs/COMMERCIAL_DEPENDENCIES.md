# Commercial dependency review — V0.5 release candidate

The V0.4 core review is retained below. V0.5 additions were checked on 16 October 2026 against the official tagged C2PA Python and Chromaprint source/license files linked below. This candidate review is not a declaration of release publication or legal certification of every possible resolved binary.

FORGE's existing MIT source license is unchanged. The source package does not bundle Python wheels, FFmpeg binaries, GPU runtimes or model weights; the updater does not install them. The optional Media Privacy installer obtains a pinned C2PA package only after explicit confirmation. A future binary distribution must inventory its exact resolved dependencies and retain applicable notices. Optional version ranges and FFmpeg build choices can resolve to different obligations.

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
| c2pa-python 0.38.0 — optional V0.5 Content Credentials reader | Official tagged `pyproject.toml` declares **MIT OR Apache-2.0**, and both license texts are present in the tagged source. Follow the chosen license's conditions, including applicable copyright/license/NOTICE and patent provisions. Native libraries and transitive dependencies inside the exact installed wheel need their own inventory; the top-level dual license is not a blanket grant for everything an arbitrary distribution bundles. FORGE pins this supported SDK and uses it only for local reading/validation. |
| Chromaprint — optional capability of the user's FFmpeg | The reviewed official **v1.5.1** `LICENSE.md` says Chromaprint's own code is MIT, but included FFmpeg portions are LGPL 2.1, so the combined project should be treated as **LGPL 2.1**. Its external FFT library can impose additional terms. This reviewed source version is not a claim about the version compiled into every user's FFmpeg. FORGE detects the installed FFmpeg Chromaprint muxer; it does not bundle Chromaprint, install an AcoustID client, or contact an acoustic lookup service. |
| FFmpeg / FFprobe | Build-dependent LGPL/GPL/nonfree licensing. The Linux **validation host** uses 7.1.5 with `--enable-gpl` and x264/x265, reporting GPLv2-or-later. This does not identify a user's Windows binary. Before bundling, inspect the exact build configuration, supply required notices/corresponding source or relinking materials as applicable, and exclude non-redistributable `--enable-nonfree` builds. Calling a user-installed executable does not grant rights to redistribute it. Codec patent obligations are separate. |
| NVIDIA CUDA/cuDNN/drivers | Proprietary vendor terms and specific redistribution conditions; not covered by FORGE's MIT license. No vendor binaries are included here. |
| Whisper / converted Whisper weights / speaker models | Inspect the model card and exact weight repository/revision independently of inference-library licenses. No weights are bundled. Do not infer universal commercial permission from a library's MIT license. |
| User-selected fonts, logos, clips and media | Font presets use available local runtime/system fonts; custom TTF/OTF files, image marks and audible clips are selected by the user. FORGE supplies no new ownership or redistribution grant for them. A font's rasterization/embedding permissions and a media owner's authorization remain separate from FORGE's MIT code license. No external font/image service is used by the Studio. |
| Development-only tools | pytest (MIT), HTTPX (BSD-3-Clause), Playwright (Apache-2.0), NumPy (BSD-3-Clause plus included notices), zxing-cpp (Apache-2.0) and Chromium's multiple licenses. They are not needed for the production core. Chromium and the QR decoder are validation tools, not shipped application binaries. |

The permissive core and dual-licensed C2PA source permit commercial use subject to their conditions. Chromaprint/FFT choices, GPL media binaries, proprietary GPU components, exact model-weight licenses and third-party asset ownership need separate attention in a bundled product. C2PA credentials describe signed assertions; processing them grants no ownership, authentication endorsement or license to the underlying media. The referenced custom visual assets were not available in this session; the interface uses existing FORGE branding and local SVG icons, without an external font/image download.

V0.5's built-in spectral and sampled pHash/dHash/color methods are implemented in FORGE source using the existing runtime and Pillow. The application does not add a separate perceptual-hash Python package, hosted fingerprint database or mandatory recognition-service subscription. Optional Chromaprint absence is reported as a capability limitation, not repaired by an unapproved binary download.

## Verified publisher references

- [qrcode v8.2 license](https://github.com/lincolnloop/python-qrcode/blob/v8.2/LICENSE)
- [Pillow license](https://github.com/python-pillow/Pillow/blob/12.3.0/LICENSE)
- [faster-whisper v1.2.1 license](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/LICENSE)
- [CTranslate2 license](https://github.com/OpenNMT/CTranslate2/blob/master/LICENSE)
- [yt-dlp 2026.08.19 license](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/LICENSE) and [licensing notes](https://github.com/yt-dlp/yt-dlp#license)
- [pyannote.audio 4.0.7 license](https://github.com/pyannote/pyannote-audio/blob/4.0.7/LICENSE)
- [Community-1 model card](https://huggingface.co/pyannote/speaker-diarization-community-1)
- [c2pa-python v0.38.0 package metadata](https://github.com/contentauth/c2pa-python/blob/v0.38.0/pyproject.toml), [MIT license](https://github.com/contentauth/c2pa-python/blob/v0.38.0/LICENSE-MIT), and [Apache-2.0 license](https://github.com/contentauth/c2pa-python/blob/v0.38.0/LICENSE-APACHE)
- [Chromaprint v1.5.1 licensing notes](https://github.com/acoustid/chromaprint/blob/v1.5.1/LICENSE.md)
- [FFmpeg legal and redistribution checklist](https://ffmpeg.org/legal.html)
- [CUDA EULA](https://docs.nvidia.com/cuda/eula/index.html)

No license-server request is introduced by V0.5. Media and fingerprints are processed locally. C2PA network manifest fetching, online revocation, remote identity resolution and trust refresh are disabled in the adapter. Dependency metadata discovery, approved optional package installation and consented model acquisition remain separate explicit functionality. The absence of network validation also limits what can be concluded about current credential trust or revocation.
