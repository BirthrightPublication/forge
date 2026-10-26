"""Private JSON-file protocol for the isolated speech worker; never an HTTP server."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
os.environ['DO_NOT_TRACK']='1'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['download','synthesize'])
    parser.add_argument('--request',type=Path,required=True)
    args=parser.parse_args();request=json.loads(args.request.read_text('utf-8'))
    response=Path(request['response'])
    try:
        from forge.speech_runtime import download_model,run_inference
        if args.action=='download':result=download_model(request)
        else:result=run_inference(request)
        response.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    except Exception as error:
        # References, scripts and private text are never printed on stdout/stderr.
        detail=str(error)
        for private in [request.get('data',''),request.get('text',''),request.get('options',{}).get('reference_audio','')]:
            if private:detail=detail.replace(private,'<private>')
        response.write_text(json.dumps({'success':False,'error':detail[:600]}),encoding='utf-8')
        return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
