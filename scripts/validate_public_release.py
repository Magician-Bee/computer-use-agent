"""Validate a bounded public-source release; no models or native desktop."""
from __future__ import annotations
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
TEST_FILES = [
    'tests/test_providers.py', 'tests/test_task_store.py',
    'tests/test_task_store_pause.py', 'tests/test_public_release.py',
]

def tracked_files():
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True)
    return [Path(os.fsdecode(p)) for p in result.stdout.split(b'\0') if p]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/public-validation.json')
    args = parser.parse_args()
    os.chdir(ROOT)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    files = tracked_files()
    checks = []
    syntax_errors = []
    python_count = 0
    for path in files:
        if path.suffix == '.py':
            python_count += 1
            try:
                ast.parse(path.read_text(encoding='utf-8'), filename=path.as_posix())
            except (SyntaxError, UnicodeError, OSError):
                syntax_errors.append(path.as_posix())
    checks.append({'name': 'python_syntax', 'exit_code': int(bool(syntax_errors)),
                   'files_checked': python_count, 'review_files': syntax_errors})
    env = os.environ.copy()
    for key in tuple(env):
        if any(x in key.upper() for x in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY', 'GIT_CONFIG_')):
            env.pop(key, None)
    env['COMPUTERUSE_TEST_PYTHON'] = sys.executable
    env['CI'] = '1'
    npm = shutil.which('npm')
    junit = ROOT / 'artifacts/public-release-pytest.xml'
    junit.parent.mkdir(parents=True, exist_ok=True)
    commands = [
        ('public_privacy', [sys.executable, 'scripts/check_public_privacy.py']),
        ('python_contracts', [sys.executable, '-m', 'pytest', *TEST_FILES, '-q', '--junitxml=' + str(junit)]),
        ('frontend_contracts', [npm or 'npm', '--prefix', 'frontend', 'test']),
    ]
    for name, command in commands:
        started = time.monotonic()
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
            code, output = result.returncode, result.stdout + result.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, output = 1, type(exc).__name__
        item = {'name': name, 'command': [str(x).replace(str(ROOT), '<workspace>') for x in command],
                'exit_code': code, 'seconds': round(time.monotonic() - started, 3)}
        if name == 'public_privacy':
            match = re.search(r'Tracked text files checked: (\d+); review items: (\d+)', output)
            if match:
                item.update(files_checked=int(match[1]), review_items=int(match[2]))
        if name == 'python_contracts' and junit.exists():
            tree = ET.parse(junit).getroot()
            suites = [tree] if tree.tag == 'testsuite' else list(tree.findall('testsuite'))
            totals = {key: sum(int(s.get(key, '0')) for s in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
            totals['passed'] = totals['tests'] - totals['failures'] - totals['errors'] - totals['skipped']
            item['counts'] = totals
        if name == 'frontend_contracts':
            item['counts'] = {k: int(v) for k, v in re.findall(r'^# (tests|pass|fail|cancelled|skipped) (\d+)', output, re.MULTILINE)}
            item['uses_mock_api'] = True
            item['uses_real_headless_browser'] = True
        checks.append(item)
        print(json.dumps(item), flush=True)
        if code:
            safe = output.replace(str(ROOT), '<workspace>').replace(str(Path.home()), '<home>')
            print(safe[-5000:], flush=True)
    manifest = {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
                if p.is_file() and p.as_posix() not in ('docs/evidence/public-validation.json',)
                and not p.as_posix().startswith('.github/workflows/publish-public-source-once')}
    versions = {}
    for package in ('pytest', 'pydantic', 'httpx', 'playwright', 'jsonschema'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = 'not installed'
    report = {
        'schema_version': 1, 'release': '0.1.0-public.1',
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'environment': {'os': platform.system(), 'architecture': platform.machine(), 'python': platform.python_version()},
        'dependency_versions': versions, 'checks': checks,
        'passed': all(c['exit_code'] == 0 for c in checks),
        'test_scope': 'selected Python contracts, static/privacy checks and real Chromium UI tests with a mock API',
        'source_manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'source_manifest': manifest, 'real_model_calls': 0, 'native_desktop_tests': False,
        'full_historical_suite_run': False,
        'limitations': ['Heuristic privacy checks are not proof of absence of every secret.',
                        'Public-source preparation does not erase retained old GitHub objects, Actions records or external clones.',
                        'Mock-model and browser-fixture success is not general task success or desktop certification.'],
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
