# Skill companion resources

A skill can ship more than its `SKILL.md`. Any files sitting beside it are
discovered at registration and served on demand:

```
my-skill/
  SKILL.md
  references/formatting.md
  scripts/run.py
  agents/coder.md
  assets/diagram.png
```

Registration scans the skill's directory and classifies each file by its
subfolder (`references/`, `scripts/`, `agents/`, `assets/`), falling back to the
extension for skills that keep every file at the root. The result is stored as
the skill's `resource_manifest`.

Read a companion file with the `resource` query parameter:

```
GET /api/skills/{path}/content?resource=references/formatting.md
```

The path must appear in the stored manifest. Anything else is refused before
any upstream fetch, which is what keeps `../` out of the request.

## Supported hosting platforms

Discovery asks the platform for the skill directory's file tree. Four platforms
are recognised:

| Platform | SKILL.md URL form | Tree API |
|---|---|---|
| GitHub | `raw.githubusercontent.com/<owner>/<repo>/<ref>/<dir>/SKILL.md` (short ref) or `.../refs/heads/<ref>/...` | `api.github.com/repos/<owner>/<repo>/git/trees/<ref>` |
| GitHub Enterprise | blob or raw URL on the configured host | `GITHUB_API_BASE_URL/repos/...` |
| GitLab | `/-/raw/` or `/-/blob/` URL | `/api/v4/projects/<id>/repository/tree` |
| Gitee | `gitee.com/<owner>/<repo>/raw/<ref>/<dir>/SKILL.md` | `gitee.com/api/v5/repos/<owner>/<repo>/git/trees/<ref>` |

A host outside this list logs a warning that names the host. That is
deliberately distinct from "the skill has no companion files", which logs
nothing: both end with an empty manifest, and at debug level they used to be
indistinguishable.

## Credentials

Each credential goes only to its own origin.

- **GitHub** uses `GITHUB_PAT` / the GitHub App, and only when the skill's tree
  URL is on `github.com`, a `*.github.com` subdomain, or the configured GHES
  host. It is never sent to gitee.com.
- **Gitee** uses `GITEE_ACCESS_TOKEN` (a personal access token, sent as the
  `access_token` query parameter). Leave it empty for public repositories; set
  it when the skill lives in a private Gitee repository, or when anonymous
  rate limiting starts returning 429. Gitee limits anonymous reads far more
  tightly than GitHub, so a registry with many Gitee-hosted skills will hit
  that ceiling without a token.

Neither token is written to a log. Every URL the discovery path logs goes
through `redact_url()`, which drops the query string.

## Refreshing an existing skill

If files were added to the skill's repository after registration, refresh
without re-registering:

```
POST /api/skills/{path}/refresh-resources
```

That re-runs discovery against the skill's stored URL and replaces the manifest.
It does not recompute `content_integrity`, so a refresh alone will not trip the
drift check.

## Rate limits

Discovery makes one tree request per skill. Many skills in the same repository
share one tree, but they are fetched independently, so a bulk refresh of a large
registry can exhaust GitHub's anonymous quota (60 requests/hour) and start
returning 429. Set `GITHUB_PAT` for any registry with more than a handful of
GitHub-hosted skills.
