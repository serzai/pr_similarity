"""Test verdicts require real command execution, valid XML and safe public evidence."""

import shlex
import threading
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from benchmark.execution.process import run_process
from benchmark.execution.validation import run_validation


class LocalTestBackend:
    """Unit adapter for trusted fixture shell scripts; integration tests use real isolation."""

    def __init__(self, root: Path) -> None:
        self.home = root / 'private " & space'
        self.cwd = root / 'source'
        self.runtime = root / 'runtime'
        for path in (self.home, self.cwd, self.runtime):
            path.mkdir()
        self.public_paths = {str(self.home): '/sandbox-home'}
        self.paths = SimpleNamespace(home=str(self.home), runtime_file=lambda name: str(self.runtime / name))

    def environment(self, bindings: dict[str, str]) -> dict[str, str]:
        self.last_environment = {'PATH': '/usr/bin:/bin', 'HOME': str(self.home), **bindings}
        return self.last_environment

    def run_process(self, argv: list[str], **options):
        return run_process(argv, **options)


@pytest.mark.parametrize(('body', 'exit_code', 'status'), [
    ('<testcase/>', 0, 'passed'),
    ('<testcase/>', 1, 'error'),
    ('<testcase><failure/></testcase>', 0, 'failed'),
    ('<testcase><error/></testcase>', 1, 'failed'),
    ('<testcase><skipped/></testcase>', 0, 'error'),
    ('', 0, 'error'),
])
def test_command_and_case_results_both_control_verdict(
    tmp_path: Path, body: str, exit_code: int, status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('OPENCODE_AUTH_CONTENT', 'private-provider-value')
    sandbox = LocalTestBackend(tmp_path)
    (sandbox.runtime / 'validation.sh').write_text(
        'printf %s ' + shlex.quote('<testsuite>' + body + '</testsuite>')
        + ' > "$PRSIM_TEST_REPORT"\nexit ' + str(exit_code) + '\n'
    )
    artifacts = tmp_path / 'run/attempt'; artifacts.mkdir(parents=True)
    result = run_validation(sandbox, artifacts, tmp_path / 'run', timeout=2, grace=.1,
                            cancel=threading.Event(), bindings={}, secrets=['private-provider-value'])
    assert result.status == status
    assert result.exit_code == exit_code
    assert result.junit is not None and result.log is not None
    assert 'OPENCODE_AUTH_CONTENT' not in sandbox.last_environment


def test_rejects_credential_hidden_in_xml_character_references(tmp_path: Path) -> None:
    sandbox = LocalTestBackend(tmp_path)
    xml = '<testsuite><testcase name="&#99;redential-value"/></testsuite>'
    (sandbox.runtime / 'validation.sh').write_text('printf %s ' + shlex.quote(xml) + ' > "$PRSIM_TEST_REPORT"')
    artifacts = tmp_path / 'run/attempt'; artifacts.mkdir(parents=True)
    result = run_validation(sandbox, artifacts, tmp_path / 'run', timeout=2, grace=.1,
                            cancel=threading.Event(), bindings={}, secrets=['credential-value'])
    assert result.status == 'error' and result.reason == 'test_report_contains_credentials'
    assert result.junit is None and not (artifacts / 'tests.xml').exists()


def test_xml_path_aliases_preserve_xml_escaping_and_case_counts(tmp_path: Path) -> None:
    sandbox = LocalTestBackend(tmp_path)
    root = ElementTree.Element('testsuite')
    ElementTree.SubElement(root, 'testcase', name=str(sandbox.home / 'example'))
    xml = ElementTree.tostring(root, encoding='unicode')
    (sandbox.runtime / 'validation.sh').write_text('printf %s ' + shlex.quote(xml) + ' > "$PRSIM_TEST_REPORT"')
    artifacts = tmp_path / 'run/attempt'; artifacts.mkdir(parents=True)
    result = run_validation(sandbox, artifacts, tmp_path / 'run', timeout=2, grace=.1,
                            cancel=threading.Event(), bindings={}, secrets=[])
    assert result.status == 'passed'
    assert ElementTree.parse(artifacts / 'tests.xml').find('testcase').attrib['name'] == '/sandbox-home/example'


def test_timeout_keeps_partial_cases_without_claiming_pass(tmp_path: Path) -> None:
    sandbox = LocalTestBackend(tmp_path)
    (sandbox.runtime / 'validation.sh').write_text(
        'printf %s "<testsuite><testcase/></testsuite>" > "$PRSIM_TEST_REPORT"\nsleep 5\n'
    )
    artifacts = tmp_path / 'run/attempt'; artifacts.mkdir(parents=True)
    result = run_validation(sandbox, artifacts, tmp_path / 'run', timeout=1, grace=.1,
                            cancel=threading.Event(), bindings={}, secrets=[])
    assert result.status == 'timeout' and result.counts is not None and result.counts.passed == 1


def test_cancel_before_start_does_not_fabricate_test_results(tmp_path: Path) -> None:
    sandbox = LocalTestBackend(tmp_path)
    (sandbox.runtime / 'validation.sh').write_text('exit 0\n')
    artifacts = tmp_path / 'run/attempt'; artifacts.mkdir(parents=True)
    cancel = threading.Event(); cancel.set()
    result = run_validation(sandbox, artifacts, tmp_path / 'run', timeout=2, grace=.1,
                            cancel=cancel, bindings={}, secrets=[])
    assert result.status == 'cancelled' and result.counts is None and result.junit is None
