"""External recovery installers hand an active service a managed installation request."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from launcher import alive, data_directory, request

parser = argparse.ArgumentParser()
parser.add_argument('--engine', choices=['core','transcription','downloads','speakers','media-privacy'], required=True)
args = parser.parse_args()
runtime = alive(data_directory())
if not runtime:
    raise SystemExit('FORGE is not running. Use the external installer to repair its environment.')
print('Install compatible ' + args.engine + ' packages from PyPI after existing jobs finish.')
print('FORGE will stop and reopen automatically. This approves future repairs for this dependency class.')
if input('Continue? [y/N] ').strip().lower() != 'y':
    raise SystemExit(0)
result = request(runtime, '/api/maintenance/install', {'engines': [args.engine], 'approve': True})
print(result.get('message', 'Maintenance requested.'))
