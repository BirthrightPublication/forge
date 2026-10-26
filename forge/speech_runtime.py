"""Managed TTS installations, immutable model receipts and isolated inference helpers."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import MethodType
import urllib.request
from urllib.parse import urlsplit
import uuid
import venv
import zipfile

from .core import ROOT, json_write, no_window
from .model_cache import cache_lock
from .speech_engines import SPECS, MING_CODE_REVISION, REVISION, _read, _root, environment_python, environment_path

# PyPI publishes these two pure-Python/data-only packages as sdists. Their
# reviewed setup scripts only package Python/dictionary files. Native extensions
# still require binary wheels; arbitrary source or compiler builds are forbidden.
FRONTEND_SOURCES = {
    'jieba': {'version':'0.42.1','filename':'jieba-0.42.1.tar.gz','maximum_bytes':20_000_000,
        'url':'https://files.pythonhosted.org/packages/c6/cb/18eeb235f833b726522d7ebed54f2278ce28ba9438e3135ab0278d9792a2/jieba-0.42.1.tar.gz',
        'sha256':'055ca12f62674fafed09427f176506079bc135638a14e23e25be909131928db2'},
    'unidic-lite': {'version':'1.0.8','filename':'unidic-lite-1.0.8.tar.gz','maximum_bytes':48_000_000,
        'url':'https://files.pythonhosted.org/packages/55/2b/8cf7514cb57d028abcef625afa847d60ff1ffbf0049c36b78faa7c35046f/unidic-lite-1.0.8.tar.gz',
        'sha256':'db9d4572d9fdd4d00a97949d4b0741ec480ee05a7e7e2e32f547500dae27b245'},
}


def file_hash(path,algorithm='sha256'):
    digest=hashlib.new(algorithm)
    if algorithm=='sha1':digest.update(f'blob {Path(path).stat().st_size}\0'.encode())
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def verify_model(path):
    path=Path(path);receipt=_read(path/'forge-receipt.json')
    if not REVISION.fullmatch(receipt.get('revision','')) or not receipt.get('files'):
        raise ValueError('Invalid speech model receipt.')
    for name,metadata in receipt['files'].items():
        relative=PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name:
            raise ValueError('Unsafe model path.')
        file=path/name
        if file.is_symlink() or not file.resolve().is_relative_to(path.resolve()) or not file.is_file() or file.stat().st_size!=metadata['size'] or file_hash(file)!=metadata['sha256']:
            raise ValueError('Speech model integrity failed. Repair the selected model.')
    return receipt


def _command(args,progress=None,timeout=7200):
    # No command output is exposed to a browser: dependency diagnostics can carry
    # usernames, cache locations or proxy credentials.
    result=subprocess.run(list(map(str,args)),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout,**no_window())
    if result.returncode:raise RuntimeError('Isolated speech runtime setup failed. Check network access, Python version and compatible binary wheels; existing environments were preserved.')
    return result.stdout


def _fetch_ming_code(destination):
    """Retrieve only source/text from the exact reviewed upstream Git tree."""
    base='https://api.github.com/repos/inclusionAI/Ming-3.0-tts'
    headers={'User-Agent':'FORGE speech installer','Accept':'application/vnd.github+json'}
    with urllib.request.urlopen(urllib.request.Request(base+'/git/trees/'+MING_CODE_REVISION+'?recursive=1',headers=headers),timeout=30) as response:
        tree=json.loads(response.read(2_000_000))
    allowed={item['path']:item['sha'] for item in tree.get('tree',[]) if item.get('type')=='blob' and
             (item['path'].endswith('.py') or item['path'] in {'LICENSE','LICENSE.txt','NOTICE','cookbooks/instruction_en.md'})}
    if not allowed or len(allowed)>300:raise RuntimeError('Invalid upstream speech runtime inventory.')
    url='https://api.github.com/repos/inclusionAI/Ming-3.0-tts/tarball/'+MING_CODE_REVISION
    with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=60) as response:raw=response.read(30_000_001)
    if len(raw)>30_000_000:raise RuntimeError('Speech source download exceeded its size limit.')
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
        found=set()
        for member in archive:
            parts=PurePosixPath(member.name).parts
            name='/'.join(parts[1:])
            if name not in allowed:continue
            if not member.isfile() or member.size>3_000_000 or '..' in parts or member.name.startswith('/'):
                raise RuntimeError('Unsafe upstream speech source archive.')
            data=archive.extractfile(member).read()
            identity=hashlib.sha1(f'blob {len(data)}\0'.encode()+data).hexdigest()
            if identity!=allowed[name]:raise RuntimeError('Upstream speech source does not match its pinned Git object.')
            path=Path(destination)/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);found.add(name)
    if found!=set(allowed):raise RuntimeError('Incomplete pinned speech runtime source.')
    json_write(Path(destination)/'forge-upstream.json',{'repository':'inclusionAI/Ming-3.0-tts','revision':MING_CODE_REVISION,'git_blob_verified':True})


def build_frontend_wheels(python,environment):
    """Build only two explicitly reviewed data/Python packages in the new env."""
    environment=Path(environment)
    sources=environment/'frontend-sources';wheels=environment/'frontend-wheels'
    sources.mkdir();wheels.mkdir()
    _command([python,'-m','pip','install','--disable-pip-version-check','--only-binary=:all:',
              'setuptools==80.9.0','wheel==0.45.1'])
    records=[]
    for name,spec in FRONTEND_SOURCES.items():
        request=urllib.request.Request(spec['url'],headers={'User-Agent':'FORGE approved frontend installer'})
        with urllib.request.urlopen(request,timeout=60) as response:
            if urlsplit(response.geturl()).hostname!='files.pythonhosted.org':
                raise RuntimeError('Unexpected language-data publisher redirect.')
            raw=response.read(spec['maximum_bytes']+1)
        if len(raw)>spec['maximum_bytes'] or hashlib.sha256(raw).hexdigest()!=spec['sha256']:
            raise RuntimeError('Language-data source integrity verification failed.')
        archive=sources/spec['filename'];archive.write_bytes(raw)
        _command([python,'-m','pip','wheel','--disable-pip-version-check','--no-deps',
                  '--no-build-isolation','--wheel-dir',wheels,archive],timeout=300)
        expected=name.replace('-','_')+'-'+spec['version']+'-'
        matches=[path for path in wheels.iterdir() if path.name.startswith(expected) and path.name.endswith('-none-any.whl')]
        if len(matches)!=1:raise RuntimeError('The reviewed language package did not build a pure-Python wheel.')
        wheel=matches[0]
        with zipfile.ZipFile(wheel) as contents:
            for member in contents.infolist():
                relative=PurePosixPath(member.filename)
                if relative.is_absolute() or '..' in relative.parts or '\\' in member.filename or relative.suffix.lower() in {'.exe','.dll','.pyd','.so','.dylib'}:
                    raise RuntimeError('The reviewed language wheel contains an unsafe or native payload.')
        records.append({'package':name,'version':spec['version'],'source_url':spec['url'],
                        'source_sha256':spec['sha256'],'source_bytes':len(raw),
                        'wheel':wheel.name,'wheel_sha256':file_hash(wheel),'native_compilation':False})
    json_write(environment/'forge-frontend-artifacts.json',{'schema':1,'sources':records,
               'build_requirements':{'setuptools':'80.9.0','wheel':'0.45.1'}})
    return wheels


def install_engine(engine,data,*,consent=False,revision='',progress=lambda *a,**k:None,device='cpu'):
    if engine not in {'kokoro','ming'}:raise ValueError(SPECS.get(engine,{}).get('unavailable_reason','Unknown engine.'))
    if consent is not True:raise ValueError('Approve this model and isolated runtime download before installing.')
    if device not in {'cpu','cuda'}:raise ValueError('Select CPU or the approved CUDA 12.4 runtime.')
    if revision and (not isinstance(revision,str) or not REVISION.fullmatch(revision)):raise ValueError('Select an immutable model revision.')
    if sys.version_info[:2] not in {(3,10),(3,11),(3,12)}:
        raise RuntimeError('These isolated speech stacks require Python 3.10–3.12. Existing FORGE environments are preserved.')
    data=Path(data).resolve();environments=data/'environments';environments.mkdir(parents=True,exist_ok=True)
    with cache_lock(environments/('.speech-'+engine+'.lock'),progress):
        staging=environments/('.speech-'+engine+'-'+uuid.uuid4().hex)
        stable=environment_path(data,engine,device=device)
        if stable.exists() and not _read(stable/'forge-runtime.json').get('import_tested'):
            # No completed runtime ever points to this incomplete installation.
            stable.rename(environments/('.incomplete-'+engine+'-'+uuid.uuid4().hex))
        if stable.exists():
            # A valid immutable environment is never changed by a repair/download.
            result=download_model_in_environment(engine,data,revision,stable,progress)
            activate(data,engine,result['model_revision'])
            select_environment(data,engine,stable)
            return {**result,'runtime_installed':True,'inference_verified':False}
        progress(0,'Creating an isolated '+SPECS[engine]['label']+' environment',stage='Speech runtime installation')
        try:
            venv.EnvBuilder(with_pip=True).create(staging)
            python=staging/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            progress(0,'Installing pinned speech dependencies',stage='Speech runtime installation')
            frontend_wheels=build_frontend_wheels(python,staging) if engine=='kokoro' else None
            if device=='cuda':
                wheels=['torch==2.6.0+cu124']+(['torchaudio==2.6.0+cu124'] if engine=='ming' else [])
                _command([python,'-m','pip','install','--disable-pip-version-check','--timeout','30','--retries','1','--only-binary=:all:',
                          '--index-url','https://download.pytorch.org/whl/cu124',*wheels])
            _command([python,'-m','pip','install','--disable-pip-version-check','--timeout','30','--retries','1','--only-binary=:all:',
                      *(['--find-links',frontend_wheels] if frontend_wheels else []),
                      '-r',ROOT/SPECS[engine]['requirements']])
            _command([python,'-m','pip','check'])
            if engine=='ming':_fetch_ming_code(staging/'upstream')
            script=ROOT/'scripts'/'speech_runtime.py'
            result=download_model_in_environment(engine,data,revision,staging,progress)
            smoke='import kokoro,soundfile; from kokoro import KModel,KPipeline' if engine=='kokoro' else 'import sys;sys.path.insert(0,"upstream");from ming_tts import MingTTS'
            # Ming import must execute against the staged upstream directory.
            if engine=='ming':smoke='import sys;sys.path.insert(0,'+repr(str(staging/'upstream'))+');from ming_tts import MingTTS'
            _command([python,'-c',smoke],timeout=120)
            frontends=_language_frontends(python) if engine=='kokoro' else {}
            if engine=='kokoro' and not all(frontends.get('codes',{}).get(code,False) for code in 'abefhipjz'):
                missing=' '.join(code for code in 'abefhipjz' if not frontends.get('codes',{}).get(code,False))
                raise RuntimeError('Kokoro language frontend validation failed for: '+missing+'. Existing model environments were preserved.')
            json_write(staging/'forge-runtime.json',{'engine':engine,'python':sys.version.split()[0],
                       'requirements_sha256':file_hash(ROOT/SPECS[engine]['requirements']),
                       'upstream_revision':MING_CODE_REVISION if engine=='ming' else 'PyPI kokoro==0.9.4',
                       'installed':time.time(),'device':device,'import_tested':True,'inference_tested':False,
                       'language_frontends':frontends,
                       'frontend_artifacts':_read(staging/'forge-frontend-artifacts.json') if engine=='kokoro' else {}})
            # Each dependency/code fingerprint has its own immutable environment.
            # Existing environments remain available for jobs and rollback.
            staging.rename(stable)
            activate(data,engine,result['model_revision'])
            select_environment(data,engine,stable)
            return {**result,'runtime_installed':True,'inference_verified':False}
        except BaseException:
            # Failed staged installations are retained for diagnosis; the active
            # environment is never modified by pip.
            raise


def select_environment(data,engine,path):
    with cache_lock(Path(data)/'environments'/'.speech-selected.lock'):
        selection=Path(data)/'environments'/'speech-selected.json'
        values=_read(selection);values[engine]=Path(path).name;json_write(selection,values)


def _language_frontends(python):
    # Importing misaki.espeak configures the bundled espeakng-loader library and
    # data paths. ctypes.find_library alone misses that working Windows backend.
    code="""import importlib.metadata,json,os
os.environ['HF_HUB_OFFLINE']='1'
os.environ['TRANSFORMERS_OFFLINE']='1'
from kokoro import KPipeline
samples={'a':('en','The window is open.'),'b':('en','The window is open.'),
 'e':('es','La ventana está abierta.'),'f':('fr','La fenêtre est ouverte.'),
 'h':('hi','खिड़की खुली है।'),'i':('it','La finestra è aperta.'),
 'p':('pt','A janela está aberta.'),'j':('ja','窓が開いています。'),'z':('zh','窗户开着。')}
result={'en':True,'codes':{},'errors':{},'method':'Real offline phonemization through original KPipeline(model=False)'}
for code,(language,text) in samples.items():
 try:
  pipeline=KPipeline(lang_code=code,repo_id='hexgrad/Kokoro-82M',model=False)
  chunks=list(pipeline(text))
  ready=bool(chunks and any(item.phonemes.strip() for item in chunks))
  result['codes'][code]=ready
  result[language]=bool(result.get(language,True) and ready)
 except Exception as error:
  result['codes'][code]=False;result[language]=False;result['errors'][code]=type(error).__name__
result['espeak']=all(result['codes'].get(code,False) for code in 'efhip')
result['packages']={}
for name in ['misaki','espeakng-loader','phonemizer-fork','pyopenjtalk-plus','fugashi','jaconv','mojimoji','unidic-lite','cn2an','jieba','ordered-set','pypinyin','pypinyin-dict']:
 try:result['packages'][name]=importlib.metadata.version(name)
 except importlib.metadata.PackageNotFoundError:result['packages'][name]='missing'
print(json.dumps(result,ensure_ascii=False))"""
    try:return json.loads(_command([python,'-c',code],timeout=120).splitlines()[-1])
    except (ValueError,RuntimeError):return {}


def download_model_in_environment(engine,data,revision,environment,progress):
    python=Path(environment)/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    request=Path(environment)/('download-'+uuid.uuid4().hex+'.request.json');response=request.with_suffix('.result.json')
    json_write(request,{'engine':engine,'data':str(data),'revision':revision,'response':str(response)})
    progress(0,'Resolving and verifying the selected model revision',stage='Speech model download')
    try:
        with tempfile.TemporaryFile(mode='w+',encoding='utf-8') as errors:
            with subprocess.Popen([str(python),str(ROOT/'scripts'/'speech_runtime.py'),'download','--request',str(request)],
                                  stdout=subprocess.PIPE,stderr=errors,text=True,encoding='utf-8',errors='replace',**no_window()) as process:
                watchdog=threading.Timer(7200,process.kill);watchdog.daemon=True;watchdog.start()
                try:
                    for line in process.stdout:
                        try:event=json.loads(line)
                        except ValueError:continue
                        if event.get('kind')=='progress':progress(0,event.get('message','Downloading model'),stage='Speech model download')
                    code=process.wait(timeout=30)
                finally:watchdog.cancel()
            result=_read(response)
            if code or not result.get('success'):
                raise RuntimeError('Speech model download/verification failed. The publisher may be unavailable or offline; no unverified model was activated.')
        return result
    finally:
        request.unlink(missing_ok=True);response.unlink(missing_ok=True)


def download_model(request):
    """Runs in the isolated environment where huggingface_hub is installed."""
    from huggingface_hub import HfApi, hf_hub_download
    engine=request['engine'];repo=SPECS[engine]['repository'];data=Path(request['data'])
    requested=request.get('revision') or SPECS[engine]['approved_revision']
    info=HfApi().model_info(repo,revision=requested,files_metadata=True)
    revision=info.sha
    if not REVISION.fullmatch(revision or ''):raise RuntimeError('Publisher did not return an immutable revision.')
    if revision!=requested:raise RuntimeError('Publisher returned a different revision than the approved model request.')
    destination=_root(data)/engine/revision
    if (destination/'forge-receipt.json').exists():
        # Complete snapshots are immutable even during repair. A corrupt snapshot
        # must first be quarantined while no job uses it.
        receipt=verify_model(destination)
        return {'success':True,'engine':engine,'model_revision':revision,
                'download_bytes':receipt['bytes'],'cached':True}
    destination.mkdir(parents=True,exist_ok=True)
    files={};siblings=[]
    for item in info.siblings:
        name=item.rfilename;relative=PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or re.search(r'[:<>|?*\x00-\x1f]',name):raise ValueError('Unsafe publisher model path.')
        if engine=='kokoro':
            allowed=name in {'config.json','kokoro-v1_0.pth','LICENSE','LICENSE.txt','NOTICE','README.md','VOICES.md'} or (name.startswith('voices/') and name.endswith('.pt') and len(relative.parts)==2)
        else:
            allowed=(len(relative.parts)==1 and (name.endswith(('.json','.safetensors','.onnx','.txt','.jinja')) or name in {'LICENSE','NOTICE','README.md'}))
        if allowed:siblings.append(item)
    required={'config.json','kokoro-v1_0.pth','voices/am_onyx.pt'} if engine=='kokoro' else {'config.json','model.safetensors','campplus.onnx','tokenizer.json','tokenizer_config.json'}
    if not required.issubset({item.rfilename for item in siblings}):raise RuntimeError('Publisher model layout is incompatible with this adapter.')
    if len(siblings)>1000:raise RuntimeError('Publisher model inventory exceeds the installation limit.')
    for index,item in enumerate(siblings):
        name=item.rfilename
        print(json.dumps({'kind':'progress','message':f'Downloading and verifying model file {index+1}/{len(siblings)}: {name}'}),flush=True)
        file=Path(hf_hub_download(repo_id=repo,filename=name,revision=revision,local_dir=destination,etag_timeout=20))
        lfs=item.lfs
        expected=(lfs.get('sha256') if isinstance(lfs,dict) else getattr(lfs,'sha256',None)) if lfs else item.blob_id
        algorithm='sha256' if lfs else 'sha1'
        if not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}' if lfs else r'[a-f0-9]{40}',expected):
            raise RuntimeError('Publisher did not provide a verifiable artifact identity.')
        if file_hash(file,algorithm)!=expected:raise RuntimeError('Model artifact integrity verification failed.')
        if isinstance(item.size,int) and file.stat().st_size!=item.size:raise RuntimeError('Model artifact size mismatch.')
        files[name]={'sha256':file_hash(file),'size':file.stat().st_size}
    receipt={'engine':engine,'repository':repo,'revision':revision,'files':files,'bytes':sum(v['size'] for v in files.values()),
             'installed':time.time(),'publisher_blob_verified':True,'inference_tested':False}
    json_write(destination/'forge-receipt.json',receipt)
    # Activation belongs to the parent only after runtime imports succeeded.
    return {'success':True,'engine':engine,'model_revision':revision,'download_bytes':receipt['bytes']}


def activate(data,engine,revision):
    verify_model(_root(data)/engine/revision)
    with cache_lock(_root(data)/'.selected.lock'):
        selected=_read(_root(data)/'selected.json');selected[engine]=revision
        json_write(_root(data)/'selected.json',selected)


def blend_tensors(packs,weights,torch):
    """Actual model-native weighted tensor mixture; original packs stay untouched."""
    if not packs or len(packs)!=len(weights):raise ValueError('No compatible voice tensors to blend.')
    shape=tuple(packs[0].shape)
    if len(shape) not in {2,3} or shape[-1]!=256 or not 1<=shape[0]<=1024:raise ValueError('Invalid Kokoro voice tensor shape.')
    if any(tuple(pack.shape)!=shape or not bool(torch.isfinite(pack).all()) for pack in packs):
        raise ValueError('Kokoro voice packs must have identical shapes and finite values.')
    return sum((pack.to(dtype=torch.float32)*weight for pack,weight in zip(packs,weights)),torch.zeros_like(packs[0],dtype=torch.float32))


def prepare_ming_cpu(model,torch):
    """Use matching FP32 weights and reference inputs on the CPU adapter.

    Pinned Ming weights are opened as BF16 by upstream, whose inference contexts
    are CUDA-only. Its speaker embeddings are FP32, so CPU synthesis otherwise
    reaches a mismatched Linear operation. The upstream prompt encoder separately
    forces BF16 audio; adapt that instance method along with the model conversion.
    This retains the pinned source on disk and does not change the CUDA path.
    Reference: inclusionAI/Ming-3.0-tts commit 028c1eace193e7acdb256453da89b6e9207e052b;
    its MIT notice is retained in forge/licenses/Ming-3.0-runtime-LICENSE.txt.
    """
    core=model.model
    core.float()
    core.llm_dytpe=torch.float32

    def encode_cpu_prompt(instance,prompt_waveform):
        waveform=prompt_waveform.to(device=instance.device,dtype=torch.float32)
        length=torch.tensor([waveform.size(1)],dtype=torch.long,device=instance.device)
        latent,_=instance.audio.encode_latent(waveform,length)
        return latent

    core.encode_prompt_latent=MethodType(encode_cpu_prompt,core)
    return model


def run_inference(request):
    import numpy as np
    import soundfile as sf
    import torch
    from .speech_engines import resolve_options, model_path, LANGUAGES, normalize_blend, EMOTIONS, parse_ming_voice_catalog
    text=request['text'];data=Path(request['data']);value=resolve_options(request['options'],data)
    path=model_path(data,value['engine'],value['model_revision']);receipt=verify_model(path)
    device=value['device']
    if device=='auto':device='cuda' if torch.cuda.is_available() else 'cpu'
    if device=='cuda' and not torch.cuda.is_available():raise RuntimeError('PyTorch CUDA is unavailable in this isolated speech environment. Choose CPU or repair the compatible CUDA runtime.')
    torch.manual_seed(value['seed'])
    if torch.cuda.is_available():torch.cuda.manual_seed_all(value['seed'])
    output=Path(request['output'])
    if output.exists():raise ValueError('Refusing to overwrite speech output.')
    if value['engine']=='kokoro':
        frontends=_read(Path(sys.prefix)/'forge-runtime.json').get('language_frontends',{})
        language=value['language']
        if language in {'ja','zh'} and not frontends.get(language):
            raise RuntimeError('The optional phonemizer for this language has not been installed and validated.')
        if language not in {'en','ja','zh'} and not frontends.get(language,frontends.get('espeak',False)):
            raise RuntimeError('This Kokoro language needs an installed, validated eSpeak-NG runtime.')
        from kokoro import KModel, KPipeline
        model=KModel(repo_id=SPECS['kokoro']['repository'],config=str(path/'config.json'),model=str(path/'kokoro-v1_0.pth')).to(device).eval()
        blend=normalize_blend(value['blend']) if value.get('blend') else [{'voice':value['voice'],'weight':1.0}]
        language=LANGUAGES[value['voice'][0]][0]
        packs=[]
        for entry in blend:
            name='voices/'+entry['voice']+'.pt'
            if name not in receipt['files']:raise ValueError('The selected source voice is not part of this verified Kokoro revision.')
            packs.append(torch.load(path/name,map_location='cpu',weights_only=True))
        pack=blend_tensors(packs,[v['weight'] for v in blend],torch)
        pipeline=KPipeline(lang_code=value['voice'][0],repo_id=SPECS['kokoro']['repository'],model=model,device=device)
        # Passing the actual mixed embedding bypasses upstream lazy voice downloads.
        chunks=[item.audio.detach().cpu().float().numpy() for item in pipeline(text,voice=pack,speed=value['speed']) if item.audio is not None]
        if not chunks:raise RuntimeError('Kokoro produced no audio. Check phonemizer and language support.')
        audio=np.concatenate(chunks);sample_rate=24000
    else:
        upstream=Path(sys.prefix)/'upstream'
        pinned=_read(upstream/'forge-upstream.json')
        if pinned.get('revision')!=MING_CODE_REVISION or not pinned.get('git_blob_verified'):
            raise RuntimeError('Ming upstream runtime source is missing its pinned verification receipt.')
        sys.path.insert(0,str(upstream))
        import transformers
        original=transformers.AutoTokenizer.from_pretrained
        def local_tokenizer(*args,**kwargs):
            kwargs['trust_remote_code']=False;kwargs['local_files_only']=True
            return original(*args,**kwargs)
        transformers.AutoTokenizer.from_pretrained=local_tokenizer
        try:
            from ming_tts import MingTTS
            model=MingTTS(str(path),device='cuda:0' if device=='cuda' else 'cpu',text_normalization=False)
        finally:transformers.AutoTokenizer.from_pretrained=original
        if device=='cpu':prepare_ming_cpu(model,torch)
        task=value['task'];kwargs={'task':task,'text':text,'output_path':None}
        if task in {'IP','FREEFORM','VOICE_DESIGN'}:kwargs['use_zero_spk_emb']=True
        else:
            reference=Path(value['reference_audio'])
            if not reference.is_file() or reference.stat().st_size>100_000_000:raise ValueError('Reference audio is missing or exceeds 100 MB.')
            kwargs.update(prompt_wav_path=str(reference),use_spk_emb=True)
        if task=='IP':
            voice_catalog=parse_ming_voice_catalog((upstream/'cookbooks/instruction_en.md').read_text('utf-8'))
            if value['voice'] not in {v['id'] for v in voice_catalog}:raise ValueError('Choose a documented built-in Ming voice.')
            kwargs['controls']={'ip':value['voice']}
        elif task=='FREEFORM':kwargs['instruction']=value['instructions']
        elif task=='VOICE_DESIGN':kwargs['controls']={'style':value['instructions']}
        elif task=='EMOTION':kwargs['controls']={'emotion':EMOTIONS[value['emotion']]}
        elif task=='BASIC':
            kwargs['controls']={'speed':{'slow':'慢速','medium':'中速','fast':'快速'}[value['attribute_speed']],
                                'pitch':{'low':'低','medium':'中','high':'高'}[value['attribute_pitch']],
                                'volume':{'low':'低','medium':'中','high':'高'}[value['attribute_volume']]}
        elif task=='DIALECT':kwargs['controls']={'dialect':'chuanyu dialect' if value['dialect']=='chuanyu' else 'cantonese'}
        if task in {'TTS','MULTILINGUAL'}:kwargs.update(language_id=value['language'],prompt_text=value['reference_text'])
        audio=model.synthesize(**kwargs).detach().cpu().float().numpy().reshape(-1)
        sample_rate=int(model.sample_rate)
    if not len(audio) or not np.isfinite(audio).all():raise RuntimeError('Speech engine returned empty or invalid audio.')
    output.parent.mkdir(parents=True,exist_ok=True)
    sf.write(output,audio,sample_rate,format='WAV',subtype='PCM_16')
    if sf.info(output).frames!=len(audio):raise RuntimeError('Generated audio verification failed.')
    return {'success':True,'engine':value['engine'],'model_revision':value['model_revision'],
            'runtime_revision':value['runtime_revision'],
            'voice':value.get('voice',''),'voice_revision':value['model_revision'],
            'sample_rate':sample_rate,'duration':len(audio)/sample_rate,'device':device,
            'seed':value['seed'],'task':value['task'],'source_files':receipt['files'].get('voices/'+value.get('voice','')+'.pt',{}),
            'precision':'float32' if value['engine']=='kokoro' or device=='cpu' else 'bfloat16',
            'text_normalization':'Kokoro language phonemizer' if value['engine']=='kokoro' else 'Source-preserving FORGE text; optional pynini disabled'}
