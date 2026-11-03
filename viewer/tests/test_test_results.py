"""Public test status is independently tied to one attempt's JUnit bytes."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from viewer.errors import ViewerError
from viewer.config import Limits
from viewer.models.report import ReportAgentInputConfig, ReportTestCounts, ReportTests
from viewer.util import digest
from viewer.validation.test_results import validate_test_evidence, validate_test_summary
from viewer.validation.archive import validate_archive


PREFIX = 'tasks/task/attempts/001/'


def result(content: bytes) -> tuple[ReportTests, dict]:
    receipt = ReportAgentInputConfig(path=PREFIX + 'tests.xml', size_bytes=len(content), sha256=digest(content))
    value = ReportTests(
        status='passed', exit_code=0, duration_seconds=1,
        counts=ReportTestCounts(total=1, passed=1, failed=0, errors=0, skipped=0),
        log=PREFIX + 'tests.log', junit=receipt, reason=None,
    )
    return value, {receipt.path: receipt, PREFIX + 'tests.log': SimpleNamespace()}


def test_valid_owned_junit_is_accepted() -> None:
    xml = b'<testsuite><testcase name="check"/></testsuite>'
    value, inventory = result(xml)
    validate_test_evidence(value, PREFIX, inventory, lambda _: xml)


@pytest.mark.parametrize('xml', [
    b'<testsuite><testcase><failure/></testcase></testsuite>',
    b'<testsuite><testcase><skipped/></testcase></testsuite>',
    b'<testsuite/>', b'<not-junit/>',
    b'<!DOCTYPE testsuite><testsuite><testcase/></testsuite>',
])
def test_rejects_fabricated_pass_even_when_all_digests_are_recomputed(xml: bytes) -> None:
    value, inventory = result(xml)
    with pytest.raises(ViewerError, match='evidence|JUnit'):
        validate_test_evidence(value, PREFIX, inventory, lambda _: xml)


def test_junit_cannot_belong_to_another_attempt() -> None:
    xml = b'<testsuite><testcase/></testsuite>'
    value, inventory = result(xml)
    with pytest.raises(ViewerError):
        validate_test_evidence(value, PREFIX.replace('/001/', '/002/'), inventory, lambda _: xml)


def test_report_only_pass_requires_a_successful_process_and_nonempty_population() -> None:
    value, _ = result(b'<testsuite><testcase/></testsuite>')
    value.exit_code = 1
    with pytest.raises(ViewerError):
        validate_test_summary(value)
    value.exit_code = 0
    value.counts = ReportTestCounts(total=0, passed=0, failed=0, errors=0, skipped=0)
    with pytest.raises(ViewerError):
        validate_test_summary(value)


def test_historical_absent_tests_stay_absent() -> None:
    validate_test_summary(None)
    validate_test_evidence(None, PREFIX, {}, lambda _: b'')


def test_actual_required_test_archives_are_accepted_by_independent_viewer(tmp_path: Path) -> None:
    benchmark = Path(__file__).parents[2] / 'benchmark'
    interpreter = benchmark / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('requires sibling benchmark module venv')
    exported = tmp_path / 'producer'
    process = subprocess.run(
        [str(interpreter), '-m', 'pytest', 'tests/test_validation_workflow.py', '-k', 'not crash', '-q'],
        cwd=benchmark, env={**os.environ, 'PYTHONHASHSEED': '42', 'PRSIM_TESTED_FIXTURE_DIR': str(exported)},
        capture_output=True, text=True, timeout=300,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    for status in ('passed', 'failed'):
        imported = validate_archive(exported / (status + '.tar.gz'), tmp_path / status, Limits())
        tests = imported.report.tasks[0].tests
        assert tests is not None and tests.status == status
        assert tests.counts is not None and tests.counts.total == 1
        assert imported.report.run.status == ('completed' if status == 'passed' else 'completed_with_errors')
