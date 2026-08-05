"""``loupfeed doctor``: checks report, never crash, and never leak values."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.cli import main
from agent.cli.doctor import (
    FAIL,
    PASS,
    DoctorOptions,
    check_endpoints,
    check_env,
    render,
    run_doctor,
)

FULL_ENV = {
    "TEAMS_APP_ID": "x" * 36,
    "TEAMS_APP_PASSWORD": "y" * 40,
    "TEAMS_APP_TENANT_ID": "z" * 36,
    "ATLASSIAN_EMAIL": "agent@example.com",
    "ATLASSIAN_API_TOKEN": "t" * 100,
}


def test_env_check_reports_lengths_not_values():
    with patch.dict("os.environ", FULL_ENV, clear=True):
        results = check_env(DoctorOptions())
    rendered = render(results)
    assert "t" * 20 not in rendered
    assert "agent@example.com" not in rendered
    required = [r for r in results if r.name.startswith("env:TEAMS") or "ATLASSIAN" in r.name]
    assert all(r.status == PASS for r in required)


def test_env_check_fails_on_missing_required():
    with patch.dict("os.environ", {}, clear=True):
        results = check_env(DoctorOptions())
    assert any(r.status == FAIL for r in results)
    assert all(r.fix for r in results if r.status == FAIL)


def _resp(status):
    r = MagicMock()
    r.status_code = status
    return r


def test_teams_webhook_must_reject_unsigned():
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("agent.cli.doctor.requests.post", return_value=_resp(401)),
    ):
        results = check_endpoints(DoctorOptions())
    webhook = next(r for r in results if r.name == "endpoint:teams-webhook")
    assert webhook.status == PASS


def test_teams_webhook_open_endpoint_is_a_failure():
    """A 200 for an unsigned request means the JWT verification is off."""
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("agent.cli.doctor.requests.post", return_value=_resp(200)),
    ):
        results = check_endpoints(DoctorOptions())
    webhook = next(r for r in results if r.name == "endpoint:teams-webhook")
    assert webhook.status == FAIL


def test_broken_check_does_not_hide_the_rest():
    import agent.cli.doctor as doc

    def _boom(_opts):
        raise RuntimeError("boom")

    original = doc.CHECKS
    doc.CHECKS = (doc.check_env, _boom)
    try:
        with patch.dict("os.environ", FULL_ENV, clear=True):
            results = run_doctor()
    finally:
        doc.CHECKS = original
    assert any(r.status == FAIL and "crashed" in r.detail for r in results)
    assert any(r.name.startswith("env:") for r in results)


def test_cli_exit_code_reflects_failures():
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("agent.cli.doctor.requests.post", return_value=_resp(401)),
        patch("agent.cli.doctor.requests.get", return_value=_resp(500)),
    ):
        code = main(["doctor", "--json"])
    assert code == 1
