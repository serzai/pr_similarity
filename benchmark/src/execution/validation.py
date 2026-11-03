"""Execute required project tests inside the attempt's existing isolation boundary."""

import html
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from xml.etree import ElementTree

from benchmark.errors import BenchmarkError
from benchmark.execution.process import Redactor
from benchmark.execution.sandbox import Sandbox
from benchmark.execution.test_results import read_junit, read_junit_report, test_status
from benchmark.models.report import ReportTestCounts, ReportTests
from benchmark.persistence.artifact_refs import reference
from benchmark.persistence.storage import atomic_write


def run_validation(
    sandbox: Sandbox, artifacts: Path, run_directory: Path, *, timeout: int,
    grace: float, cancel: threading.Event, bindings: dict[str, str], secrets: list[str],
) -> ReportTests:
    """Keep tests independent of agent exit and never forward provider credentials."""
    name = 'test-results-' + uuid.uuid4().hex + '.xml'
    private_report = sandbox.home / name
    environment = sandbox.environment(bindings)
    environment['PRSIM_TEST_REPORT'] = sandbox.paths.home + '/' + name
    log = artifacts / 'tests.log'
    result = sandbox.run_process(
        ['/bin/bash', sandbox.paths.runtime_file('validation.sh')],
        env=environment, cwd=sandbox.cwd, timeout=timeout, grace=grace,
        cancel=cancel, log=log, secrets=secrets,
    )
    status = result.status if result.status in ('timeout', 'cancelled') else 'error'
    reason = None
    counts = None
    receipt = None
    public_report = artifacts / 'tests.xml'
    try:
        measured, content = read_junit_report(private_report)
        root = ElementTree.fromstring(content.decode('utf-8-sig'))
        decoded = ''.join(root.itertext()) + ''.join(value for node in root.iter() for value in node.attrib.values())
        if any(secret and (secret.encode() in content or html.escape(secret).encode() in content or secret in decoded)
               for secret in secrets):
            raise BenchmarkError('test_report_contains_credentials', 'Test report contains a known credential.')
        aliases = dict(sandbox.public_paths)
        for path, alias in list(aliases.items()):
            escaped = html.escape(path, quote=False)
            for variant in (html.escape(path), escaped, escaped.replace('"', '&quot;')):
                aliases[variant] = alias
        def public(value: str) -> str:
            return Redactor([], replacements=aliases).feed(value.encode(), final=True).decode()

        for node in root.iter():
            if node.text is not None:
                node.text = public(node.text)
            if node.tail is not None:
                node.tail = public(node.tail)
            node.attrib = {key: public(value) for key, value in node.attrib.items()}
        atomic_write(public_report, ElementTree.tostring(root, encoding='utf-8', xml_declaration=True))
        if read_junit(public_report) != measured:
            public_report.unlink()
            raise BenchmarkError('invalid_test_report', 'Public test evidence differs from measured test counts.')
        counts = ReportTestCounts(**asdict(measured))
        receipt = reference(run_directory, public_report.relative_to(run_directory).as_posix())
        if result.status not in ('timeout', 'cancelled'):
            status, reason = test_status(result.exit_code, measured)
    except BenchmarkError as error:
        public_report.unlink(missing_ok=True)
        reason = error.code
    finally:
        # The random result name is launcher-owned; never recurse into source or HOME.
        if private_report.is_symlink() or private_report.is_file():
            private_report.unlink()
    return ReportTests(
        status=status, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
        counts=counts, log=log.relative_to(run_directory).as_posix() if log.is_file() else None,
        junit=receipt, reason=reason,
    )
