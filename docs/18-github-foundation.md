# 18 — GitHub foundation

Phase 15 of the roadmap. It adds a read-only GitHub REST integration: an async client with a
token from the environment only, connection health, a synced repository table and admin
endpoints to browse repositories, branches, commits, trees and file contents. Nothing in this
phase writes to GitHub. Issues, PRs, Actions and webhooks come in later phases on top of this
client.

## What it does

- **Client** (`app/integrations/github/client.py`): async `httpx` client for the GitHub REST
  API (`X-GitHub-Api-Version: 2022-11-28`).
  - **Token:** `GITHUB_TOKEN` from the environment only (a fine-grained PAT). It is never read
    from the DB or a request, never stored and never logged. The DB keeps only a fingerprint:
    the first 16 hex characters of the token's sha256.
  - **Redaction** (`app/integrations/github/redact.py`): every exception message, log field
    and error body that leaves the client goes through `redact()`. That replaces the
    configured token and anything shaped like a GitHub credential (`ghp_`, `gho_`, `ghu_`,
    `ghs_`, `ghr_`, `github_pat_`, `Authorization: Bearer|token …`).
  - **Pagination:** follows the `Link: rel="next"` header, capped at `GITHUB_MAX_PAGES` pages of
    100. A next link to another host is never followed.
  - **Retry:** at most 3 attempts. Exponential backoff (`GITHUB_RETRY_BACKOFF_SECONDS` × 2^n)
    on 5xx and network errors. A 4xx is never retried, except a 429 or a secondary rate
    limit (a 403 with `retry-after`, `x-ratelimit-remaining: 0` or "secondary rate limit").
    Those wait for `retry-after` (or the reset time, or 60 s) when that is no more than
    `GITHUB_MAX_RETRY_WAIT_SECONDS`; otherwise they return a 429 with `Retry-After`.
  - **Rate-limit floor:** every response updates the client's rate-limit snapshot
    (`x-ratelimit-limit/remaining/used/reset/resource`). While `remaining` is below
    `GITHUB_RATE_LIMIT_FLOOR` and the reset is still ahead, requests are refused locally with
    429 `github_rate_limit_floor`. GitHub is not called.
  - **Timeouts and redirects:** connect and read timeouts are set. Redirects are followed by
    hand, at most 3, and only to the API host itself, so the token is never sent elsewhere.
    A redirect to another host is a 502 `github_redirect`.
  - **Cache:** successful GETs are cached in memory for `GITHUB_CACHE_TTL_SECONDS`, up to 256
    entries. Syncs and `GET /user` bypass the cache.
- **Allowlist:** `GITHUB_ALLOWED_REPOS`, when set, is the exact list of visible repos (narrowed
  by `GITHUB_ALLOWED_OWNERS` if that is set too). Otherwise `GITHUB_ALLOWED_OWNERS` decides.
  With neither set, only the token user's own repos (owner == login) are visible. A repo
  outside the allowlist is a 404, exactly like a missing one, and GitHub is not called for
  it. Matching ignores case.
- **Tables** (schema `github`, migration 0012, additive):
  - `github.connections`: one row per token fingerprint, holding token_kind, api_url, login,
    github_user_id, scopes, ok, detail, the rate-limit snapshot and last_checked_at.
  - `github.repositories`: synced metadata, keyed by GitHub's repo id. Columns: full_name,
    owner, name, description, private, default_branch, archived, pushed_at, html_url,
    synced_at and removed_at. Rows are never deleted. A repo a complete sync no longer sees
    gets `removed_at`, which is cleared if it comes back.
  - `github.sync_runs`: one row per sync, with its trigger (`scheduler` or `api`), status,
    counts, error and actor.
- **Scheduler:** the task `github_sync` runs `GET /user` (connection health), then the
  repository sync. The job `github-sync` (`GITHUB_SYNC_CRON`) is added to the default jobs
  only when `GITHUB_TOKEN` is set.
- **Agent registry:** the task registers and heartbeats the agent `github` (kind
  `integration`), with login, ok, detail, rate limit and repo counts in `details`.
  `details.stale_after_s = GITHUB_AGENT_STALE_SECONDS`. A failure sets state `error` and
  writes an agent log. Pausing the agent (`POST /api/agents/github/pause`) makes the task
  skip.

## Endpoints

All of them are admin only, including the GETs (the code is the owner's), and POSTs need CSRF.
Without `GITHUB_TOKEN`, `GET /connection` answers `configured: false` and every other endpoint
answers 503 `{"code": "github_not_configured"}`.

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/v1/github/connection` | Health: configured?, fingerprint, token kind, login, scopes, ok/detail, rate limit, last check, allowlist, last sync. Never the token. |
| POST | `/api/v1/github/connection/check` | Calls `GET /user` now and stores the result (audited `github_connection_checked`). |
| GET | `/api/v1/github/repositories?limit=&offset=&q=&refresh=` | `Page[Repository]` from `github.repositories`, allowlisted and not removed. `refresh=true` syncs first (audited `github_repositories_synced`). |
| GET | `/api/v1/github/repositories/{owner}/{repo}` | Live repo detail. A renamed repo is re-checked against the allowlist. |
| GET | `…/{owner}/{repo}/branches` | All branches (paginated, capped): name, sha, protected. |
| GET | `…/{owner}/{repo}/commits?sha=&path=&per_page=&page=` | One page of commits. `per_page` ≤ 100 (else 422), `page` ≤ 100. |
| GET | `…/{owner}/{repo}/tree?ref=&path=&recursive=` | Git tree of `ref` (default: the default branch) at `path`. Cut at `GITHUB_MAX_TREE_ENTRIES` with `truncated: true`. |
| GET | `…/{owner}/{repo}/contents/{path}?ref=` | File: UTF-8 text up to `GITHUB_MAX_FILE_BYTES`. Binary (NUL bytes or not UTF-8) and larger files return metadata only (`content_kind` `binary` / `too_large`). Dirs list their entries. Symlinks and submodules return their target. |

Errors look like `{"detail": {"code": …, "detail": …}}`:

| Status | Code | When |
| --- | --- | --- |
| 400 | `invalid_path`, `invalid_ref`, `invalid_sha`, `invalid_owner`, `invalid_repo` | Rejected before GitHub is called. This covers `..`/`.` segments (also `%2e%2e`, which is checked after one more URL-decode), absolute paths, `//`, backslashes and control characters. |
| 404 | `not_found` | Not on GitHub, or outside the allowlist. |
| 409 / 422 | `github_conflict` | GitHub's own 409/422 (e.g. an empty repository). |
| 429 | `github_rate_limited`, `github_rate_limit_floor` | Comes with `Retry-After`. |
| 502 | `github_unavailable`, `github_auth_failed`, `github_forbidden`, `github_redirect`, `github_error` | 5xx or network failure after the retries; bad token; no permission; refused redirect. |
| 503 | `github_not_configured` | No `GITHUB_TOKEN`. |

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `GITHUB_TOKEN` | unset | Fine-grained PAT (see [manual-setup.md](manual-setup.md#15--github-foundation)). Unset turns the integration off. |
| `GITHUB_API_URL` | `https://api.github.com` | REST base URL (GHES: `https://host/api/v3`). |
| `GITHUB_ALLOWED_OWNERS` | `[]` | JSON list of owners. Empty means the token user's own repos only. |
| `GITHUB_ALLOWED_REPOS` | `[]` | Optional JSON list of `owner/name`. When set, it is the exact list. |
| `GITHUB_CACHE_TTL_SECONDS` | `60` | In-memory GET cache. `0` turns it off. |
| `GITHUB_CONNECT_TIMEOUT` / `GITHUB_READ_TIMEOUT` | `5` / `20` | Seconds. |
| `GITHUB_MAX_ATTEMPTS` | `3` | 1 to 3. |
| `GITHUB_RETRY_BACKOFF_SECONDS` | `0.5` | Backoff base. |
| `GITHUB_MAX_RETRY_WAIT_SECONDS` | `20` | The longest 429 / secondary-limit wait inside a request. |
| `GITHUB_RATE_LIMIT_FLOOR` | `50` | Refuse locally below this many remaining requests. |
| `GITHUB_MAX_PAGES` | `10` | Pagination cap (pages of 100). |
| `GITHUB_MAX_FILE_BYTES` | `524288` | Largest file returned as text (at most 1 MiB, GitHub's own contents limit). |
| `GITHUB_MAX_TREE_ENTRIES` | `5000` | Tree entries returned. |
| `GITHUB_SYNC_CRON` | `*/20 * * * *` | `github-sync` job schedule (`SCHEDULER_TIMEZONE`). |
| `GITHUB_AGENT_STALE_SECONDS` | `3600` | The `github` agent goes stale without a sync heartbeat for this long. |

## Deploy and validate

1. Create the PAT and put it in the server's `.env`, as described in
   [manual-setup.md](manual-setup.md#15--github-foundation). Set
   `GITHUB_ALLOWED_OWNERS` / `GITHUB_ALLOWED_REPOS`.
2. `docker compose run --rm migrate` applies 0012 (the `github` schema and three new tables).
3. `docker compose up -d api scheduler`. The scheduler seeds `github-sync` only when the token
   is set. A scheduler that was already running seeds it on restart.
4. As an admin (with the session cookie and CSRF header):
   ```bash
   curl -s -b jar https://api.spasta.online/api/v1/github/connection | jq
   curl -s -b jar -H "x-csrf-token: $CSRF" -X POST https://api.spasta.online/api/v1/github/connection/check | jq '.ok,.login,.rate_limit'
   curl -s -b jar 'https://api.spasta.online/api/v1/github/repositories?refresh=true' | jq '.total'
   curl -s -b jar https://api.spasta.online/api/v1/github/repositories/OWNER/REPO/contents/README.md | jq .content_kind
   ```
5. `GET /api/agents` shows `github` (kind `integration`) after the first `github-sync` run.
   `GET /api/scheduler/jobs` lists `github-sync`.
6. Check that the token never appears:
   `docker compose logs api scheduler | grep -c github_pat_` must print 0.

## Rollback

- Turn it off: remove `GITHUB_TOKEN` and restart api + scheduler. The endpoints then answer 503
  and the job is no longer seeded. Pause an existing `github-sync` job with
  `POST /api/scheduler/jobs/github-sync/pause`, or pause the `github` agent.
- Schema: `docker compose run --rm migrate alembic downgrade 0011` drops only the three
  `github.*` tables and then the schema if it is empty (no CASCADE). Synced metadata can be
  re-created from GitHub with a sync.
- Revoke the PAT on GitHub (Settings → Developer settings → Fine-grained tokens) if it may have
  leaked.

## Tests

- `tests/test_github_unit.py` (no DB, respx):
  - pagination over 3 pages, the max-pages cap and an off-host next link
  - retry on 502 then success, 5xx giving up after 3 attempts with backoff, network retry, no
    retry on 4xx
  - a 429 with `retry-after`, the secondary-rate-limit 403, a too-long `retry-after`
  - the rate-limit floor refusal (GitHub not called) and the floor lifting after reset
  - an off-host redirect refused, a same-host redirect followed, the cache
  - redaction of a token in an error body (exception and captured structlog events),
    redaction helper shapes, the fingerprint
  - path/ref validation, allowlist rules, config parsing, `default_jobs` only with a token
- `tests/test_github_db.py` (`-m db`, respx for GitHub):
  - not configured: `/connection` says so, everything else 503 `github_not_configured`
  - non-admin 403 on every endpoint, anonymous 401, CSRF required on the check
  - the connection check stored and audited, a failed check recorded with the token redacted
  - repositories: sync over 2 pages, the default allowlist filtering, paging and `q`,
    removal marking, sync runs
  - allowlist 404 with no GitHub call, explicit `GITHUB_ALLOWED_REPOS`
  - repo detail, branches (paginated), commits (params forwarded, `per_page` cap 422, bad sha
    400), tree (default ref, `ref:path`, recursive, traversal 400)
  - contents: text, binary, too large (both by size and by GitHub's `encoding: none`), dir, 404
    and traversal rejected (`%2e%2e`, double-encoded, `%2e`, backslash, `//`)
  - a token in a 500 body: retried, 502, never in the response or the client's logs
  - 429 with `Retry-After`, the floor refusal through the API and its snapshot in `/connection`
  - the scheduler task `github_sync`: agent `github` heartbeat (idle, then error)

## Open risks

- **Fine-grained PATs report no scopes.** `scopes` stays empty for them (GitHub sends
  `x-oauth-scopes` for classic tokens only). Missing permissions show up as 403
  `github_forbidden` on the call that needs them.
- **Paths in `git/trees/{ref}:{path}`.** The tree endpoint passes a branch name with slashes
  unescaped. GitHub resolves this in practice, but an ambiguous ref and path could resolve
  differently from what was meant; pass a commit sha as `ref` to be exact.
- **The cache is per process.** The API and the scheduler each have their own. A file changed
  on GitHub can be served stale for up to `GITHUB_CACHE_TTL_SECONDS`.
- **No ETag requests yet.** Repository syncs re-list everything each run. At 1000 repos
  (the 10 × 100 cap) that is 10 requests per sync. A capped listing never marks repos as
  removed.
- **The login is learned once.** In the default allowlist mode, the token user's login comes
  from the last connection check, or from one `/user` call cached per process. Changing the
  token to another user needs a restart (a new fingerprint creates a new connection row
  anyway).
