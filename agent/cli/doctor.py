"""``loupfeed doctor`` — the deployment verification checklist as code.

Every check here mirrors a step in ``docs/standard-setup.md`` and exists
because its failure mode was hit on a real deployment. Checks never print
secret values, only presence and length. Exit code is non-zero when any
check fails, so the command can gate setup phases and CI.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field

import requests

_TIMEOUT = 20

PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

_ICON = {PASS: "✓", WARN: "!", FAIL: "✗", SKIP: "-"}


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class DoctorOptions:
    write_checks: bool = False
    calendar_mailbox: str = ""
    local_base_url: str = "http://localhost:2024"
    results: list[CheckResult] = field(default_factory=list)


REQUIRED_ENV = (
    "TEAMS_APP_ID",
    "TEAMS_APP_PASSWORD",
    "TEAMS_APP_TENANT_ID",
    "ATLASSIAN_EMAIL",
    "ATLASSIAN_API_TOKEN",
)
RECOMMENDED_ENV = (
    "CONNECTOR_PUBLIC_BASE_URL",
    "ASSEMBLY_AI_API_KEY",
    "TOKEN_ENCRYPTION_KEY",
)

# Connectors the pm agent cannot function without; anything else registered
# but unconnected (e.g. the optional delegated ms365 fallback) is a warning.
REQUIRED_CONNECTORS = frozenset({"atlassian"})


def _site_base() -> str:
    return os.environ.get("ATLASSIAN_SITE_URL", "https://dinolabgmbh.atlassian.net").rstrip("/")


def check_env(_: DoctorOptions) -> list[CheckResult]:
    out: list[CheckResult] = []
    for key in REQUIRED_ENV:
        n = len(os.environ.get(key, ""))
        out.append(
            CheckResult(
                f"env:{key}",
                PASS if n else FAIL,
                f"length {n}",
                "" if n else "set it in the secrets store and re-render the env",
            )
        )
    for key in RECOMMENDED_ENV:
        n = len(os.environ.get(key, ""))
        out.append(
            CheckResult(
                f"env:{key}",
                PASS if n else WARN,
                f"length {n}",
                "" if n else "recommended; some features stay off without it",
            )
        )
    return out


def check_graph(opts: DoctorOptions) -> list[CheckResult]:
    from ..utils.msgraph import GRAPH_BASE, get_graph_app_token

    token = get_graph_app_token()
    if not token:
        return [
            CheckResult(
                "graph:token",
                FAIL,
                "no app token",
                "check TEAMS_APP_ID / TEAMS_APP_PASSWORD / TEAMS_APP_TENANT_ID",
            )
        ]
    out = [CheckResult("graph:token", PASS, "app token minted")]

    r = requests.get(
        f"{GRAPH_BASE}/users?$top=1",
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
    )
    out.append(
        CheckResult(
            "graph:directory",
            PASS if r.status_code == 200 else FAIL,
            f"status {r.status_code}",
            "" if r.status_code == 200 else "grant User.Read.All (application) + admin consent",
        )
    )

    if not opts.write_checks or not opts.calendar_mailbox:
        out.append(
            CheckResult(
                "graph:calendar",
                SKIP,
                "write check off",
                "run with --write-checks --calendar-mailbox <email> to prove Calendars.ReadWrite",
            )
        )
        return out

    ev = requests.post(
        f"{GRAPH_BASE}/users/{opts.calendar_mailbox}/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "subject": "loupfeed doctor (auto-deleted)",
            "start": {"dateTime": "2030-01-01T09:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2030-01-01T09:15:00", "timeZone": "UTC"},
        },
        timeout=_TIMEOUT,
    )
    if ev.status_code >= 400:
        out.append(
            CheckResult(
                "graph:calendar",
                FAIL,
                f"create status {ev.status_code}",
                "grant Calendars.ReadWrite (application) + admin consent; token cache lasts up to 1h",
            )
        )
        return out
    event_id = ev.json().get("id", "")
    dr = requests.delete(
        f"{GRAPH_BASE}/users/{opts.calendar_mailbox}/events/{event_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
    )
    out.append(
        CheckResult(
            "graph:calendar",
            PASS if dr.status_code == 204 else WARN,
            f"create ok, cleanup status {dr.status_code}",
            "" if dr.status_code == 204 else "delete the doctor test event manually",
        )
    )
    return out


def _rest_identity() -> tuple[str, str]:
    """(displayName, accountId) of the REST credential, empty strings on failure."""
    r = requests.get(
        f"{_site_base()}/wiki/rest/api/user/current",
        auth=(os.environ.get("ATLASSIAN_EMAIL", ""), os.environ.get("ATLASSIAN_API_TOKEN", "")),
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        return "", ""
    d = r.json()
    return str(d.get("displayName") or ""), str(d.get("accountId") or "")


def check_atlassian_rest(_: DoctorOptions) -> list[CheckResult]:
    name, account_id = _rest_identity()
    if not account_id:
        return [
            CheckResult(
                "atlassian:rest",
                FAIL,
                "identity call failed",
                "check ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN / ATLASSIAN_SITE_URL",
            )
        ]
    return [CheckResult("atlassian:rest", PASS, f"acting as {name}")]


def check_atlassian_connector(_: DoctorOptions) -> list[CheckResult]:
    from ..connector_auth import connection_status

    status = connection_status()
    if not status:
        return [CheckResult("atlassian:connector", SKIP, "no connectors registered")]
    out: list[CheckResult] = []
    for cname, s in status.items():
        connected = bool(s.get("connected"))
        severity = FAIL if cname in REQUIRED_CONNECTORS else WARN
        out.append(
            CheckResult(
                f"connector:{cname}",
                PASS if connected else severity,
                "connected"
                if connected
                else "not connected (optional)"
                if severity == WARN
                else "not connected",
                ""
                if connected
                else f"visit /connectors/{cname}/start logged in AS THE AGENT ACCOUNT",
            )
        )
    if not status.get("atlassian", {}).get("connected"):
        return out

    async def _mcp_identity() -> str:
        from ..pm_connectors import load_connector_tools

        tools = await load_connector_tools()
        who = next((t for t in tools if "userinfo" in t.name.lower()), None)
        if who is None:
            return ""
        result = await who.ainvoke({})
        return result if isinstance(result, str) else json.dumps(result)

    try:
        identity_blob = asyncio.run(_mcp_identity())
    except Exception as exc:  # noqa: BLE001 — a doctor reports, it never crashes
        out.append(
            CheckResult(
                "atlassian:mcp-identity", FAIL, f"{type(exc).__name__}", "check platform logs"
            )
        )
        return out

    if not identity_blob:
        out.append(CheckResult("atlassian:mcp-identity", WARN, "no user-info tool exposed"))
        return out

    _, rest_account = _rest_identity()
    if rest_account and rest_account in identity_blob:
        out.append(CheckResult("atlassian:mcp-identity", PASS, "matches the REST identity"))
    else:
        out.append(
            CheckResult(
                "atlassian:mcp-identity",
                WARN,
                "MCP and REST identities differ",
                "writes will carry two different authors; redo the connector consent "
                "as the agent account",
            )
        )
    return out


def check_prompt_overlay(_: DoctorOptions) -> list[CheckResult]:
    path = os.environ.get("PM_PROMPT_EXTRA_FILE", "/etc/loupfeed/pm-prompt.md")
    try:
        size = len(open(path, encoding="utf-8").read().strip())
    except OSError:
        return [
            CheckResult(
                "overlay:pm-prompt",
                WARN,
                f"{path} unreadable",
                "the agent runs org-agnostic without workspace conventions",
            )
        ]
    return [CheckResult("overlay:pm-prompt", PASS if size else WARN, f"{size} chars")]


def check_endpoints(opts: DoctorOptions) -> list[CheckResult]:
    out: list[CheckResult] = []
    public = os.environ.get("CONNECTOR_PUBLIC_BASE_URL", "").rstrip("/")
    if public:
        try:
            r = requests.get(f"{public}/connectors/status", timeout=_TIMEOUT)
            out.append(
                CheckResult(
                    "endpoint:public",
                    PASS if r.status_code == 200 else FAIL,
                    f"status {r.status_code}",
                    "" if r.status_code == 200 else "check the CDN/route to the deployment",
                )
            )
        except requests.RequestException as exc:
            out.append(CheckResult("endpoint:public", FAIL, type(exc).__name__))
    else:
        out.append(CheckResult("endpoint:public", SKIP, "CONNECTOR_PUBLIC_BASE_URL not set"))

    base = public or opts.local_base_url
    try:
        r = requests.post(f"{base}/webhooks/teams", json={}, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        out.append(CheckResult("endpoint:teams-webhook", FAIL, type(exc).__name__))
        return out
    if r.status_code in (401, 403):
        out.append(CheckResult("endpoint:teams-webhook", PASS, "rejects unsigned requests"))
    elif r.status_code == 404:
        out.append(
            CheckResult(
                "endpoint:teams-webhook", FAIL, "route missing", "adapter not mounted on this app"
            )
        )
    elif r.status_code == 503:
        out.append(
            CheckResult(
                "endpoint:teams-webhook",
                FAIL,
                "adapter unconfigured",
                "TEAMS_APP_ID / TEAMS_APP_PASSWORD not loaded by the service",
            )
        )
    else:
        out.append(
            CheckResult(
                "endpoint:teams-webhook",
                FAIL,
                f"status {r.status_code} for an unsigned request",
                "the endpoint must reject unauthenticated calls",
            )
        )
    return out


CHECKS: tuple[Callable[[DoctorOptions], list[CheckResult]], ...] = (
    check_env,
    check_graph,
    check_atlassian_rest,
    check_atlassian_connector,
    check_prompt_overlay,
    check_endpoints,
)


def run_doctor(opts: DoctorOptions | None = None) -> list[CheckResult]:
    opts = opts or DoctorOptions()
    results: list[CheckResult] = []
    for check in CHECKS:
        try:
            results.extend(check(opts))
        except Exception as exc:  # noqa: BLE001 — one broken check must not hide the rest
            results.append(CheckResult(f"{check.__name__}", FAIL, f"crashed: {type(exc).__name__}"))
    opts.results = results
    return results


def render(results: list[CheckResult]) -> str:
    width = max(len(r.name) for r in results) + 2
    lines = []
    for r in results:
        line = f" {_ICON[r.status]} {r.name.ljust(width)} {r.detail}"
        if r.fix:
            line += f"\n     fix: {r.fix}"
        lines.append(line)
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, WARN, FAIL, SKIP)}
    lines.append(
        f"\n {counts[PASS]} passed, {counts[WARN]} warnings, "
        f"{counts[FAIL]} failed, {counts[SKIP]} skipped"
    )
    return "\n".join(lines)
