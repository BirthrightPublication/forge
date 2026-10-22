"""Read-only diagnostics in a disposable process (CUDA can terminate its host)."""
import argparse
import json
import platform
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from forge.core import data_directory
from forge.cuda_health import runtime_report, inference_report
parser = argparse.ArgumentParser()
parser.add_argument('--inference', choices=['tiny','base','small','medium','large-v3','large-v3-turbo'])
args = parser.parse_args()
result = inference_report(data_directory(), args.inference) if args.inference else runtime_report()
result.update(python=platform.python_version(), platform=platform.platform())
print(json.dumps(result))
