"""Regression for #117276: ``hermes doctor``'s exit status must agree with its unresolved findings."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("issues,manual,fixed,fix,expected", [
    (["needs repair"], [], 0, False, 1),
    ([], ["manual repair"], 0, False, 1),
    ([], [], 0, False, 0),
    ([], [], 1, True, 0),
    ([], ["remaining repair"], 1, True, 1),
])
def test_doctor_command_reports_remaining_findings(monkeypatch, capsys, issues, manual, fixed, fix, expected):
    import hermes_cli.doctor as doctor
    from hermes_cli.main import cmd_doctor
    from hermes_cli.doctor_report import Finding

    def check(should_fix):
        assert should_fix is fix
        return Finding(issues=issues, manual_issues=manual, fixed=fixed)

    monkeypatch.setattr(doctor, "DOCTOR_CHECKS", ((None, check),))
    result = cmd_doctor(SimpleNamespace(fix=fix, ack=None, live=False))
    output = capsys.readouterr().out
    assert result == expected
    for issue in issues + manual:
        assert issue in output


@pytest.mark.parametrize("unresolved", [False, True])
def test_doctor_cli_process_status_matches_summary(unresolved):
    import subprocess
    import sys
    from pathlib import Path

    program = f"""
import sys
import hermes_cli.doctor as doctor
from hermes_cli.doctor_report import Finding
from hermes_cli.main import main
issues = ['fixture unresolved problem'] if {unresolved!r} else []
doctor.DOCTOR_CHECKS = ((None, lambda fix: Finding(issues=issues)),)
sys.argv = ['hermes', 'doctor']
main()
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == int(unresolved), result.stdout + result.stderr
    assert ("fixture unresolved problem" if unresolved else "All checks passed") in result.stdout


@pytest.mark.parametrize("outcome", ["healthy", "findings", "crash"])
def test_doctor_completed_result_is_written_only_after_checks(tmp_path, outcome):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    receipt = tmp_path / "result.json"
    program = f"""
import sys
import hermes_cli.doctor as doctor
from hermes_cli.doctor_report import Finding
from hermes_cli.main import main

def check(fix):
    assert fix is False
    print('fixture diagnostic report')
    if {outcome!r} == 'crash':
        raise RuntimeError('fixture doctor crash')
    return Finding(issues=['fixture finding'] if {outcome!r} == 'findings' else [])

doctor.DOCTOR_CHECKS = ((None, check),)
sys.argv = ['hermes', 'doctor', '--result-json', {str(receipt)!r}]
main()
"""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "home")
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run([sys.executable, "-c", program],
                            cwd=Path(__file__).resolve().parents[2], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == int(outcome != "healthy"), result.stdout + result.stderr
    assert 'fixture diagnostic report' in result.stdout
    if outcome == "crash":
        assert 'RuntimeError: fixture doctor crash' in result.stderr
        assert not receipt.exists()
    else:
        data = json.loads(receipt.read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        assert data["command"] == "doctor"
        assert data["completed"] is True
        assert data["exit_code"] == result.returncode
        assert data["issues"] == (["fixture finding"] if outcome == "findings" else [])
        assert data["manual_issues"] == []
        assert data["fixed"] == 0
