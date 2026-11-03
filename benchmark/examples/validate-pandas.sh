#!/bin/bash
# Called by the harness after source capture, with PRSIM_TEST_REPORT outside source.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
python - <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

root = Path.cwd().resolve()
changed = subprocess.check_output(['git', 'diff', '--name-only', '-z', 'HEAD']).decode().split('\0')
added = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z']).decode().split('\0')
files = []
for name in sorted(set(changed + added)):
    if not name.startswith('pandas/tests/') or not name.endswith('.py'):
        continue
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise SystemExit('Changed test file is missing or unsafe: ' + name)
    files.append(name)
if not files:
    if any(name.startswith('pandas/') and '/tests/' not in name
           and name.endswith(('.py', '.pyx', '.pxd', '.c', '.h', '.cpp')) for name in changed + added):
        raise SystemExit('Functional source changed without a changed regression test. Add or update coverage.')
    # Documentation-only changes still receive a real project API smoke.
    files = ['pandas/tests/api/test_api.py']
print('Required test files:', json.dumps(files), flush=True)
raise SystemExit(subprocess.call([
    sys.executable, '-X', 'faulthandler', '-m', 'pytest', '-q',
    '--junitxml=' + os.environ['PRSIM_TEST_REPORT'], *files,
]))
PY
