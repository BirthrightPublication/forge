"""Read public publisher metadata on a native runner before model acceptance.

This probe downloads no weights and executes no publisher code. Failed publisher
access fails the check; it is never reported as successful model acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

REPOSITORIES = {
    'kokoro':'hexgrad/Kokoro-82M',
    'ming':'inclusionAI/Ming-3.0-tts-0.5b',
    'bark-3.0':'ConwayResearch/Underdog-Bark-3.0',
    'bark-3.0-plus':'ConwayResearch/Underdog-Bark-3.0-Plus',
    'bark-3.0-pro':'ConwayResearch/Underdog-Bark-3.0-Pro',
    'bark-3.0-clean':'ConwayResearch/Underdog-Bark-3.0-Clean',
}


def fetch(url,limit=2000000):
    request=urllib.request.Request(url,headers={'User-Agent':'FORGE native speech acceptance'})
    with urllib.request.urlopen(request,timeout=20) as response:
        data=response.read(limit+1)
    if len(data)>limit:
        raise ValueError('Publisher response exceeded the metadata size limit.')
    return data


def probe(output):
    output.mkdir(parents=True,exist_ok=True)
    entries=[]
    for engine,repository in REPOSITORIES.items():
        entry={'engine':engine,'repository':repository,'status':'failed'}
        try:
            metadata=json.loads(fetch('https://huggingface.co/api/models/'+repository+'?blobs=true'))
            revision=metadata.get('sha','')
            if not re.fullmatch('[a-f0-9]{40}',revision):
                raise ValueError('No immutable publisher revision was supplied.')
            entry.update(revision=revision,gated=metadata.get('gated'),
                         license=metadata.get('cardData',{}).get('license'),
                         model_files=[{'name':x['rfilename'],'bytes':x.get('size'),
                                       'lfs':x.get('lfs'),'blobId':x.get('blobId')}
                                      for x in metadata.get('siblings',[])])
            folder=output/engine;folder.mkdir(exist_ok=True)
            notices={}
            for name in ('README.md','LICENSE','LICENSE.txt','NOTICE'):
                if not any(x['rfilename']==name for x in metadata.get('siblings',[])):
                    continue
                data=fetch(f'https://huggingface.co/{repository}/resolve/{revision}/{name}')
                (folder/name).write_bytes(data)
                notices[name]=hashlib.sha256(data).hexdigest()
            if not notices:
                raise ValueError('Publisher metadata was readable but no model card or notices could be fetched.')
            entry.update(status='passed',notice_sha256=notices)
            summary={'engine':engine,'repository':repository,'revision':revision,
                     'license':entry['license'],'notices':notices}
            message=json.dumps(summary).replace('%','%25').replace('\r','%0D').replace('\n','%0A')
            print('::notice title=Verified speech publisher metadata::'+message,flush=True)
            for name in notices:
                if name not in {'LICENSE','LICENSE.txt','NOTICE'}:
                    continue
                notice=(folder/name).read_text('utf-8',errors='replace')
                message=json.dumps({'engine':engine,'file':name,'characters':len(notice),
                                    'text':notice[:30000],'truncated':len(notice)>30000})
                message=message.replace('%','%25').replace('\r','%0D').replace('\n','%0A')
                print('::notice title=Speech publisher license and notice::'+message,flush=True)
        except Exception as error:
            code=getattr(error,'code',None)
            # Do not expose proxies, signed redirects or credential-bearing paths.
            entry.update(error_type=type(error).__name__,http_status=code,
                         error='Publisher metadata/artifact access failed.' if not isinstance(error,ValueError) else str(error))
        entries.append(entry)
        if entry['status']!='passed':
            print(f'::error title=Speech publisher access::{engine}: {entry["error_type"]}; HTTP status {entry.get("http_status") or "unavailable"}',flush=True)
    report={'source_commit':os.environ.get('GITHUB_SHA',''),'platform':sys.platform,
            'all_publishers_accessible':all(x['status']=='passed' for x in entries),
            'model_inference_tested':False,'weights_downloaded':False,'entries':entries}
    (output/'publisher-access.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2),flush=True)
    return report['all_publishers_accessible']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    raise SystemExit(0 if probe(args.output) else 1)
