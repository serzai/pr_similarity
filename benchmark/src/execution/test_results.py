"""Read bounded, regular JUnit evidence without trusting declared aggregate counts."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from benchmark.errors import BenchmarkError


@dataclass(frozen=True)
class TestCounts:
    """Count actual testcase outcomes, including skipped and errored cases."""

    __test__ = False
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int


def read_junit_report(path: Path, *, limit: int = 16 * 1024 * 1024) -> tuple[TestCounts, bytes]:
    """Reject links, oversized reports and entity declarations before parsing XML."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError('Report must be a bounded regular file.')
            content = stream.read(limit + 1)
        if len(content) > limit or any(token in content.upper() for token in (b'<!DOCTYPE', b'<!ENTITY')):
            raise ValueError('Unsupported XML declaration or size.')
        # Restrict the encoding so a UTF-16 declaration cannot bypass the DTD check.
        text = content.decode('utf-8-sig')
        if '\0' in text:
            raise ValueError('Unsupported XML encoding.')
        root = ElementTree.fromstring(text)
        if root.tag not in ('testsuite', 'testsuites'):
            raise ValueError('Unsupported JUnit root.')
        counts = dict(total=0, passed=0, failed=0, errors=0, skipped=0)
        for case in root.iter('testcase'):
            counts['total'] += 1
            outcome = ('errors' if case.find('error') is not None else
                       'failed' if case.find('failure') is not None else
                       'skipped' if case.find('skipped') is not None else 'passed')
            counts[outcome] += 1
        return TestCounts(**counts), content
    except (OSError, ValueError, UnicodeError, ElementTree.ParseError) as error:
        raise BenchmarkError('invalid_test_report', 'Test report is missing, unsafe or invalid JUnit XML.') from error


def read_junit(path: Path, *, limit: int = 16 * 1024 * 1024) -> TestCounts:
    """Read counts from one secure report without retaining testcase output."""
    return read_junit_report(path, limit=limit)[0]


def test_status(exit_code: int | None, counts: TestCounts) -> tuple[str, str | None]:
    """A successful process alone cannot prove that any test executed."""
    if counts.total == counts.skipped:
        return 'error', 'no_tests_executed'
    if counts.failed or counts.errors:
        return 'failed', None
    if exit_code != 0:
        return 'error', 'test_command_failed'
    return 'passed', None


test_status.__test__ = False
