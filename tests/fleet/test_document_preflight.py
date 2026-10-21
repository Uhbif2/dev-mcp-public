import pytest

from dev_mcp.fleet.spec_preflight import _command, _executable_exists, preflight_task, ProbeNotApplicable
from dev_mcp.fleet.model import Task


def test_document_script_arguments_keep_immutable_preflight_and_do_not_run_fixture(tmp_path):
    script = tmp_path/'tools/tzcheck.py'
    script.parent.mkdir()
    script.write_text('raise RuntimeError("must not execute in fixture")', encoding='utf-8')
    command = 'py -3 tools/tzcheck.py doc docs/DECISION.md --sections Цель Риски --min-bytes 3000'
    argv, error = _command(command, tmp_path)
    assert error is None and _executable_exists(argv, tmp_path)
    calls = []
    def fixture(args, directory):
        calls.append(directory)
        assert directory != tmp_path
        raise ProbeNotApplicable('read-only script is not a fixture probe')
    item = Task('Product','TZ-1','ready',1,'work','','',(),(str(tmp_path/'docs/DECISION.md'),),(),(command,),'Doc','')
    report = preflight_task(item,tmp_path,dry_runner=fixture,require_dry_run=True)
    assert report.action == 'register'
    assert calls and report.checks[0].dry_run == 'not_executed'
    assert report.checks[0].admissible_as_evidence is False


@pytest.mark.parametrize('tail', [
    'doc ../private.md --sections A',
    'doc C:/private.md --sections A',
    'doc docs/ok.md --sections A --execute',
    'doc docs/ok.md --min-bytes 0',
    'doc docs/ok.md --min-bytes 1000001',
    'doc docs/ok.md --sections',
    'doc docs/ok.md --sections A -c print(1)',
    'publish docs/ok.md',
    'doc docs/ok.md;whoami',
])
def test_document_checker_does_not_enable_arbitrary_script_flags(tmp_path, tail):
    assert _command('py -3 tools/tzcheck.py '+tail,tmp_path)[1] is not None
