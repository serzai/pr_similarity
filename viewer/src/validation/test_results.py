"""Independently verify project-test outcomes against owned JUnit evidence."""

from collections.abc import Callable, Mapping
from xml.etree import ElementTree

from viewer.errors import ViewerError
from viewer.models.report import ReportTests
from viewer.util import digest


def _require(condition: object) -> None:
    if not condition:
        raise ViewerError('invalid_test_evidence', 'Project-test result differs from its owned evidence.')


def validate_test_summary(tests: ReportTests | None) -> None:
    """Unknown and empty test populations must never become passing results."""
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
    tests: ReportTests | None, prefix: str, inventory: Mapping,
    read_file: Callable[[str], bytes],
) -> None:
    """Bind a test log/report to one native attempt and recompute case counts."""
    validate_test_summary(tests)
    if tests is None:
        return
    if tests.log is not None:
        _require(tests.log == prefix + 'tests.log' and tests.log in inventory)
    if tests.junit is None:
        return
    receipt = tests.junit
    _require(receipt.path == prefix + 'tests.xml' and receipt.path in inventory)
    content = read_file(receipt.path)
    item = inventory[receipt.path]
    _require(len(content) == receipt.size_bytes == item.size_bytes)
    _require(digest(content) == receipt.sha256 == item.sha256)
    _require(len(content) <= 16 * 1024 * 1024)
    try:
        _require(b'<!DOCTYPE' not in content.upper() and b'<!ENTITY' not in content.upper())
        text = content.decode('utf-8-sig')
        _require('\0' not in text)
        root = ElementTree.fromstring(text)
        _require(root.tag in ('testsuite', 'testsuites'))
        measured = dict(total=0, passed=0, failed=0, errors=0, skipped=0)
        for case in root.iter('testcase'):
            measured['total'] += 1
            outcome = 'passed'
            for tag, category in (('error', 'errors'), ('failure', 'failed'), ('skipped', 'skipped')):
                if case.find(tag) is not None:
                    outcome = category
                    break
            measured[outcome] += 1
        _require(tests.counts is not None and tests.counts.to_wire() == measured)
    except (UnicodeError, ElementTree.ParseError) as error:
        raise ViewerError('invalid_test_evidence', 'Invalid JUnit evidence.') from error
