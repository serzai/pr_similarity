"""Required project tests are sealed separately from successful agent execution."""

import json
import os
from pathlib import Path

import pytest
import yaml

from benchmark.config import load_config
from benchmark.models import Report
from benchmark.persistence.run_store import RunStore
from benchmark.publication.archive import create_archive, verify_archive
from benchmark.runtime import coordinator
from test_artifact_workflow import _crash, _inputs


@pytest.mark.parametrize('fails', [False, True])
def test_required_validation_changes_run_success_without_changing_native_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    inputs = _inputs(tmp_path)
    script = tmp_path / 'validate.sh'
    script.write_text('''set -eu
python - <<'PY'
import os
from pathlib import Path
Path(os.environ['PRSIM_TEST_REPORT']).write_text(''' + repr(
        '<testsuite><testcase name="regression">' + ('<failure/>' if fails else '') + '</testcase></testsuite>'
    ) + ''')
# Test side effects must not become edits attributed to the agent.
Path('src/value.py').write_text('VALIDATION_SIDE_EFFECT = True\\n')
PY
exit ''' + ('1' if fails else '0') + '\n')
    path = tmp_path / 'config.yaml'
    data = yaml.safe_load(path.read_text())
    data['agent']['validation'] = {'script': str(script), 'timeout_seconds': 30}
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    monkeypatch.setattr(coordinator, 'provision_opencode', lambda *_a, **_kw: inputs.runtime)
    code, run = coordinator.run(config, inputs.dataset, output=tmp_path / 'runs', progress='off')
    report = Report.model_validate(json.loads((run / 'report.json').read_text()))
    row = report.tasks[0]
    assert code == int(fails)
    assert report.run.status == ('completed_with_errors' if fails else 'completed')
    assert row.execution.status == 'completed' and row.execution.exit_code == 0
    assert row.tests is not None and row.tests.status == ('failed' if fails else 'passed')
    assert row.tests.counts is not None and row.tests.counts.total == 1
    assert row.tests.junit is not None and row.tests.log is not None
    assert (run / 'agent/validation.sh').read_bytes() == script.read_bytes()
    assert b'VALIDATION_SIDE_EFFECT' not in (run / row.changes.diff).read_bytes()
    assert row.scoring.status == 'available'
    selected = RunStore.open(run).state.tasks[0]
    assert selected.seal is not None and selected.outcome is not None
    archive = create_archive(run, tmp_path / 'tested.tar.gz')
    assert verify_archive(archive.path).complete
    if exported := os.environ.get('PRSIM_TESTED_FIXTURE_DIR'):
        destination = Path(exported)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / ('failed.tar.gz' if fails else 'passed.tar.gz')).write_bytes(archive.path.read_bytes())
    def no_native_replay(*_args, **_kwargs):
        raise AssertionError('Sealed test results must survive resume without a new agent')
    monkeypatch.setattr(coordinator, 'provision_opencode', no_native_replay)
    assert coordinator.run(config, inputs.dataset, resume=run, progress='off')[0] == int(fails)
    resumed = RunStore.open(run).snapshot().tasks[0]
    assert resumed.tests == row.tests and resumed.final_attempt == row.final_attempt


def test_crash_during_tests_requires_a_new_attempt_without_fabricating_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    script = tmp_path / 'validate.sh'
    script.write_text('''set -eu
echo TESTING_READY
sleep 2
printf '%s' '<testsuite><testcase/></testsuite>' > "$PRSIM_TEST_REPORT"
''')
    path = tmp_path / 'config.yaml'
    data = yaml.safe_load(path.read_text())
    data['agent']['validation'] = {'script': str(script), 'timeout_seconds': 30}
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    run, notice = _crash(tmp_path, 'testing')
    assert notice.descendants
    first = next(run.glob('tasks/*/attempts/001'))
    assert not (first / 'seal.json').exists()
    assert not (first / 'tests.xml').exists()
    monkeypatch.setattr(coordinator, 'provision_opencode', lambda *_a, **_kw: inputs.runtime)
    assert coordinator.run(config, inputs.dataset, resume=run, progress='off')[0] == 0
    row = RunStore.open(run).snapshot().tasks[0]
    assert row.final_attempt is not None and row.final_attempt.sequence == 2
    assert row.tests is not None and row.tests.status == 'passed'
    assert not (first / 'seal.json').exists()
