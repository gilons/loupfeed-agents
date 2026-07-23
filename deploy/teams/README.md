# Teams app setup

The Teams adapter is served by the platform at `POST /webhooks/teams`
(publicly: `https://<your-domain>/webhooks/teams`).

## One-time Azure/Teams registration

1. **Entra app registration** (portal.azure.com → App registrations → New):
   single tenant, no redirect URI. Note the **Application (client) ID** and
   create a **client secret**. These become `TEAMS_APP_ID` /
   `TEAMS_APP_PASSWORD` (+ `TEAMS_APP_TENANT_ID` = your tenant id) in the
   platform secret.
2. **Azure Bot resource** (Create resource → Azure Bot): use the same app id
   (Single Tenant), set the **messaging endpoint** to
   `https://<your-domain>/webhooks/teams`, and enable the **Microsoft Teams
   channel**.
3. **Teams app package**: fill `manifest.template.json` (replace
   `${TEAMS_APP_ID}`, adjust `validDomains` to your public domain), add
   `color.png` (192×192) and `outline.png` (32×32), zip the three files, and
   upload via Teams → Apps → Manage your apps → Upload an app (or the org app
   catalog).

## Behavior

- Thread ⇄ session: every Teams thread (channel thread, 1:1 chat, meeting
  chat) maps to one LangGraph `pm` thread; messages are speaker-labeled.
- If the Atlassian connector isn't connected yet, the bot replies with the
  org-wide OAuth connect link instead of running the agent.
