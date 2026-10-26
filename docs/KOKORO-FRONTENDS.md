# Kokoro language frontend installation

Kokoro 0.9.4 exposes nine official language codes: American/British English, Spanish,
French, Hindi, Italian, Brazilian Portuguese, Japanese and Mandarin Chinese. Voice
embeddings and neural weights remain the original revision-verified Kokoro files.

FORGE installs the corresponding text frontends in the isolated Kokoro environment.
It does not change the Whisper or core Python environment. Installer activation
requires actual offline phonemization using the original
`KPipeline(lang_code=..., model=False)` for every official code. Readiness is recorded
per language; checking only a system eSpeak DLL name is insufficient on Windows.
Misaki configures the library and data supplied by `espeakng-loader` itself.

## Native packages and dictionary data

- English and eSpeak languages retain `misaki[en]==0.9.4`, pinned spaCy and its
  English model, `espeakng-loader==0.2.4`, `phonemizer-fork==3.3.2` and
  `num2words==0.5.14`.
- Japanese retains Misaki's original `JAG2P`/Cutlet implementation with
  `fugashi==1.5.2`, `jaconv==0.5.0`, `mojimoji==0.0.13` and the complete local
  `unidic-lite==1.0.8` dictionary. No runtime dictionary download is needed.
  Misaki imports `pyopenjtalk` even in Cutlet mode. The original PyPI pyopenjtalk
  has no native Windows wheel, so the compatible `pyopenjtalk-plus==0.4.1.post10`
  wheel supplies that import without requiring an owner-installed C++ compiler.
  This package includes its own OpenJTalk dictionary; the default Kokoro Cutlet
  phonemization path is retained.
- Chinese installs `cn2an==0.5.24`, `jieba==0.42.1`, `ordered-set==4.1.0`,
  `pypinyin==0.55.0` and `pypinyin-dict==0.9.0` for the original Misaki `ZHG2P`.

Native packages require publisher binary wheels. Two packages are published only
as pure-Python/data source archives. Their setup scripts were inspected: they only
package Python and dictionary files, with no compiler or secondary download.
The installer retrieves only these exact artifacts, verifies their SHA-256 before
running a build, and uses pinned binary `setuptools==80.9.0` and `wheel==0.45.1`
with `--no-deps --no-build-isolation`. The built wheel must have a pure-Python tag
and cannot contain native executable extensions. No general source-build fallback
is enabled for other packages.

| Package | Approved source artifact | SHA-256 |
| --- | --- | --- |
| jieba 0.42.1 | [PyPI source](https://files.pythonhosted.org/packages/c6/cb/18eeb235f833b726522d7ebed54f2278ce28ba9438e3135ab0278d9792a2/jieba-0.42.1.tar.gz) | `055ca12f62674fafed09427f176506079bc135638a14e23e25be909131928db2` |
| unidic-lite 1.0.8 | [PyPI source](https://files.pythonhosted.org/packages/55/2b/8cf7514cb57d028abcef625afa847d60ff1ffbf0049c36b78faa7c35046f/unidic-lite-1.0.8.tar.gz) | `db9d4572d9fdd4d00a97949d4b0741ec480ee05a7e7e2e32f547500dae27b245` |

The immutable environment's `forge-frontend-artifacts.json` records source URLs,
source hashes/sizes, built wheel hashes and build-tool pins. `forge-runtime.json`
retains that record and the actual frontend probe results. New requirement pins
create a new environment fingerprint; prior environments remain intact.

## Verification and license scope

The reviewed source-wheel build and actual Spanish/French/Hindi/Italian/Portuguese,
Japanese and Chinese phonemization were exercised in a separate Linux environment.
That is text-frontend verification, not neural speech or Windows acceptance.
The opt-in Kokoro acceptance harness adds real offline synthesis with one installed
official voice per language code when run with `--scope full`. Non-silent output
establishes the code path; it is not a pronunciation or language-quality benchmark.

These packages retain their own notices. PyPI identifies pyopenjtalk-plus and
jaconv as MIT, fugashi as MIT/BSD-3-Clause, mojimoji as Apache and Chinese wrappers
as MIT. UniDic's dictionary includes separate BSD/GPL/LGPL choice notices; the
unidic-lite packaging wrapper is MIT. Phonemizer/eSpeak components include GPL
obligations. This document does not relicense those components or establish combined
commercial redistribution rights. The application ZIP does not bundle their
binaries or dictionaries; explicitly installed environments retain their full
publisher notices for the separate dependency/license review.
