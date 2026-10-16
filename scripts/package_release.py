"""Build a local, manifest-verified FORGE release ZIP; never package local data or environments.
Usage from the repository: python scripts/package_release.py --output B:\\Forge\\Build
Edit forge/__init__.py and VERSION.txt deliberately before publishing a NEW release.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from update import APP_DIRS, ROOT_FILES, safe_relative, installed_version, inspect_release

ROOT=Path(__file__).resolve().parents[1]

def build(output: Path):
    version=installed_version(ROOT)
    if (ROOT/'VERSION.txt').read_text().strip()!=version:raise ValueError('VERSION.txt does not match forge/__init__.py')
    files={}
    for file in sorted(ROOT.rglob('*')):
        if not file.is_file():continue
        rel=file.relative_to(ROOT).as_posix();parts=file.relative_to(ROOT).parts
        if '__pycache__' in parts or '.pytest_cache' in parts or file.suffix=='.pyc':continue
        if len(parts)==1:
            if rel not in ROOT_FILES or rel=='release-manifest.json':continue
        elif parts[0] not in APP_DIRS:continue
        if file.is_symlink():raise ValueError('Do not package source symlinks: '+rel)
        safe_relative(rel)
        files[rel]=file.read_bytes()
    manifest={'app':'FORGE','format':1,'version':version,'minimum_from':'0.1.0',
        'note':'Checksums detect corruption, not publisher authenticity. Original scripts already present are never replaced.',
        'files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
    output.mkdir(parents=True,exist_ok=True)
    short=version[:-2] if version.endswith('.0') else version
    archive=output/f'FORGE-v{short}.zip';prefix=f'FORGE-v{short}/'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for rel,data in files.items():z.writestr(prefix+rel,data)
        z.writestr(prefix+'release-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    inspect_release(archive)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest+'  '+archive.name+'\n',encoding='utf-8')
    print(str(archive));print('SHA256 '+digest);print(str(len(files))+' application files plus manifest')
    return archive

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'Build')
    build(parser.parse_args().output.resolve())
