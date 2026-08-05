# Standard setup: Teams + Atlassian for a loupfeed agents deployment

The complete, ordered process for wiring a loupfeed agents deployment into a
Microsoft 365 tenant and an Atlassian organization. Every step and every
warning below was learned on a real deployment; follow the order, the order
is load-bearing. See `docs/generic-connect` in the loupfeed monorepo for the
plan to automate most of this away.

Legend: **[admin]** needs a tenant/org admin in a browser. **[cli]** is
scriptable today. Everything else is platform configuration.

---

## 0. Prerequisites

- A deployed loupfeed-agents instance with a public HTTPS endpoint
  (`PUBLIC_BASE_URL`, e.g. behind CloudFront). The endpoint must forward
  query strings (OAuth callbacks arrive as query params).
- A secrets store the deployment's env renderer reads from. Never bake
  credentials into env files by hand; put them in the store and re-render.
- The brand icon as SVG (rendered to PNG at 192px for the Teams manifest and
  480px for avatars; `rsvg-convert -w 480 -h 480 icon.svg -o icon.png`).

## 1. Microsoft side

### 1.1 App registration (Entra) **[admin]**

One Entra app serves both the Bot Framework identity and app-only Graph
access. Create it (or reuse the bot registration) and record:
`TEAMS_APP_ID` (client id), `TEAMS_APP_PASSWORD` (client secret),
`TEAMS_APP_TENANT_ID`.

> **Gotcha:** bots created in the Teams Developer Portal are single-tenant.
> Leave `TEAMS_APP_TENANT_ID` set; the botframework.com default audience is
> only for true multi-tenant bots, and outbound replies fail with an
> authentication error if this is wrong.

### 1.2 Bot registration (Teams Developer Portal) **[admin]**

No Azure subscription is required: dev.teams.microsoft.com > Tools > Bot
management. Set the messaging endpoint to
`{PUBLIC_BASE_URL}/webhooks/teams`.

### 1.3 Graph application permissions **[admin]**

On the app registration, add **Application** permissions and click **Grant
admin consent**:

| Permission | What it powers |
| --- | --- |
| `User.Read.All` | requester identity, people lookups |
| `Sites.Read.All` | channel recordings + shared files (SharePoint) |
| `OnlineMeetingTranscript.Read.All` | transcripts of scheduled meetings |
| `Calendars.ReadWrite` | `graph_create_meeting` on the requester's calendar |

Deliberately NOT granted: `Files.Read.All` (OneDrive-shared chat files stay
inaccessible by design), `Chat.Read.All` (chat access comes per-install via
RSC instead, see 1.4).

> **Gotcha:** the running service caches its Graph token for up to an hour.
> After a new grant, restart the service or wait out the cache.
>
> **Hardening (optional):** `Calendars.ReadWrite` app-only can write any
> mailbox. Scope it with an Exchange application access policy bound to a
> security group of allowed users.

### 1.4 Teams app package

Build the manifest from `deploy/teams/manifest.template.json` (substitute
`${TEAMS_APP_ID}`, zip with `color.png` + `outline.png`). Rules the template
already encodes; do not regress them:

- Manifest schema **v1.25**, `supportsChannelFeatures: "tier1"` AND a
  `configurableTabs` entry. Both are required for private/shared channels:
  the channel `+` Apps picker is the tab gallery, so a bot-only manifest can
  never be installed there. The failure is silent (mentions autocomplete but
  activities are never delivered).
- Do NOT add `supportedChannelTypes`; it is deprecated in v1.25 and mutually
  exclusive with `supportsChannelFeatures` (upload fails).
- RSC `authorization.permissions.resourceSpecific` carries all read grants
  (`ChannelMessage.Read.Group`, `ChatMessage.Read.Chat`, transcript, file and
  member reads). These activate **per install**, per team/chat/meeting; no
  tenant-wide grant is involved.

Upload to the org catalog **[admin]** and then install:

> **Gotcha 1:** upload and install ONLY from teams.microsoft.com in a
> browser. The desktop client serves a cached old package and grants
> nothing, with no error.
>
> **Gotcha 2:** RSC message-read means Teams delivers EVERY message in
> installed surfaces to the bot endpoint. The adapter's mention gate is what
> keeps the bot silent on team chatter; never remove it.
>
> **Gotcha 3 (platform limitation, unresolved):** bots cannot deliver
> messages in private channels (Microsoft postponed the capability). Use a
> group chat as the fallback surface. Group chats are flat: no threads,
> mention required for every request.

### 1.5 Agent mailbox identity (optional but recommended)

Create an M365 group `agent-name@your-domain` (M365 admin center, not the
Entra blade; only the admin center lets you pick your vanity domain). Owner:
the operating admin. Enable "people outside the organization can email this
group" BEFORE using it to receive third-party signup mail. This mailbox is
the agent's account identity on other platforms (Atlassian below).

## 2. Atlassian side

### 2.1 Agent account **[admin]**

Invite the agent mailbox to the Atlassian org with **Jira User + Confluence
User** roles only (no JSM/CSM; those consume support-agent seats). Accept
the invite from the mailbox, set the display name and avatar. On Free plans
this consumes one of the 10 seats.

### 2.2 MCP connector (the main read/write path) **[admin]**

1. admin.atlassian.com > Rovo > Rovo MCP server > **Domains**: add
   `{PUBLIC_BASE_URL}/**`. This allowlist, not app access settings, is what
   gates OAuth callback domains.
2. Visit `{PUBLIC_BASE_URL}/connectors/atlassian/start` **logged in as the
   agent account** and approve for your site. Whoever consents becomes the
   author of everything the agent writes; do not consent as yourself.

> **Gotcha:** the grant's resource list is snapshotted at consent. If a Jira
> or Confluence product was provisioned minutes before, wait a few minutes
> and redo the consent, otherwise the token covers only the older product.
>
> **Gotcha:** connector tools are cached ~5 minutes in the service; after a
> reconnect, wait it out before verifying.

### 2.3 API token (for the REST-only gaps)

The MCP toolset has no folder operations and no attachment upload. The
platform's `confluence_file_capture` and `confluence_attach_image` tools go
straight to the REST API with Basic auth. Create an API token as the agent
account and set in the secrets store: `ATLASSIAN_EMAIL` (agent account
email), `ATLASSIAN_API_TOKEN`, `ATLASSIAN_SITE_URL`. Restart the service.

> **Gotcha:** if these are unset the filing tools fail politely and capture
> pages pile up at the space root. Verify with the whoami check below.

## 3. Transcription (optional)

`transcribe_recording` needs `ASSEMBLYAI_API_KEY`. Keys are not
region-scoped; the EU endpoint is pinned in code. Nothing else to configure.

## 4. Org overlay

Deployment-specific behavior lives OUTSIDE the platform:

- `/etc/loupfeed/agent.env`: org values (allowed GitHub orgs, model id).
- `/etc/loupfeed/pm-prompt.md`: workspace conventions (project keys, filing
  conventions, tone rules). Re-read per run; no restart needed.

## 5. Verification checklist

Run after any credential change. All of these exist as one-off scripts from
the reference deployment; fold them into a `doctor` command eventually.

1. Service healthy: `systemctl is-active`, `:2024` health 200.
2. Env rendered: every expected key has a non-zero value length (check
   lengths, never print values).
3. Graph: create + delete a test calendar event as the operating admin.
4. Atlassian REST identity: `GET /wiki/rest/api/user/current` returns the
   AGENT account, not a human.
5. Atlassian MCP identity: call the connector's user-info tool; the reply
   must name the agent account.
6. Teams: mention the bot 1:1 (instant reply), in a public channel (reply in
   thread), and confirm silence on untagged channel chatter.

## 6. Known operational gotchas

- The box repo is pulled as the service user (`sudo -u <user> git pull`); a
  pull as root leaves root-owned objects that break every later pull.
- Prompt-overlay pulls need an ephemeral installation token; never persist a
  tokenized git remote (App tokens expire hourly and rot the remote).
- The model has no clock; `CurrentTimeMiddleware` stamps every model call.
  If dates drift, check that middleware is wired before anything else.
- Confluence folder titles are unique per SPACE. Date trees must be
  slug-prefixed (`standups 2026-08`); a bare `2026` can exist only once.
