"""Run required real downloader checks and retain honest platform evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def notice(value):
    return str(value).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def run(output, *, require_windows=False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    receipt = {'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, timeout=10).strip(),
        'working_tree_clean': not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True, timeout=10).strip(),
        'status': 'failed', 'platform': sys.platform, 'native_windows_execution': os.name == 'nt',
        'physical_gpu_tested': False, 'live_youtube_tested': False,
        'scope': 'Progress/classification fixtures plus real owned localhost media, FFmpeg merging, worker interruption and recovery',
        'checked': datetime.now(timezone.utc).isoformat()}
    try:
        if require_windows and os.name != 'nt':
            raise ValueError('This required acceptance job must execute natively on Windows.')
        for module in ('yt_dlp', 'pytest'):
            if importlib.util.find_spec(module) is None:
                raise ValueError('Required acceptance dependency is missing: ' + module)
        receipt['yt_dlp'] = importlib.metadata.version('yt-dlp')
        for program in ('ffmpeg', 'ffprobe'):
            binary = shutil.which(program)
            if not binary:
                raise ValueError('Real ' + program + ' is required; media acceptance must not silently skip.')
            receipt[program] = subprocess.check_output([binary, '-version'], text=True, errors='replace', timeout=10).splitlines()[0]
        report = output / 'DOWNLOAD_TESTS.xml'
        result = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'tests/test_download_worker_v9.py',
                                 '--junitxml=' + str(report)], cwd=ROOT, timeout=600)
        if not report.is_file():
            raise ValueError('The required downloader suite did not produce its test report.')
        cases = ET.parse(report).findall('.//testcase')
        receipt['tests'] = len(cases)
        receipt['skipped'] = sum(case.find('skipped') is not None for case in cases)
        receipt['failed'] = sum(case.find('failure') is not None or case.find('error') is not None for case in cases)
        receipt['cases'] = [{'name': case.get('name', ''),
            'status': 'skipped' if case.find('skipped') is not None else 'failed'
                if case.find('failure') is not None or case.find('error') is not None else 'passed'} for case in cases]
        for case in cases:
            failure = case.find('failure')
            if failure is None:
                failure = case.find('error')
            if failure is not None:
                print('::error title=Real downloader acceptance::' + notice(case.get('name', '') + ': ' + failure.get('message', '')[-2500:]), flush=True)
        if result.returncode or not cases or receipt['skipped'] or receipt['failed']:
            raise ValueError('All required downloader tests must pass without optional-media skips.')
        receipt['status'] = 'passed'
        print('::notice title=Verified actual downloader acceptance::' + notice(json.dumps(receipt)), flush=True)
        return True
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        receipt['error'] = str(error)[:2000]
        print('::error title=Required downloader acceptance::' + notice(receipt['error']), flush=True)
        return False
    finally:
        (output / 'DOWNLOAD_ACCEPTANCE.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--require-windows', action='store_true')
    arguments = parser.parse_args()
    raise SystemExit(0 if run(arguments.output, require_windows=arguments.require_windows) else 1)
