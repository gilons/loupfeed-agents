# The bug agent (triage graph)

A bug report reaches the agent and it answers three questions: is it real, where
does it live, and which change most likely caused it. It does not fix anything.
Its output is a report on the ticket that a developer can act on, plus an
internal record so the second report of the same defect is a lookup.

Read-only by construction: no sandbox, no product access, no write tools. Fixing
stays with the coding agent, and handing a bug over stays a human's decision.

## Why the commit pinning is exact

A loupfeed release string is `<surface>@<commit>` (`@loupfeed/build`'s build
identity), and the build uploads an id-to-source manifest keyed by that release.
So an instrumented report already names the tree the reporter was running:

1. `release` gives the sha of the build.
2. The manifest resolves the failing element or stack frame to `path:line` **in
   that tree**.
3. `git_blame_line` at that sha names the commit responsible for that line.
4. `git_compare` between the last good release and the first bad one is the set
   the culprit must be in. Intersected with the blamed file, that is usually a
   handful of commits.

The trap the tools are shaped around: those line numbers are only valid at that
commit. Blaming them at the head of the default branch names a real commit,
plausibly, and wrongly. `git_blame_line` therefore has no default `ref`.

A release of `dev`, or one carrying a `-dirty` suffix, was not built from a clean
commit. `surfaces.parse_release` marks those unpinnable and the agent is told to
say so rather than blame the nearest commit.

## Anchored and unanchored reports

Reports arrive two ways, and they deserve different confidence:

- **Anchored** — a loupfeed feedback thread or crash group. Release, resolved
  source, breadcrumbs, replay. Triage is a lookup.
- **Prose** — a support ticket or a chat message. No anchor. The agent first
  looks for the anchored twin with `loupfeed_find_reports` (same screen, same
  wording, same window); only if nothing matches does it fall back to code
  search, and the report says it did.

## The surface registry

Everything product-specific lives in `/etc/loupfeed/surfaces.json`
(`LOUPFEED_SURFACES_FILE`), so the platform names no product. One entry per
built surface:

```json
[
  {
    "key": "acme-webapp",
    "repo": "acme/acme",
    "path_root": "apps/webapp",
    "jira_projects": ["BUG"],
    "loupfeed": {
      "api": "https://loupfeed.acme.dev",
      "org": "acme",
      "project": "acme",
      "token_env": "LOUPFEED_DASHBOARD_TOKEN"
    }
  }
]
```

| Field | Meaning |
|---|---|
| `key` | The surface half of the release string, so a report resolves to this entry. |
| `repo` | `owner/name`. Where the code is. |
| `path_root` | Prefix that turns a manifest path into a repository path. Manifest paths are relative to the *build* root, so a monorepo needs this (`app/x.tsx` -> `apps/webapp/app/x.tsx`). Omit for a single-app repo. |
| `jira_projects` | Projects whose issues belong to this surface. **This is also what routes an issue to triage**: a mention on an issue in a mapped project is a bug report. |
| `loupfeed` | Instance coordinates. `token_env` names the environment variable holding the dashboard token, so the mapping stays config and the secret stays in the environment. Omit the block entirely for a surface with no loupfeed instance; triage then runs search-based only. |

Deployment-specific investigation notes (repository layout, ownership, known
quirks) go in `/etc/loupfeed/triage-prompt.md` (`TRIAGE_PROMPT_EXTRA_FILE`),
appended to the prompt the same way `pm-prompt.md` is for the pm graph.

## Routing

`atlassian_adapter.route` sends an issue to triage when its Jira project maps to
a surface. Routing on the project rather than on the comment's wording keeps the
entry point predictable: no keyword sniffing, and the deployment decides which
projects hold defects by writing the mapping. Assignment still wins, so
assigning an issue to the app is still "go fix it" and reaches the coding agent.

Triage is therefore tag-driven today: somebody mentions the app on a bug. Firing
it automatically on every escalated support ticket is a config change away, and
worth turning on only once its reports are trusted, since it comments publicly.

## Tools

| Tool | Purpose |
|---|---|
| `loupfeed_find_reports` | Find the anchored twin of a prose ticket. |
| `loupfeed_report` | One report in full: release, resolved source, frames, first-seen release. |
| `git_blame_line` | The commit owning one line at one exact commit. |
| `git_commits_touching` | File-level history, date-boundable. |
| `git_commit_diff` | A suspect's actual patch. |
| `git_compare` | The introduced-in window between two releases. |
| `find_prior_triage` | Has this defect been triaged already? |
| `record_triage` | Log the finding so the next repeat matches it. |

Plus the platform's read-only Atlassian connector tools, `read_repo_file`,
`search_repo_code`, `github_api`, `web_search` and `fetch_url`.

## The internal log

`record_triage` writes one record per triaged report onto the thread's metadata
(the same place reviewer findings live), fingerprinted on surface, symptom and
path. The fingerprint strips ids, numbers and shas, because two reports of one
defect never agree on those. `find_prior_triage` matches on it, so a repeat
report costs a lookup instead of an investigation. In an organisation with many
reporters that matching is most of triage's value.

## Honesty rules

The prompt enforces these, and they are the difference between a report that
saves a developer an hour and one that costs them two:

- Never name a suspect commit whose diff was not read.
- Never blame lines at a ref other than the release they came from.
- Say when a release cannot be pinned, and fall back to file-level history.
- Say when the finding is search-based rather than anchored.
- An honest "unclear, here are the two candidates" beats a fabricated pin.

## Not in this version

Reproduction. The agent cannot drive the product, so it neither confirms a repro
nor records a demo video; the loupfeed demo engine
(`@loupfeed/demo-core`, `demo-mcp`) is the natural home for that, and a
replay-derived scenario that ends in the reported symptom would make a repro
*proven* rather than asserted. Deliberately out of scope until static triage is
trusted.
