# Atlassian entry-app spike: findings

Ran 2026-08-05 against the real dinolabgmbh site (Jira Free team-managed +
Confluence Free), app installed in the `development` environment. Answers the
open questions in loupfeed `docs/12-generic-connect.md` P2 with evidence, not
documentation reading.

## Verdict

**The Forge entry-app design works on the Free plan.** Both flagship
triggers are viable: assignment/mention on a Jira issue and comments on a
Confluence page. Proceed to the P2 MVP.

## Answers

### 1. Can a Forge app user be assigned issues on team-managed Free? YES

The app appears in user search as `accountType: app`, active, alongside the
human agent account:

```
loupfeed-entry-spike | type=app       | 712020:3b1d36e0-…
Loupfeed             | type=atlassian | 712020:410bca9c-…
```

`PUT /rest/api/3/issue/SPB-3/assignee` with the app's accountId returned 204
and the issue's assignee reads back as `loupfeed-entry-spike (type: app)`.
This is the "assign a bug to loupfeed" flow, confirmed. **The app user
consumes no license seat**, so the invited human agent account can be
retired once the app owns the work (frees a Free-plan seat).

### 2. Do events reach the app on Free plans? YES, with a naming trap

Delivered, verified in logs:

| Event | Fired | Payload highlights |
| --- | --- | --- |
| `avi:jira:assigned:issue` | yes | `issue`, `changelog` (assignee), `atlassianId` (actor), `associatedUsers` |
| `avi:jira:updated:issue` | yes | same, changelog names the changed field |
| `avi:jira:commented:issue` | yes | full `comment` ADF body, `issue`, actor |
| `avi:confluence:created:comment` | yes (2/2) | `content` (comment id + title "Re: <page>"), actor |

**GOTCHA (silent failure, same class as the Teams manifest traps):**
`avi:jira:created:comment` passes `forge lint` and deploys clean but NEVER
fires. The working event id is **`avi:jira:commented:issue`**. Zero of two
comments delivered under the wrong name; the very next comment arrived under
the right one. Any event id must be proven by a live event, never by the
linter.

`avi:confluence:mentioned:comment` also lints clean but did not fire for a
comment containing an `<ac:link><ri:user/></ac:link>` mention of the app.
Not needed: `created:comment` fires and carries the body, so detect mentions
in the handler (same approach the Teams adapter uses).

### 3. Is mention detection possible from the payload? YES

Jira comment events carry the complete ADF body, so a `mention` node with
the app's accountId is directly detectable. Confluence comment events carry
the comment id; fetch the body via REST when the event arrives.

## Also learned (setup mechanics)

- Forge CLI is fully non-interactive EXCEPT: Developer Space creation, the
  Developer Terms acceptance (legal + billing consent, must be a human
  decision), and space selection. A pty driver handles the rest.
- App registration is per-account: an app created by a non-admin account
  cannot be installed on the site. Register with an org admin, or make the
  agent account an admin. (`forge register` re-registers an existing app.)
- Confluence triggers need `read:confluence-content.summary` in addition to
  the granular v2 scopes; the linter does catch this one.
- Installing across products is two commands (`--product jira`, then
  `--product confluence`), not one.
- `@forge/cli` must come from the public npm registry when a private
  registry is configured (401 otherwise).

## Working manifest (spike)

```yaml
modules:
  trigger:
    - key: loupfeed-jira-events
      function: main
      events:
        - avi:jira:commented:issue      # NOT avi:jira:created:comment
        - avi:jira:assigned:issue
        - avi:jira:updated:issue
    - key: loupfeed-confluence-events
      function: main
      events:
        - avi:confluence:created:comment
        - avi:confluence:updated:page
  function:
    - key: main
      handler: index.run
permissions:
  scopes:
    - read:jira-work
    - read:comment:confluence
    - read:page:confluence
    - read:confluence-content.summary
```

## END-TO-END PROVEN 2026-08-05

Atlassian event > Forge app > sandbox deployment, on real events:

```
avi:jira:assigned:issue   SPB-4  addressed=True   (assigned to the app)
avi:jira:updated:issue    SPB-4  addressed=True
avi:jira:commented:issue  SPB-4  addressed=True   text='E2E:  fix this please.'
avi:jira:commented:issue  SPB-4  addressed=False  text='E2E: unrelated chatter, nobody tagged.'
```

The last line is the important one: ordinary project chatter arrives and is
correctly NOT addressed to us, so the agents stay out of it. Unauthenticated
posts are rejected (401) and authenticated ones accepted (202).

Wiring that made it work:

- Deployment: `POST /webhooks/atlassian` (`agent/atlassian_adapter.py`,
  loupfeed-agents PR #28), shared secret in `X-Loupfeed-Secret`, compared in
  constant time; normalises ADF to text + mention ids; gates on mention or
  assignment. Secret rendered from the store as
  `ATLASSIAN_APP_SHARED_SECRET` (renderer needs the key added explicitly,
  then `install` the renderer on the box).
- Forge app: `external.fetch.backend` egress to the deployment host (the
  linter rewrites the deprecated string form to `- address:`), plus
  environment variables `DEPLOYMENT_URL`, `SHARED_SECRET` (`--encrypt`),
  `APP_ACCOUNT_ID`. Variables only take effect after a `forge deploy`, and
  adding egress needs `forge install --upgrade` per product.
- Adding egress bumps the major version: `forge deploy --approve
  MAJOR_VERSION_RULE` in non-interactive use.

## DISPATCH WORKING END TO END 2026-08-05

A mention on a Confluence page produced a real agent answer as a comment:

```
webhook  avi:confluence:created:comment 288194578 addressed=True  text='… what is this page for?'
dispatch confluence:288194578 -> pm thread=b97a6128-…
reply    "An end-to-end probe page used to test that dispatches to the right
          agent work — it has no real content beyond confirming it exists…"
```

Routing contract shipped (loupfeed-agents PRs #29-#31):

- **Assign the app an issue** > coding agent, opens a PR carrying the issue key.
- **Mention the app** > pm agent, answers or drafts.
- One issue/page maps to one agent thread, so follow-ups continue it.
- Replies go through `redact_internals`; runs happen in a background task so
  the webhook 202s immediately.

Two more live-only findings, both fixed:

1. **Confluence comment events carry no body** (Jira's do). The mention gate
   saw `text=''` and never fired. Fix: fetch the comment body via REST,
   extract `ri:account-id` mentions from storage format, and reply on the
   PARENT page rather than the comment.
2. **`langgraph dev`'s blockbuster 500s on blocking I/O in async routes.**
   Both the hydration fetch and the reply POST now go through
   `asyncio.to_thread`. Same trap the connector token store hit; a
   source-inspecting test pins it.

Note the loop is observable: the agent's own reply arrives back as a new
event and is correctly `addressed=False`, so it does not answer itself.

## CODING PATH PROVEN 2026-08-05 (with three real defects found)

Assigning SPB-5 (a genuine bug: six German translation keys missing from
`de/candidate.ts`) to the app produced deliveru PR #2494, authored by the
GitHub App identity `app/loupfeed`, titled with the issue key, with a
summary commented back on the Jira issue. It even found MORE than the
ticket described (an entire missing `skills.source` subsection).

Defects the run exposed, all fixed:

1. **One assignment dispatched twice** (PR #33). Jira emits both
   `avi:jira:assigned:issue` and `avi:jira:updated:issue` for a single
   assignment, each carrying the assignee changelog. Only the specific
   assigned event may trigger the coding path.
2. **The branch was not cut from a clean base** (PR #34). The PR contained
   an already-merged commit (`perf: slim profile loader`, in main as
   e67ec4e62) and its merge-base was 35 commits behind main: the per-thread
   sandbox reuses its clone, so leftover work from an earlier task became
   the branch point. The prompt now requires `git fetch` + branch from
   `origin/main` and a `git log origin/main..HEAD` self-check. If it
   recurs, reset the clone in `ensure_sandbox_for_thread` instead.
3. **It claimed "all checks passed" when CI had reported none** (PR #34).
   The prompt now forbids claiming a check result it has not read.

Lesson for every entry app: the agent's own git hygiene and honesty about
verification are part of the contract, not incidental.

## Next for the P2 MVP

1. Add `external.fetch.backend` egress to the manifest plus an admin config
   page holding the deployment URL + shared secret (routing per install, no
   relay needed).
2. Forward matched events to the deployment; reply as a comment via
   `asApp()`. Reuse the Teams adapter's mention-gate and thread-mapping
   shape so one Jira issue or page maps to one agent thread.
3. Confirm `asApp()` REST covers the folder + attachment operations that
   currently need the API token, then retire `ATLASSIAN_API_TOKEN`.
4. Still open: whether page-level (not comment) mentions can be detected at
   all, and whether an app user can be set as a Confluence page author.
