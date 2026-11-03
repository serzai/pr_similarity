"""Required test evidence must distinguish passing tests from skipped or invalid runs."""

from pathlib import Path

import pytest

from benchmark.errors import BenchmarkError
from benchmark.execution.test_results import read_junit, test_status


def test_counts_cases_instead_of_trusting_suite_totals(tmp_path: Path) -> None:
    path = tmp_path / 'junit.xml'
    path.write_text('''<testsuites tests="999" failures="0"><testsuite>
      <testcase name="pass"/><testcase name="fail"><failure/></testcase>
      <testcase name="error"><error/></testcase><testcase name="skip"><skipped/></testcase>
    </testsuite></testsuites>''')
    counts = read_junit(path)
    assert (counts.total, counts.passed, counts.failed, counts.errors, counts.skipped) == (4, 1, 1, 1, 1)
    assert test_status(0, counts) == ('failed', None)


@pytest.mark.parametrize(('body', 'exit_code', 'expected'), [
    ('<testcase/>', 0, ('passed', None)),
    ('<testcase/>', 1, ('error', 'test_command_failed')),
    ('<testcase/>', None, ('error', 'test_command_failed')),
    ('', 0, ('error', 'no_tests_executed')),
    ('<testcase><skipped/></testcase>', 0, ('error', 'no_tests_executed')),
    ('<testcase><failure/></testcase>', 1, ('failed', None)),
])
def test_requires_executed_tests_and_successful_command(
    tmp_path: Path, body: str, exit_code: int | None, expected: tuple[str, str | None]
) -> None:
    path = tmp_path / 'junit.xml'
    path.write_text('<testsuite>' + body + '</testsuite>')
    assert test_status(exit_code, read_junit(path)) == expected


@pytest.mark.parametrize('content', [
    b'<testsuite>', b'<unrelated/>', b'<!DOCTYPE testsuite><testsuite/>',
    b'<!DOCTYPE testsuite [<!ENTITY test "value">]><testsuite/>',
    '<!DOCTYPE testsuite><testsuite/>'.encode('utf-16'),
])
def test_rejects_malformed_or_entity_bearing_reports(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / 'junit.xml'; path.write_bytes(content)
    with pytest.raises(BenchmarkError, match='Test report'):
        read_junit(path)


def test_rejects_missing_link_directory_and_oversized_report(tmp_path: Path) -> None:
    report = tmp_path / 'real.xml'; report.write_text('<testsuite><testcase/></testsuite>')
    link = tmp_path / 'link.xml'; link.symlink_to(report)
    for path in (tmp_path / 'missing.xml', link, tmp_path):
        with pytest.raises(BenchmarkError):
            read_junit(path)
    with pytest.raises(BenchmarkError):
        read_junit(report, limit=8)
