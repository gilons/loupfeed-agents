"""Configurable-tab shim that makes the app installable in private channels.

Teams only offers "add this app here" inside a channel through the tab gallery,
and that gallery lists apps declaring a ``configurableTabs`` entry. A bot-only
manifest therefore cannot be installed into a private or shared channel at all:
team-level installs are not inherited by private channels, and the bot never
receives their activities (an @mention resolves in the compose box but is
silently dropped).

These two pages exist purely so the app shows up in that gallery. The config
page saves a fixed content URL and marks itself valid immediately, so adding
the tab is one click; the content page is a short "what to do next" card.

Served from the same public host as the bot endpoint, which is already listed
in the manifest's ``validDomains``.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/teams/tab", tags=["teams"])

_TEAMS_SDK = "https://res.cdn.office.net/teams-js/2.24.0/js/MicrosoftTeams.min.js"

_STYLE = """
  :root { color-scheme: light dark; }
  body { margin: 0; padding: 24px;
         font: 15px/1.5 "Segoe UI", -apple-system, system-ui, sans-serif; }
  h1 { font-size: 18px; margin: 0 0 8px; }
  p { margin: 0 0 12px; max-width: 46em; }
  code { padding: 1px 5px; border-radius: 4px;
         background: rgba(128, 128, 128, 0.18); }
"""


@router.get("/config", response_class=HTMLResponse)
async def tab_config() -> str:
    """Tab configuration page — self-validates and saves a fixed content URL."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Add loupfeed</title>
<script src="{_TEAMS_SDK}"></script><style>{_STYLE}</style></head>
<body>
  <h1>Add loupfeed to this channel</h1>
  <p>loupfeed answers when you @mention it. Adding it here also lets it read
     this channel's conversation, files and meeting transcripts so it can
     triage them into your planning system.</p>
  <p>Click <strong>Save</strong> to finish.</p>
<script>
microsoftTeams.app.initialize().then(function () {{
  microsoftTeams.pages.config.registerOnSaveHandler(function (event) {{
    microsoftTeams.pages.config.setConfig({{
      entityId: "loupfeed",
      contentUrl: window.location.origin + "/teams/tab",
      websiteUrl: "https://github.com/gilons/loupfeed-agents",
      suggestedDisplayName: "loupfeed",
    }});
    event.notifySuccess();
  }});
  microsoftTeams.pages.config.setValidityState(true);
}});
</script>
</body></html>"""


@router.get("", response_class=HTMLResponse)
async def tab_content() -> str:
    """Tab content page."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>loupfeed</title>
<script src="{_TEAMS_SDK}"></script><style>{_STYLE}</style></head>
<body>
  <h1>loupfeed is connected to this channel</h1>
  <p>Mention <code>@loupfeed</code> in a message or thread to ask about or act
     on your planning system — initiatives, PRDs, tech specs, bugs.</p>
  <p>After a call, mention it in the meeting chat and it will read the
     transcript, write the dated capture page, and propose next steps.</p>
<script>microsoftTeams.app.initialize();</script>
</body></html>"""
