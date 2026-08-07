# loupfeed for Atlassian (entry app)

The Atlassian entry surface for a loupfeed agents deployment, shipped with the
platform in `deploy/atlassian/` beside the Teams package in `deploy/teams/`.
Installed on a Jira and Confluence site, it does two jobs:

- **Forwards** the events the agents care about (issue comments, assignments,
  page comments and edits) to your deployment's `/webhooks/atlassian`.
- **Acts as the app**, so replies, page and folder writes, and attachments are
  authored by *loupfeed* rather than by a person's account, and a deployment
  needs no Atlassian API token of its own. It exposes three web triggers for
  this: `reply`, an allowlisted `proxy`, and `attach`.

Nothing in this repository is specific to one organisation. Your deployment URL
and shared secret are yours; the app id is issued to you by `forge register`.

## Publish your own copy

You need the [Forge CLI](https://developer.atlassian.com/platform/forge/set-up-forge/)
and a site you administer.

```bash
cd deploy/atlassian
npm install
forge login                       # your Atlassian account + an API token
forge register                    # issues YOUR app id
LOUPFEED_DEPLOYMENT_URL=https://agents.example.dev bin/render-manifest
forge deploy
forge install --product jira      # and again with --product confluence
```

`bin/render-manifest` generates `manifest.yml` from `manifest.template.yml`. It
is generated, and git-ignored, because one value in it cannot be a runtime
setting: **Forge requires every egress destination to be declared in the
manifest**, so each publisher renders and deploys their own copy pointing at
their own deployment. Re-running it preserves the app id already registered.

Then tell the app where to send events and how to authenticate:

```bash
forge variables set DEPLOYMENT_URL https://agents.example.dev
forge variables set --encrypt SHARED_SECRET "$(openssl rand -hex 32)"
forge variables set APP_ACCOUNT_ID <the app's Atlassian account id>
```

The same secret goes into your deployment as `ATLASSIAN_APP_SHARED_SECRET`, and
the three web trigger URLs (`forge webtrigger`) become
`ATLASSIAN_APP_REPLY_URL`, `ATLASSIAN_APP_PROXY_URL` and
`ATLASSIAN_APP_ATTACH_URL`. The app reads its URL and secret from app storage
first and falls back to these variables, so an installation can be re-pointed
without a redeploy.

`APP_ACCOUNT_ID` is what the deployment's mention gate compares against. Find it
with `/rest/api/3/user/search?query=<your app name>`: the entry whose
`accountType` is `app`.

## Rendering replies

The deployment sends `bodyAdf` (Jira) or `bodyStorage` (Confluence) alongside the
plain `text`, and this app posts whichever it is given. Rendering lives in the
deployment because that is where it is tested; `text` remains the fallback for
callers that send none. Sending only `text` is why replies once arrived as a
single paragraph with `**bold**` and backticks showing literally.

## The proxy allowlist

`proxy` performs Atlassian reads and writes with `asApp()`, but only for paths on
an explicit allowlist, and returns `403 not allowed` for anything else. That is a
deliberate boundary: the deployment cannot use the app as a general-purpose
credential. Widening it is a code change here, reviewable on its own.

## Notes

- Scopes are the minimum the four functions need. `forge lint` is the authority
  on scope names; several plausible-sounding ones do not exist (attachments are
  `write:confluence-file`, not `write:attachment:confluence`).
- `storage` is not a named export of `@forge/api` v8. It hangs off the default
  export (`api.storage`), and importing it the other way makes every trigger
  throw before it can forward.
- Confluence events carry no body, only an id, so the deployment hydrates the
  comment or page before its mention gate can see anything.

See [`SPIKE-FINDINGS.md`](./SPIKE-FINDINGS.md) for what the original spike
established, including the pricing and identity constraints behind these
choices.
