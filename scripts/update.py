"""FORGE offline release updater. Standard library only; no downloaded code is run to validate a ZIP.
The same source is embedded in Update FORGE.bat for external recovery and upgrades from V0.1 onward.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

APP_DIRS={'forge','web','scripts','tests','docs','original_scripts'}
PROTECTED={'build','backups','.venv','.diarization-venv','tools','data','models','staging','work','exports','config'}
ROOT_FILES={'.gitignore','CHANGELOG.md','LICENSE.txt','README.md','START HERE.txt','VERSION.txt','release-manifest.json',
 'Open FORGE.bat','Stop FORGE.bat','Setup FORGE.bat','Update FORGE.bat','Repair Start Shortcut.bat','Run Tests.bat',
 'Install Downloads.bat','Install Transcription.bat','Install Speaker Labels.bat','launcher.py','server.py','pytest.ini',
 'requirements-core.txt','requirements-dev.txt','requirements-transcription.txt','requirements-downloads.txt','requirements-diarization.txt'}
MAX_TOTAL=250*1024*1024
class UpdateError(RuntimeError): pass

def version(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d+\.\d+\.\d+',value):
        raise UpdateError('Invalid release version.')
    return tuple(int(x) for x in value.split('.'))

def safe_relative(value):
    if not isinstance(value,str) or not value or re.search(r'[\\<>:"|?*\x00-\x1f]',value):
        raise UpdateError('Unsafe archive path.')
    p=PurePosixPath(value)
    if p.is_absolute() or str(p)!=value or any(x in {'..','.'} or x.endswith((' ','.')) for x in p.parts):
        raise UpdateError('Unsafe archive path: '+value)
    if any(part.lower() in PROTECTED for part in p.parts):
        raise UpdateError('The release attempts to replace a protected path: '+value)
    if len(p.parts)==1:
        if value not in ROOT_FILES:
            raise UpdateError('Unregistered root file: '+value)
    elif p.parts[0] not in APP_DIRS:
        raise UpdateError('Unregistered application directory: '+value)
    if any(re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)',part,re.I) for part in p.parts):
        raise UpdateError('Reserved Windows filename.')
    return p

def read_json(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise UpdateError('Duplicate JSON key in release metadata.')
            result[key]=value
        return result
    try:return json.loads(raw,object_pairs_hook=unique)
    except (ValueError,UnicodeError) as exc:raise UpdateError('Invalid JSON metadata.') from exc

def inspect_release(path):
    """Verify every byte before staging. The hashes detect corruption, not publisher identity."""
    try:return _inspect_release(path)
    except UpdateError:raise
    except (OSError,ValueError,TypeError,KeyError,RuntimeError,zipfile.BadZipFile) as exc:
        raise UpdateError('Cannot verify release archive: '+str(exc)) from exc

def _inspect_release(path):
    if not zipfile.is_zipfile(path): raise UpdateError('Not a complete ZIP archive: '+str(path))
    with zipfile.ZipFile(path) as z:
        entries=[i for i in z.infolist() if not i.is_dir()]
        if len(entries)>5000 or sum(i.file_size for i in entries)>MAX_TOTAL:
            raise UpdateError('The release exceeds the safety limit.')
        manifests=[i for i in entries if i.filename.endswith('/release-manifest.json') or i.filename=='release-manifest.json']
        if len(manifests)!=1: raise UpdateError('This ZIP does not contain exactly one FORGE release manifest. V0.1 ZIPs are not update packages.')
        mf=manifests[0]
        if mf.file_size>2_000_000: raise UpdateError('Manifest is too large.')
        prefix=mf.filename[:-len('release-manifest.json')]
        if prefix and (prefix.count('/')!=1 or not re.fullmatch(r'FORGE-v[\d.]+/',prefix)):
            raise UpdateError('Unexpected release root folder.')
        manifest=read_json(z.read(mf))
        if not isinstance(manifest,dict) or manifest.get('app')!='FORGE' or type(manifest.get('format')) is not int or manifest['format']!=1:
            raise UpdateError('Unsupported update manifest.')
        version(manifest.get('version')); version(manifest.get('minimum_from','0.1.0'))
        hashes=manifest.get('files')
        if not isinstance(hashes,dict) or not hashes: raise UpdateError('Missing release file hashes.')
        normalized=set()
        for rel,checksum in hashes.items():
            safe_relative(rel)
            if rel.casefold() in normalized: raise UpdateError('Duplicate case-insensitive target.')
            normalized.add(rel.casefold())
            if not re.fullmatch('[a-f0-9]{64}',str(checksum)): raise UpdateError('Invalid file checksum.')
        found={}; seen=set()
        for item in entries:
            if not item.filename.startswith(prefix): raise UpdateError('Archive file outside release root.')
            rel=item.filename[len(prefix):]
            safe_relative(rel)
            if rel.casefold() in seen: raise UpdateError('Duplicate archive entry.')
            seen.add(rel.casefold())
            mode=item.external_attr>>16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0,stat.S_IFREG)):
                raise UpdateError('Links and special files are not accepted.')
            if item.flag_bits & 1: raise UpdateError('Encrypted release entries are not supported.')
            if rel=='release-manifest.json': continue
            if rel not in hashes: raise UpdateError('Unlisted file in release: '+rel)
            content=z.read(item)
            if hashlib.sha256(content).hexdigest()!=hashes[rel]:
                raise UpdateError('Integrity verification failed: '+rel)
            if rel.endswith('.py'):
                try: compile(content,rel,'exec')
                except (SyntaxError,UnicodeError) as exc: raise UpdateError('Python source failed compilation: '+rel) from exc
            found[rel]=content
        if set(found)!=set(hashes): raise UpdateError('The archive is missing manifest files.')
        for required in ('forge/__init__.py','forge/app.py','launcher.py','server.py','web/app.js','web/index.html'):
            if required not in found: raise UpdateError('Required application file is missing: '+required)
        declared=re.search(rb"__version__\s*=\s*['\"]([0-9.]+)",found['forge/__init__.py'])
        if not declared or declared.group(1).decode()!=manifest['version']:
            raise UpdateError('Manifest version does not match application code.')
        found['release-manifest.json']=z.read(mf)
        return manifest,found

def installed_version(root):
    p=root/'forge'/'__init__.py'
    if not p.is_file(): raise UpdateError('FORGE installation not found: '+str(root))
    m=re.search(r"__version__\s*=\s*['\"]([0-9.]+)",p.read_text(encoding='utf-8'))
    if not m: raise UpdateError('Could not read the installed FORGE version.')
    version(m.group(1));return m.group(1)

def find_installation(home):
    if (home/'server.py').is_file() and (home/'forge'/'__init__.py').is_file(): return home
    matches=[p for p in home.glob('FORGE*') if p.is_dir() and (p/'server.py').is_file() and (p/'forge'/'__init__.py').is_file()]
    if len(matches)==1: return matches[0]
    raise UpdateError('Place Update FORGE.bat beside your active Open FORGE.bat, or in its parent with only one extracted FORGE folder. Multiple installations are not guessed.')

def select_release(folder,current,*,root=None):
    candidates=[];invalid=[]
    for path in folder.glob('FORGE-v*.zip'):
        try:
            if is_link(path):raise UpdateError('Release archive links and junctions are not accepted.')
            manifest,_=inspect_release(path)
            if version(manifest['version'])>version(current):
                if version(current)<version(manifest.get('minimum_from','0.1.0')):
                    invalid.append((path.name,'Requires base '+manifest['minimum_from']+' or newer.'))
                    continue
                if root is not None:plan_release(root,path)
                candidates.append((version(manifest['version']),path,manifest))
        except (UpdateError,OSError) as exc:
            invalid.append((path.name,str(exc)))
    if not candidates:
        details='\n'.join(name+': '+error for name,error in invalid)
        raise UpdateError('No newer verified FORGE build was found in '+str(folder)+('.\n'+details if details else '.'))
    candidates.sort(key=lambda x:(x[0],x[1].name))
    latest=[item for item in candidates if item[0]==candidates[-1][0]]
    if len({hashlib.sha256(item[1].read_bytes()).digest() for item in latest})>1:
        raise UpdateError('Two different ZIPs claim the latest version. Remove the unintended copy before updating.')
    return candidates[-1][1]

def plan_release(root,archive):
    """Read-only preflight for the CLI and browser. Counts exclude preserved originals."""
    root=Path(root).resolve();archive=Path(archive)
    if is_link(archive):raise UpdateError('Release archive links and junctions are not accepted.')
    manifest,contents=inspect_release(archive);current=installed_version(root)
    if version(manifest['version'])<=version(current):raise UpdateError('The selected release is not newer. Use rollback for a prior release.')
    minimum=manifest.get('minimum_from','0.1.0')
    if version(current)<version(minimum):raise UpdateError('This build requires base '+minimum+' or newer.')
    result={'version':manifest['version'],'current_version':current,'minimum_from':minimum,
            'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
            'changed_files':0,'new_files':0,'unchanged_files':0,'preserved_originals':0,'total_files':len(contents)}
    changed_sources=[]
    for rel,content in contents.items():
        target=assert_target(root,rel)
        if rel.startswith('original_scripts/') and target.exists():result['preserved_originals']+=1
        elif not target.exists():result['new_files']+=1
        elif target.read_bytes()==content:result['unchanged_files']+=1
        else:result['changed_files']+=1
        if rel.endswith('.py') and not (rel.startswith('original_scripts/') and target.exists()) and (not target.exists() or target.read_bytes()!=content):changed_sources.append(rel)
    bytecode_files(root,changed_sources)
    result['sha256']=result['archive_sha256'];result['preserved_files']=result['preserved_originals']
    return result

def atomic_write(path,content):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.forge-'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('xb') as f:
            f.write(content);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)

def write_json(path,value):
    atomic_write(path,json.dumps(value,ensure_ascii=False,indent=2).encode())

def local_data():
    if os.getenv('FORGE_DATA_DIR'):return Path(os.environ['FORGE_DATA_DIR']).expanduser().resolve()
    return Path(os.getenv('LOCALAPPDATA',str(Path.home()/'.local'/'share')))/'FORGE'

def runtime_request(runtime,route,body=None):
    port=runtime['port']
    if not isinstance(port,int) or not 1024<=port<=65535:raise UpdateError('Invalid local runtime port.')
    req=urllib.request.Request('http://127.0.0.1:'+str(port)+route,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'X-Forge-Token':runtime['token'],'Content-Type':'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=3) as response:
        return json.load(response)

def running(data):
    try:
        runtime=read_json((data/'runtime.json').read_text(encoding='utf-8'))
        state=runtime_request(runtime,'/api/state')
        if not state.get('version'): return None
        return runtime,state
    except Exception:return None

def stop_idle(data,root):
    live=running(data)
    if not live:return None
    runtime,state=live
    if runtime.get('root') and Path(runtime['root']).resolve()!=root.resolve():
        raise UpdateError('A different FORGE installation owns the local service. Stop it before updating this folder.')
    busy=[j for j in state['jobs'] if j['status'] in {'running','queued','cancelling','finalizing'}]
    # State shows recent history only. Check the entire local queue before shutdown.
    database=data/'forge.sqlite3'
    if database.is_file():
        with sqlite3.connect(database) as db:
            busy_count=db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','cancelling','finalizing')").fetchone()[0]
        if busy_count: busy=True
    if busy: raise UpdateError('The processing queue is not idle. Finish or cancel queued/active jobs before updating; no work was stopped.')
    settings=state['settings']
    # Write before prepare: V0.2 does not persist its own pre-maintenance preference.
    marker=root/'.forge-updating'
    if not marker.exists():
        write_json(marker,{'format':1,'root':str(root.resolve()),'data':str(data.resolve()),
                           'queue_paused':bool(settings.get('queue_paused',False))})
    prepared=False
    try:
        if version(state.get('version','0.1.0')) >= (0,2,0):
            settings=runtime_request(runtime,'/api/update/prepare',{})['previous_settings']
            prepared=True
        runtime_request(runtime,'/api/shutdown',{})
        for _ in range(80):
            if not running(data):return settings
            time.sleep(.25)
        raise UpdateError('The service did not stop cleanly. No code was replaced.')
    except Exception:
        if prepared:
            with contextlib.suppress(Exception):runtime_request(runtime,'/api/update/cancel',{})
        raise

@contextlib.contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+b')
    try:
        if os.name=='nt':
            import msvcrt
            handle.seek(0)
            if not handle.read(1):handle.write(b'0');handle.flush()
            handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    except (BlockingIOError,PermissionError) as exc:
        raise UpdateError('An update/service lock or file permission prevented access. No forced process termination was performed.') from exc
    finally:handle.close()

def assert_target(root,rel):
    p=root/rel
    # Never follow an existing junction/symlink into user content.
    current=p
    while current!=root:
        if is_link(current):
            raise UpdateError('Refusing to update through a link or junction: '+str(current))
        current=current.parent
    if p.exists() and not p.is_file():raise UpdateError('A directory occupies an application file path: '+rel)
    return p

def is_link(path):
    if path.is_symlink():return True
    # Python 3.11 has no Path.is_junction; all Windows reparse points are refused.
    try:return bool(getattr(path.lstat(),'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400))
    except FileNotFoundError:return False

def bytecode_files(root,sources):
    """Find only generated caches for changed sources, across interpreter tags."""
    files=[]
    for rel in sources:
        if not rel.endswith('.py'):continue
        safe_relative(rel);source=assert_target(root,rel)
        cache=source.parent/'__pycache__'
        if is_link(cache):raise UpdateError('Refusing to invalidate a linked bytecode cache: '+str(cache))
        if cache.exists() and not cache.is_dir():raise UpdateError('A file occupies the bytecode cache directory: '+str(cache))
        # Cache tags do not contain dots. This excludes a different source such
        # as module.other.py while accepting CPython, PyPy, and optimized caches.
        pattern=re.compile(re.escape(source.stem)+r'\.[A-Za-z0-9_-]+(?:\.opt-[A-Za-z0-9]+)?\.pyc\Z')
        candidates=[source.with_suffix('.pyc'),source.with_suffix('.pyo')]
        if cache.is_dir():candidates.extend(p for p in cache.iterdir() if pattern.fullmatch(p.name))
        for path in candidates:
            if is_link(path):raise UpdateError('Refusing to invalidate linked bytecode: '+str(path))
            if path.exists():
                if not path.is_file():raise UpdateError('A directory occupies a bytecode file: '+str(path))
                files.append(path)
    return files

def invalidate_bytecode(root,sources):
    # Validate the whole set before removing anything; never follow cache links.
    for path in bytecode_files(root,sources):path.unlink()

def backup_database(data,backup):
    source=data/'forge.sqlite3'
    if source.is_file():
        dest=backup/'user-data-snapshot'/'forge.sqlite3';dest.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(source) as src,sqlite3.connect(dest) as dst:
            src.backup(dst)

def _inspect_backup(root,backup):
    root=Path(root).resolve();backup=Path(backup)
    try:journal=read_json((backup/'journal.json').read_text(encoding='utf-8'))
    except (OSError,UnicodeError) as exc:raise UpdateError('Cannot read backup journal: '+str(exc)) from exc
    if not isinstance(journal,dict) or not isinstance(journal.get('root'),str):raise UpdateError('Invalid backup journal.')
    if Path(journal['root']).resolve()!=root:raise UpdateError('Backup belongs to a different installation.')
    version(journal.get('from'));version(journal.get('to'))
    if version(journal['from'])>=version(journal['to']):raise UpdateError('Invalid backup version range.')
    if not isinstance(journal.get('state'),str) or journal['state'] not in {'installing','completed','rolled_back'}:raise UpdateError('Invalid backup state.')
    files=journal.get('files')
    if not isinstance(files,dict) or not files:raise UpdateError('Missing backup file records.')
    if is_link(backup) or is_link(backup/'code'):raise UpdateError('Backup links and junctions are not accepted.')
    # Verify all originals first; never restore a partial/tampered backup silently.
    contents={};seen=set()
    for rel,item in files.items():
        safe_relative(rel);assert_target(root,rel)
        if rel.casefold() in seen:raise UpdateError('Duplicate case-insensitive backup target.')
        seen.add(rel.casefold())
        if not isinstance(item,dict) or type(item.get('existed')) is not bool:raise UpdateError('Invalid backup file record: '+rel)
        if rel.startswith('original_scripts/') and item['existed']:
            raise UpdateError('Invalid original-script replacement in backup.')
        if item['existed']:
            if not isinstance(item.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',item['sha256']):raise UpdateError('Invalid backup checksum: '+rel)
            try:content=assert_target(backup/'code',rel).read_bytes()
            except OSError as exc:raise UpdateError('Cannot read backup original: '+rel) from exc
            if hashlib.sha256(content).hexdigest()!=item['sha256']:raise UpdateError('Backup integrity failed: '+rel)
            contents[rel]=content
    original=contents.get('forge/__init__.py',b'')
    declared=re.search(rb"__version__\s*=\s*['\"]([0-9.]+)",original)
    if not declared or declared.group(1).decode()!=journal['from']:raise UpdateError('Backup version does not match original application code.')
    return journal,contents

def inspect_backup(root,backup):
    """Return the validated journal without stopping services or changing any files."""
    return _inspect_backup(root,backup)[0]

def restore_snapshot(root,backup):
    root=Path(root).resolve();backup=Path(backup)
    journal,contents=_inspect_backup(root,backup)
    invalidate_bytecode(root,journal['files'])
    for rel,item in journal['files'].items():
        target=assert_target(root,rel)
        if item['existed']:atomic_write(target,contents[rel])
        else:target.unlink(missing_ok=True)
    journal['state']='rolled_back';write_json(backup/'journal.json',journal)
    (root/'.forge-update-state.json').unlink(missing_ok=True)
    return journal

def apply_release(root,archive,data,*,backups=None,verify=None):
    root=root.resolve();manifest,contents=inspect_release(archive)
    current=installed_version(root)
    if version(manifest['version'])<=version(current):raise UpdateError('The selected release is not newer. Use rollback for a prior release.')
    if version(current)<version(manifest.get('minimum_from','0.1.0')):raise UpdateError('This build requires a newer base installation.')
    writes={}
    for rel,content in contents.items():
        target=assert_target(root,rel)
        if rel.startswith('original_scripts/') and target.exists():continue
        if target.is_file() and target.read_bytes()==content:continue
        writes[rel]=content
    bytecode_files(root,writes)
    backups=backups or root/'Backups'
    backup=backups/(time.strftime('%Y%m%d-%H%M%S')+'-'+current+'-to-'+manifest['version']+'-'+uuid.uuid4().hex[:6])
    backup.mkdir(parents=True,exist_ok=False)
    journal={'root':str(root),'from':current,'to':manifest['version'],'state':'preparing','files':{}}
    for rel in writes:
        p=root/rel;item={'existed':p.is_file()}
        if item['existed']:
            content=p.read_bytes();atomic_write(backup/'code'/rel,content)
            item['sha256']=hashlib.sha256(content).hexdigest()
        journal['files'][rel]=item
    backup_database(data,backup)
    journal['state']='installing';write_json(backup/'journal.json',journal)
    write_json(root/'.forge-update-state.json',{'backup':str(backup),'state':'installing'})
    try:
        invalidate_bytecode(root,writes)
        for rel,content in writes.items():atomic_write(root/rel,content)
        for rel,content in writes.items():
            if (root/rel).read_bytes()!=content:raise UpdateError('Post-copy verification failed: '+rel)
        if verify:verify(root)
        journal['state']='completed';write_json(backup/'journal.json',journal)
        (root/'.forge-update-state.json').unlink(missing_ok=True)
        write_json(root/'update-last.json',{'version':manifest['version'],'backup':str(backup),'updated':time.time()})
    except BaseException:
        restore_snapshot(root,backup)
        raise
    return backup

def smoke(root):
    py=root/'.venv'/'Scripts'/'python.exe' if os.name=='nt' else Path(sys.executable)
    if not py.is_file():raise UpdateError('Private Python environment is missing; run Setup FORGE.bat.')
    code="import tempfile; from pathlib import Path; from forge.app import create_app; from forge import __version__; d=tempfile.TemporaryDirectory(); a=create_app(Path(d.name),'smoke-check',18765,workers=False); a.state.store.close(); print(__version__)"
    p=subprocess.run([str(py),'-c',code],cwd=root,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=45,
                     env={**os.environ,'PYTHONUTF8':'1'})
    if p.returncode:raise UpdateError('Startup import smoke test failed. Code rollback was triggered.\n'+p.stderr[-2000:])
    expected=installed_version(root)
    reported=p.stdout.strip().splitlines()
    if not reported or reported[-1]!=expected:
        raise UpdateError('Startup version smoke test failed: expected '+expected+', imported '+(reported[-1] if reported else 'no version')+'. Code rollback was triggered.')

def maintenance_preference(root,data):
    marker=root/'.forge-updating'
    if not marker.is_file():return None
    raw=marker.read_text(encoding='utf-8')
    if raw=='An application update is in progress. Do not start FORGE.\n':return None
    record=read_json(raw)
    if (not isinstance(record,dict) or record.get('format')!=1 or type(record.get('queue_paused')) is not bool
            or not isinstance(record.get('root'),str) or not isinstance(record.get('data'),str)
            or Path(record['root']).resolve()!=root.resolve() or Path(record['data']).resolve()!=data.resolve()):
        raise UpdateError('Maintenance marker does not match this installation and data directory.')
    return {'queue_paused':record['queue_paused']}

def restore_pause(data,previous,*,root=None):
    if not (data/'forge.sqlite3').is_file():return
    with sqlite3.connect(data/'forge.sqlite3') as db:
        row=db.execute("SELECT value FROM kv WHERE key='update_maintenance'").fetchone()
        metadata=read_json(row[0]) if row else None
        if metadata is not None:
            if (not isinstance(metadata,dict) or not isinstance(metadata.get('root'),str)
                    or type(metadata.get('queue_paused')) is not bool or root is None
                    or Path(metadata['root']).resolve()!=root.resolve()):
                raise UpdateError('Saved maintenance preference belongs to a different installation or is invalid.')
            previous=metadata
        if previous is None:return
        row=db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()
        if row:
            value=json.loads(row[0]);value['queue_paused']=bool(previous.get('queue_paused',False))
            db.execute("UPDATE kv SET value=? WHERE key='settings'",(json.dumps(value),))
        db.execute("DELETE FROM kv WHERE key='update_maintenance'")

def write_maintenance_marker(root,data,previous=None):
    if (root/'.forge-updating').exists():return
    if previous is None and (data/'forge.sqlite3').is_file():
        with sqlite3.connect(data/'forge.sqlite3') as db:
            row=db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()
            previous=read_json(row[0]) if row else None
    write_json(root/'.forge-updating',{'format':1,'root':str(root.resolve()),'data':str(data.resolve()),
                                      'queue_paused':bool((previous or {}).get('queue_paused',False))})

def main(argv=None):
    parser=argparse.ArgumentParser(description='Update FORGE from a verified local build ZIP.')
    parser.add_argument('--root');parser.add_argument('--zip');parser.add_argument('--yes',action='store_true')
    parser.add_argument('--dry-run',action='store_true');parser.add_argument('--rollback',action='store_true')
    parser.add_argument('--no-launch',action='store_true')
    parser.add_argument('--pause-on-exit',action='store_true',help='Keep a directly launched Windows console open for review.')
    args=parser.parse_args(argv)
    home=Path(args.root or os.getenv('FORGE_UPDATE_HOME') or Path(__file__).resolve().parents[1]).resolve()
    root=find_installation(home);data=local_data()
    print('\nFORGE LOCAL BUILD UPDATER\nInstallation: '+str(root)+'\nBuilds: '+str(home/'Build'))
    print('Original media, model caches, tools, environments and user settings are preserved.')
    print('Only use builds you trust. Embedded SHA-256 hashes detect corruption; they are not a digital signature.\n')
    # Dry runs inspect only: even lock files and maintenance preferences stay untouched.
    with contextlib.nullcontext() if args.dry_run else file_lock(home/'.forge-updater.lock'):
        pending=root/'.forge-update-state.json';marker=root/'.forge-updating'
        saved=maintenance_preference(root,data)
        if pending.is_file():
            record=read_json(pending.read_text(encoding='utf-8'))
            if not isinstance(record,dict) or not isinstance(record.get('backup'),str):raise UpdateError('Invalid interrupted-update record.')
            backup=Path(record['backup']).resolve()
            if not backup.is_relative_to((home/'Backups').resolve()):raise UpdateError('Interrupted-update backup location needs manual review.')
            inspect_backup(root,backup)
            if args.dry_run:
                print('Interrupted-update backup verified. Recovery is required; no changes made.');return 0
            if not args.yes and input('An interrupted update was found. Restore its code backup? [y/N] ').lower()!='y':return 1
            previous=stop_idle(data,root)
            with file_lock(data/'service.lock'):
                restore_snapshot(root,backup)
                restore_pause(data,saved if saved is not None else previous,root=root)
                marker.unlink(missing_ok=True)
            print('Interrupted update recovered. Run the updater again to install the new release.');return 0
        if marker.exists():
            # Copying starts only after the durable pending journal exists. An orphan
            # marker therefore precedes copying or follows completed/rolled-back copying.
            installed_version(root)
            if args.dry_run:
                print('Stale maintenance marker found. Recovery is required; no changes made.');return 0
            if not args.yes and input('Recover a stale maintenance marker? [y/N] ').lower()!='y':return 1
            previous=stop_idle(data,root)
            with file_lock(data/'service.lock'):
                restore_pause(data,saved if saved is not None else previous,root=root)
                marker.unlink()
            print('Maintenance recovered. Run the updater again to continue.');return 0
        current=installed_version(root)
        if not args.rollback and not args.zip:
            try:archive=select_release(home/'Build',current,root=root)
            except UpdateError:
                if not args.yes and not args.dry_run and input('No newer usable build. Type R to roll back, or Enter to exit: ').lower()=='r':args.rollback=True
                else:raise
        if not args.rollback and not args.yes and not args.dry_run:
            archive=Path(args.zip).absolute() if args.zip else archive
            proposed=plan_release(root,archive)
            print('Available build: '+archive.name+' ('+current+' -> '+proposed['version']+')')
            action=input('Press Enter to update, R to roll back, or Q to cancel: ').strip().lower()
            if action=='q':return 0
            if action=='r':args.rollback=True
            elif action:raise UpdateError('Cancelled: no update action selected.')
        if args.rollback:
            record=read_json((root/'update-last.json').read_text()) if (root/'update-last.json').is_file() else {}
            if not isinstance(record,dict) or not isinstance(record.get('backup'),str):raise UpdateError('No completed update backup is registered.')
            backup=Path(record['backup']).resolve()
            if not backup.is_relative_to((home/'Backups').resolve()):raise UpdateError('Backup location is outside this installation’s Backups directory.')
            journal=inspect_backup(root,backup)
            if journal['state']!='completed' or journal['to']!=current:raise UpdateError('This backup is not a completed update for the installed version.')
            print('Rollback code '+journal['to']+' -> '+journal['from']+'. Current user data will not be replaced.')
            if args.dry_run:return 0
            if not args.yes and input('Restore this code backup? [y/N] ').lower()!='y':return 0
            previous=stop_idle(data,root)
            with file_lock(data/'service.lock'):
                write_maintenance_marker(root,data,previous)
                write_json(pending,{'backup':str(backup),'state':'rolling_back'})
                restore_snapshot(root,backup)
                restore_pause(data,previous,root=root)
                marker.unlink(missing_ok=True)
            print('Code restored. The database snapshot is retained separately, not automatically restored.')
        else:
            archive=Path(args.zip).absolute() if args.zip else archive
            plan=plan_release(root,archive)
            print('Selected: '+archive.name+'\nVersion: '+current+' -> '+plan['version'])
            print('ZIP SHA256: '+plan['sha256'])
            print('Files: '+str(plan['changed_files'])+' changed, '+str(plan['new_files'])+' new, '+str(plan['preserved_files'])+' original scripts preserved.')
            if args.dry_run:print('Validation passed. No changes made.');return 0
            previous=stop_idle(data,root)
            with file_lock(data/'service.lock'):
                write_maintenance_marker(root,data,previous)
                try:
                    backup=apply_release(root,archive,data,backups=home/'Backups',verify=smoke)
                finally:
                    # Keep durable markers when rollback itself failed; launchers must
                    # continue refusing to open partially restored application code.
                    if not pending.exists():
                        restore_pause(data,previous,root=root)
                        marker.unlink(missing_ok=True)
            print('\nUpdated to '+plan['version']+'\nBackup: '+str(backup))
        if not args.no_launch:
            py=root/'.venv'/'Scripts'/'pythonw.exe' if os.name=='nt' else Path(sys.executable)
            if py.is_file():subprocess.Popen([str(py),str(root/'launcher.py')],cwd=root)
        return 0

if __name__=='__main__':
    result=1
    try:result=main()
    except Exception as exc:
        print('\nUPDATE STOPPED: '+str(exc),file=sys.stderr)
    finally:
        if '--pause-on-exit' in sys.argv[1:]:
            with contextlib.suppress(EOFError,KeyboardInterrupt):input('\nPress Enter to close this updater. ')
    sys.exit(result)
