# Teams app setup

The Teams adapter is served by the platform at `POST /webhooks/teams`
(publicly: `https://<your-domain>/webhooks/teams`).

## One-time Azure/Teams registration

1. **Entra app registration** (portal.azure.com → App registrations → New):
   single tenant, no redirect URI. Note the **Application (client) ID** and
   create a **client secret**. These become `TEAMS_APP_ID` /
   `TEAMS_APP_PASSWORD` (+ `TEAMS_APP_TENANT_ID` = your tenant id) in the
   platform secret.
2. **Bot registration**: either an **Azure Bot resource** (Create resource →
   Azure Bot, same app id, Single Tenant) or — with no Azure subscription —
   the **Teams Developer Portal** (dev.teams.microsoft.com → Tools → Bot
   management). Either way, set the **messaging endpoint** to
   `https://<your-domain>/webhooks/teams` and enable the **Microsoft Teams
   channel**.
3. **Teams app package**: render it, do not hand-edit it. The template carries
   no organisation's values, and every one of them comes from two variables:

   ```bash
   TEAMS_APP_ID=<your Entra app id> \
   LOUPFEED_PUBLIC_BASE_URL=https://agents.example.dev \
     ./render-manifest
   ```

   That writes `build/loupfeed-teams.zip` (manifest plus both icons) and refuses
   to produce a package with any placeholder left unsubstituted, which is the
   failure that otherwise ships silently and grants nothing. `validDomains` gets
   the bare host, since a scheme there is rejected.

   Upload via Teams → Apps → Manage your apps → Upload an app (or the org app
   catalog). **Re-uploads are rejected unless the manifest `version` is
   bumped** — edit it in `manifest.template.json`. Always add/update the app
   from **Teams on the web**: the desktop client serves a cached package and
   grants nothing.

   Replace `color.png` (192×192) and `outline.png` (32×32) with your own icons
   if you are publishing under your own brand.

### Private and shared channels

Private and shared channels do **not** inherit the team's apps: the app must be
installed in the host team *and* added to each such channel explicitly
(channel → `+` → Apps). Two manifest requirements, and both are needed:

1. `"supportsChannelFeatures": "tier1"` (requires manifest v1.25) — declares the
   app is ready for these channels.
2. A `configurableTabs` entry. That picker is the **tab** gallery: it only lists
   apps that can add a tab, so a bot-only manifest can never be installed into a
   private channel — searching for it returns "No Results Found". `agent/teams_tab.py`
   serves a one-click config page purely to satisfy this; adding the tab is what
   installs the app (bot included) into the channel.

Symptom when only the bot is declared: `@loupfeed` still autocompletes in the
private channel's compose box (Teams offers bots installed in the parent team),
the message posts, and nothing is ever delivered to the bot endpoint.

Caveats that still apply there: each private/shared channel has **its own
SharePoint site** (use `GET /teams/{teamId}/channels/{channelId}/filesFolder`,
never the team drive), channel membership is a subset of the team, and Graph
**message change-notification subscriptions are blocked** for RSC apps in these
channels (403) — read messages on demand instead.

## Microsoft 365 read access (Graph)

The pm agent reads Microsoft 365 context through two read-only tools
(`graph_api`, `graph_meeting_transcript`) that authenticate app-only
(client credentials) as the same Entra app registration. Access comes from
two layers:

### 1. Resource-specific consent (RSC) — where the app is installed

The manifest's `authorization.permissions.resourceSpecific` block requests
application-level read permissions that are granted **per team / chat /
meeting when the app is installed there** — no tenant-wide message access,
no admin consent in Entra:

| Permission | Grants |
|---|---|
| `ChannelMessage.Read.Group` | Read channel messages of the installed team |
| `ChannelSettings.Read.Group`, `TeamSettings.Read.Group` | Channel/team names & settings |
| `TeamMember.Read.Group` | Team roster |
| `ChannelMember.Read.Group` | Channel roster + membership events (needed in private/shared channels, where channel membership ≠ team membership) |
| `ChannelMeeting.ReadBasic.Group`, `ChannelMeetingTranscript.Read.Group`, `ChannelMeetingParticipant.Read.Group` | Channel-meeting details, transcripts, participants |
| `ChatMessage.Read.Chat`, `ChatMember.Read.Chat`, `ChatSettings.Read.Chat` | Messages/members/properties of the installed chat |
| `OnlineMeeting.ReadBasic.Chat`, `OnlineMeetingTranscript.Read.Chat`, `OnlineMeetingParticipant.Read.Chat` | Meeting details, **transcript**, and participants of the meeting behind the installed chat |

Notes:

- RSC grants happen at install time; a team owner/member (or chat member /
  meeting organizer) consents in the install dialog. Tenant default
  (`ManagedByMicrosoft`) allows this; admins can restrict it via
  `Set-MgBetaTeamRscConfiguration` / `Set-MgBetaChatRscConfiguration`.
- `OnlineMeetingTranscript.Read.Chat` covers **scheduled private meetings**
  only; channel meetings are covered by `ChannelMeetingTranscript.Read.Group`.
- There is no `File.Read.Group`-style RSC permission in the current supported
  set — files shared in teams/chats are read via the tenant permissions below.
- Reference: [Resource-specific consent](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/resource-specific-consent),
  [Grant RSC permissions](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/grant-resource-specific-consent).

### 2. Tenant application permissions — directory-wide reads RSC can't cover

Add these as **Application** permissions (Microsoft Graph) on the same app
registration in the Entra admin center (App registrations → your app → API
permissions → Add a permission → Microsoft Graph → Application permissions),
then select **Grant admin consent**:

| Permission | Why |
|---|---|
| `User.Read.All` | Resolve people, profiles, org chart (`/users`, `/users/{id}/manager`) |
| `Sites.Read.All` | Read files in team SharePoint libraries and other sites |
| `Files.Read.All` *(optional)* | Files shared in 1:1/group chats live in the **sharer's OneDrive**; without this the agent can only read team-library files |
| `OnlineMeetingTranscript.Read.All` *(optional fallback)* | Only if RSC transcript reads turn out insufficient in your tenant (see below) |

Direct admin-consent URL (replace placeholders):
`https://login.microsoftonline.com/<tenant-id>/adminconsent?client_id=<app-id>`

### 3. Meeting transcript access policy (conditional)

Microsoft's docs state that app-only calls to the online-meeting APIs require
a **Teams application access policy** granting the app access to the
organizer's meetings; in practice the RSC path
(`OnlineMeetingTranscript.Read.Chat`) is granted at install and may work
without one. **Verify first** (see below); if transcript reads return
403/`Forbidden`, apply the policy:

```powershell
Install-Module MicrosoftTeams
Connect-MicrosoftTeams
New-CsApplicationAccessPolicy -Identity loupfeed-graph -AppIds "<app-id>" `
  -Description "loupfeed pm agent meeting reads"
# per organizer:
Grant-CsApplicationAccessPolicy -PolicyName loupfeed-graph -Identity "<organizer-upn>"
# or tenant-wide:
Grant-CsApplicationAccessPolicy -PolicyName loupfeed-graph -Global
```

(Policy changes can take up to ~30 minutes to apply.) If transcript reads
return 403 with `GraphAccessToTranscriptsDisabled`, an admin has disabled
Graph transcript access tenant-wide (Teams admin center) and must re-enable
it.

## Verification

1. Re-upload the app package (version bumped) to the org catalog and
   **update the app** in a test team and in a meeting (Calendar → meeting →
   Apps → add the app to the meeting, or add it to the meeting group chat).
   The install dialog should list the RSC read permissions.
2. Hold a short call with **transcription on**; say something actionable.
3. After the call, @mention the bot in the meeting chat: *"@loupfeed triage
   what we discussed into SPI ideas."*
4. The reply should quote actual transcript lines, reference chat
   messages/files it read, and propose/create ideas citing that material. If
   it reports it couldn't read the transcript, check step 3 above (access
   policy) and that the app is installed in that meeting/chat.

## Behavior

- Thread ⇄ session: every Teams thread (channel thread, 1:1 chat, meeting
  chat) maps to one LangGraph `pm` thread; messages are speaker-labeled.
- The adapter passes the conversation's Graph ids (chat id, team group id,
  meeting id) into the run so the pm agent can read the surrounding
  Microsoft 365 context where the app is installed.
- If the Atlassian connector isn't connected yet, the bot replies with the
  org-wide OAuth connect link instead of running the agent.
