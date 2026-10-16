"""FORGE offline release updater. Standard library only; no downloaded code is run to validate a ZIP.
The same source is embedded in Update FORGE.bat for the first V0.1 -> V0.2 update.
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
    if not isinstance(value,str) or not value or '\\' in value or ':' in value or '\x00' in value:
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
    return json.loads(raw,object_pairs_hook=unique)

def inspect_release(path):
    """Verify every byte before staging. The hashes detect corruption, not publisher identity."""
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
        if manifest.get('app')!='FORGE' or manifest.get('format')!=1:
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

def select_release(folder,current):
    candidates=[];invalid=[]
    for path in folder.glob('FORGE-v*.zip'):
        try:
            manifest,_=inspect_release(path)
            if version(manifest['version'])>version(current):
                candidates.append((version(manifest['version']),path,manifest))
        except (UpdateError,ValueError,zipfile.BadZipFile,KeyError) as exc:
            invalid.append((path.name,str(exc)))
    if not candidates:
        details='\n'.join(name+': '+error for name,error in invalid)
        raise UpdateError('No newer verified FORGE build was found in '+str(folder)+('.\n'+details if details else '.'))
    candidates.sort(key=lambda x:(x[0],x[1].name))
    if len(candidates)>1 and candidates[-1][0]==candidates[-2][0]:
        a=hashlib.sha256(candidates[-1][1].read_bytes()).digest();b=hashlib.sha256(candidates[-2][1].read_bytes()).digest()
        if a!=b: raise UpdateError('Two different ZIPs claim the latest version. Remove the unintended copy before updating.')
    return candidates[-1][1]

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
        if current.is_symlink() or (hasattr(current,'is_junction') and current.is_junction()):
            raise UpdateError('Refusing to update through a link or junction: '+str(current))
        current=current.parent
    if p.exists() and not p.is_file():raise UpdateError('A directory occupies an application file path: '+rel)
    return p

def backup_database(data,backup):
    source=data/'forge.sqlite3'
    if source.is_file():
        dest=backup/'user-data-snapshot'/'forge.sqlite3';dest.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(source) as src,sqlite3.connect(dest) as dst:
            src.backup(dst)

def restore_snapshot(root,backup):
    journal=read_json((backup/'journal.json').read_text(encoding='utf-8'))
    if Path(journal['root']).resolve()!=root.resolve():raise UpdateError('Backup belongs to a different installation.')
    # Verify all originals first; never restore a partial/tampered backup silently.
    for rel,item in journal['files'].items():
        safe_relative(rel);assert_target(root,rel)
        if rel.startswith('original_scripts/') and item.get('existed'):
            raise UpdateError('Invalid original-script replacement in backup.')
        if item['existed']:
            content=(backup/'code'/rel).read_bytes()
            if hashlib.sha256(content).hexdigest()!=item['sha256']:raise UpdateError('Backup integrity failed: '+rel)
    for rel,item in journal['files'].items():
        target=assert_target(root,rel)
        if item['existed']:atomic_write(target,(backup/'code'/rel).read_bytes())
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

def restore_pause(data,previous):
    if previous is None or not (data/'forge.sqlite3').is_file():return
    with sqlite3.connect(data/'forge.sqlite3') as db:
        row=db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()
        if row:
            value=json.loads(row[0]);value['queue_paused']=bool(previous.get('queue_paused',False))
            db.execute("UPDATE kv SET value=? WHERE key='settings'",(json.dumps(value),))

def main(argv=None):
    parser=argparse.ArgumentParser(description='Update FORGE from a verified local build ZIP.')
    parser.add_argument('--root');parser.add_argument('--zip');parser.add_argument('--yes',action='store_true')
    parser.add_argument('--dry-run',action='store_true');parser.add_argument('--rollback',action='store_true')
    parser.add_argument('--no-launch',action='store_true')
    args=parser.parse_args(argv)
    home=Path(args.root or os.getenv('FORGE_UPDATE_HOME') or Path(__file__).resolve().parents[1]).resolve()
    root=find_installation(home);data=local_data()
    print('\nFORGE LOCAL BUILD UPDATER\nInstallation: '+str(root)+'\nBuilds: '+str(home/'Build'))
    print('Original media, model caches, tools, environments and user settings are preserved.')
    print('Only use builds you trust. Embedded SHA-256 hashes detect corruption; they are not a digital signature.\n')
    with file_lock(home/'.forge-updater.lock'):
        pending=root/'.forge-update-state.json'
        if pending.is_file():
            record=read_json(pending.read_text())
            backup=Path(record['backup']).resolve()
            if not backup.is_relative_to((home/'Backups').resolve()):raise UpdateError('Interrupted-update backup location needs manual review.')
            if not args.yes and input('An interrupted update was found. Restore its code backup? [y/N] ').lower()!='y':return 1
            previous=stop_idle(data,root)
            with file_lock(data/'service.lock'):restore_snapshot(root,backup)
            restore_pause(data,previous)
            print('Interrupted update recovered. Run the updater again to install the new release.');return 0
        current=installed_version(root)
        if not args.rollback and not args.zip:
            try:archive=select_release(home/'Build',current)
            except UpdateError:
                if not args.yes and input('No newer usable build. Type R to roll back, or Enter to exit: ').lower()=='r':args.rollback=True
                else:raise
        if not args.rollback and not args.yes and not args.dry_run:
            archive=Path(args.zip).resolve() if args.zip else archive
            proposed,_=inspect_release(archive)
            print('Available build: '+archive.name+' ('+current+' -> '+proposed['version']+')')
            action=input('Press Enter to update, R to roll back, or Q to cancel: ').strip().lower()
            if action=='q':return 0
            if action=='r':args.rollback=True
            elif action:raise UpdateError('Cancelled: no update action selected.')
        if args.rollback:
            record=read_json((root/'update-last.json').read_text()) if (root/'update-last.json').is_file() else {}
            if not record.get('backup'):raise UpdateError('No completed update backup is registered.')
            backup=Path(record['backup']).resolve()
            if not backup.is_relative_to((home/'Backups').resolve()):raise UpdateError('Backup location is outside this installation’s Backups directory.')
            journal=read_json((backup/'journal.json').read_text())
            print('Rollback code '+journal['to']+' -> '+journal['from']+'. Current user data will not be replaced.')
            if args.dry_run:return 0
            if not args.yes and input('Restore this code backup? [y/N] ').lower()!='y':return 0
            previous=stop_idle(data,root)
            with file_lock(data/'service.lock'):
                restore_snapshot(root,backup)
            restore_pause(data,previous)
            print('Code restored. The database snapshot is retained separately, not automatically restored.')
        else:
            archive=Path(args.zip).resolve() if args.zip else archive
            manifest,_=inspect_release(archive)
            print('Selected: '+archive.name+'\nVersion: '+current+' -> '+manifest['version'])
            print('ZIP SHA256: '+hashlib.sha256(archive.read_bytes()).hexdigest())
            if args.dry_run:print('Validation passed. No changes made.');return 0
            previous=stop_idle(data,root)
            marker=root/'.forge-updating'
            try:
                with file_lock(data/'service.lock'):
                    marker.write_text('An application update is in progress. Do not start FORGE.\n')
                    backup=apply_release(root,archive,data,backups=home/'Backups',verify=smoke)
            finally:
                marker.unlink(missing_ok=True)
                restore_pause(data,previous)
            print('\nUpdated to '+manifest['version']+'\nBackup: '+str(backup))
        if not args.no_launch:
            py=root/'.venv'/'Scripts'/'pythonw.exe' if os.name=='nt' else Path(sys.executable)
            if py.is_file():subprocess.Popen([str(py),str(root/'launcher.py')],cwd=root)
        return 0

if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        print('\nUPDATE STOPPED: '+str(exc),file=sys.stderr)
        sys.exit(1)
