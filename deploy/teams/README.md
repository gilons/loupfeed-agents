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
3. **Teams app package**: fill `manifest.template.json` (replace
   `${TEAMS_APP_ID}`, adjust `validDomains` to your public domain), add
   `color.png` (192×192) and `outline.png` (32×32), zip the three files, and
   upload via Teams → Apps → Manage your apps → Upload an app (or the org app
   catalog). **Re-uploads are rejected unless the manifest `version` is
   bumped.**

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
