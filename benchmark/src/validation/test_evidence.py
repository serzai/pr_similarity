"""Bind mandatory test facts to one registered attempt and its measured XML."""

from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.execution.test_results import read_junit
from benchmark.models.base import ArtifactReceipt
from benchmark.models.report import ReportTests
from benchmark.persistence.layout import artifact_path


def _require(condition: object) -> None:
    if not condition:
        raise BenchmarkError('invalid_test_evidence', 'Project-test result differs from its owned evidence.')


def validate_test_summary(tests: ReportTests | None) -> None:
    if tests is None:
        return
    counts = tests.counts
    _require((counts is None) == (tests.junit is None))
    if counts is not None:
        values = (counts.total, counts.passed, counts.failed, counts.errors, counts.skipped)
        _require(all(type(value) is int and value >= 0 for value in values))
        _require(counts.total == counts.passed + counts.failed + counts.errors + counts.skipped)
    if tests.status == 'passed':
        _require(tests.exit_code == 0 and counts is not None)
        assert counts is not None
        _require(counts.passed > 0 and counts.failed == counts.errors == 0)
    elif tests.status == 'failed':
        _require(counts is not None and counts.failed + counts.errors > 0)
    elif tests.status == 'not_started':
        _require(counts is None and tests.exit_code is None and tests.duration_seconds is None)


def validate_test_evidence(
    directory: Path, tests: ReportTests | None, prefix: str,
    receipts: list[ArtifactReceipt], *, required: bool,
) -> None:
    _require((tests is not None) == required)
    validate_test_summary(tests)
    if tests is None:
        return
    owned = {item.path: item for item in receipts}
    if tests.log is not None:
        _require(tests.log == prefix + 'tests.log' and tests.log in owned)
    if tests.junit is not None:
        _require(tests.junit.path == prefix + 'tests.xml' and owned.get(tests.junit.path) == tests.junit)
        measured = read_junit(artifact_path(directory, tests.junit.path, must_exist=True))
        assert tests.counts is not None
        _require(tests.counts.to_wire() == {
            name: getattr(measured, name) for name in ('total', 'passed', 'failed', 'errors', 'skipped')
        })
