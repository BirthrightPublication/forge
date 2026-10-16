"""Read-only native diagnostics, kept outside the web-service process."""
import json
import platform
import shutil
import subprocess
import sys
import time
result={'checked':time.time(),'python':platform.python_version(),'platform':platform.platform(),
        'cuda_devices':0,'compute_types':[],'notes':[],'inference_tested':False}
try:
    import ctranslate2
    result['ctranslate2']=ctranslate2.__version__
    result['cuda_devices']=ctranslate2.get_cuda_device_count()
    if result['cuda_devices']:
        result['compute_types']=sorted(ctranslate2.get_supported_compute_types('cuda'))
except Exception as exc:
    result['notes'].append('CTranslate2: '+str(exc))
smi=shutil.which('nvidia-smi')
if smi:
    try:
        p=subprocess.run([smi,'--query-gpu=name,driver_version,memory.total','--format=csv,noheader'],capture_output=True,text=True,timeout=12)
        result['gpu']=p.stdout.strip()[:2000]
    except Exception as exc:
        result['notes'].append('NVIDIA diagnostic: '+str(exc))
else:
    result['notes'].append('nvidia-smi not found on PATH.')
result['notes'].append('Device enumeration is not proof of successful model loading or inference. Test a short clip on CPU first, then GPU.')
print(json.dumps(result))
