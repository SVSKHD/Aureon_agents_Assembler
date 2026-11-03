# 22 — Claude review

Phase 19 of the roadmap. An admin can ask for an automated review of an open pull request.
The review runs through the **local Claude CLI** on the server. It is advisory only: it
never approves a PR on GitHub and never satisfies or bypasses the approval gate.

This was the first non-Ollama AI path in the project. Brief rule 4 was amended on 2026-11-03
(see "Prompt 0" in [decisions.md](decisions.md)): external inference through OpenRouter is
now allowed too, opt-in and behind its own classification filter
([25-openrouter.md](25-openrouter.md), which reuses this filter's secret detection). Local
Ollama stays the default. The review is off by default and stays behind three checks:
`CLAUDE_REVIEW_ENABLED`, an availability check, and a data-classification filter that
decides what may leave the server.

## What it does

```
POST …/pulls/{n}/claude-review ──► PR detail (head, base, title, body) ──► agents.reviews (queued)
   │                                  dedupe: same repo + PR + head_sha → the existing review
   └─► background task (one at a time, bounded queue)
         1. diff = GET /compare/{base}...{head} (pinned shas), at most CLAUDE_REVIEW_MAX_DIFF_BYTES
              over the cap ─► REVIEW_UNAVAILABLE (too_large)          nothing sent
         2. classify(title, body, diff)
              BLOCKED ─► verdict BLOCKED ("classified: …")             nothing sent, CLI never started
              REDACT  ─► emails / IPs / internal hosts masked
         3. availability: enabled + on PATH + `claude --version` ok (cached 5 min)
              no ─► REVIEW_UNAVAILABLE (disabled | cli_missing | version_failed | version_timeout)
         4. claude -p --output-format json … < prompt (stdin), empty temp cwd, minimal env
              timeout | exit ≠ 0 | not JSON | schema error ─► REVIEW_UNAVAILABLE (reason)
              ok ─► APPROVED | CHANGES_REQUESTED | BLOCKED + summary + findings
POST /reviews/{id}/publish ─► GitHub PR review, event COMMENT (never APPROVE), tracked + audited
```

### Availability (`app/integrations/claude_cli.py`)

- `available()` is true only when `CLAUDE_REVIEW_ENABLED=true`, `CLAUDE_CLI_PATH` resolves to an
  executable on PATH (or is an absolute path to one), and `claude --version` exits 0 within
  10 s. The result is cached for 5 minutes, good or bad. While the setting is off, nothing is
  ever started.
- **Every run follows the same rules:**
  - argv only (`create_subprocess_exec`), never a shell
  - the prompt goes on **stdin**, never in argv (so it is not visible in `ps`)
  - a **minimal environment**: `HOME` and `PATH`, plus only the names listed in
    `CLAUDE_CLI_ENV_PASSTHROUGH`. Names that look like repo secrets (`GITHUB_*`, `DATABASE_*`,
    `REDIS*`, `SECRET*`, `PG*`, `AWS_*`, `DHAN_*`, `MT5_*`, `TRADING_*`, …) are refused even
    if listed, and the refusal is logged.
  - a fresh, **empty temporary working directory**, removed afterwards, so the CLI has no
    files around it to read
  - its own process group, killed as a whole on timeout; stdout is capped at 1 MiB
- **Default flags** (`CLAUDE_CLI_ARGS`):
  `-p --output-format json --no-session-persistence --strict-mcp-config --tools ""`. That
  means print mode with a JSON result, no saved session, no MCP servers and no tools, so the
  CLI can only read the prompt and answer. The flags are configurable as a JSON list, in case
  a CLI version renames one.

### The data-classification filter (`app/services/review_filter.py`)

It runs on the unified diff and on the PR title and body before anything is sent.

| Outcome | When | What is sent |
| --- | --- | --- |
| **BLOCKED** | A touched path is sensitive (see below), or the text contains something secret-shaped: a private key block, a GitHub token (`ghp_`, `gho_`, `ghs_`, `github_pat_`, …), an AWS access key or `aws_secret_access_key=`, a Slack / Google / `sk-` API key, credentials in a URL (`scheme://user:pw@`), a `password=` literal (a quoted literal, a config line, a URL query or `"password": "…"`), a secret-named `.env` line with a real value (`*_TOKEN=…`, `*_SECRET=…`, `DATABASE_URL=…`; empty values and `${VAR}` / `<placeholder>` are fine), or a high-entropy literal (≥ 3.5 bits/char assigned to a key/secret/token name, or ≥ 4.3 bits/char for any 32+ char quoted string; lock-file `sha512-` hashes excepted). | Nothing. The verdict is BLOCKED, with the reason `classified: …`. |
| **REDACT** | Emails, IPv4 addresses (RFC1918 and public alike), and internal hostnames: any name under `CLAUDE_REVIEW_INTERNAL_DOMAINS` (default `spasta.online`). | The text with `[email]`, `[ip]` and `[internal-host]` in place. Masking happens inside a line, so diff line numbers stay the same. |
| **ALLOW** | Nothing found. | The text as is. |

- **Sensitive paths** live in `app/core/sensitive_paths.py`, one shared list that Phase 18's
  self-repair hard blocks are meant to import too (D-19-2):
  - `secrets`: `.env`, `.env.*` (but not `.env.example`, `.sample`, `.template` or `.dist`, whose
    content is still scanned), `*.pem`, `*.key`, `*.p12`, `id_rsa*`, `.npmrc`, `.netrc`,
    `*secret*`, `*credential*`, `*.tfstate`, `*.tfvars`, and `secrets/`, `.ssh/`, `.aws/`
  - `infra`: `Dockerfile*`, `docker-compose*.yml`, `compose*.yml`, `*.tf`, `*.service`,
    `nginx*.conf`, `cloudflared*`, and `deploy/`, `infra/`, `.github/`, `systemd/`, `nginx/`,
    `k8s/`, `helm/`, `terraform/`, `ansible/`
  - `trading_risk`: any path token among trading, trade(s), risk(s), broker(s), dhan, mt5,
    order(s), execution(s), live, phase_g, paper and position(s)
  - `auth`: any path token among auth, security, login, password(s), oauth, csrf, jwt, rbac,
    session(s), permissions, procsec, prod_checks and approval(s)

  Paths are lower-cased and split on `/ . _ -`, so `app/agents/trading/dhan.py` and
  `app/auth/service.py` both match. Both sides of a rename count.
- **Reasons never quote the value**, only the rule and where it was, e.g.
  `content:github_token:app/x.py (diff line 12)` or `path:secrets:.env`. Up to 20 reasons are
  kept.
- The outcome, its reasons, the redaction counts and the touched files are stored in
  `classification`.

### The run (`app/services/claude_review.py`)

- **Pinned diff.** The diff is fetched as `compare/{base_sha}...{head_sha}`, so a push during
  the review cannot change what is reviewed. At most `CLAUDE_REVIEW_MAX_DIFF_BYTES` + 1 bytes
  are read. A larger diff, or one GitHub refuses as too large (406), gives
  REVIEW_UNAVAILABLE `too_large`, and nothing is sent.
- **The prompt is fixed.** It holds the masked title, body and diff between markers that
  carry a random nonce, tells the CLI the content is untrusted data and must not be obeyed,
  and asks for exactly this JSON:
  `{"verdict": "APPROVED"|"CHANGES_REQUESTED"|"BLOCKED", "summary": str, "findings": [{"file", "line", "severity": "info|minor|major|critical", "message"}]}`
- **Parsing:**
  - the CLI's JSON envelope is read (`result`; `is_error` or a non-success `subtype` is
    `cli_error`). A bare review object, or a stream-json list, is accepted too.
  - the review JSON is taken from `result` (as is, from a code fence, or from the outermost
    `{…}`) and validated by Pydantic with `extra="forbid"`: an unknown verdict or severity,
    an extra key or a missing field is `invalid_output`.
  - summaries and messages are token-redacted before they are stored.
- **REVIEW_UNAVAILABLE reasons:** `disabled`, `cli_missing`, `version_failed`,
  `version_timeout`, `too_large`, `github_error`, `timeout`, `cli_error`, `invalid_output`,
  `internal_error` and `cancelled` (the server shut down mid-run).
- **Concurrency:** one review runs at a time (a semaphore). At most `CLAUDE_REVIEW_MAX_QUEUE`
  wait; beyond that the request answers 429 `review_queue_full` (`Retry-After: 60`).
- **Dedupe:** a partial unique index on `(repo, pr_number, head_sha)`, for rows that are not
  `forced` and did not end REVIEW_UNAVAILABLE. Asking again for the same head returns the
  existing review (`deduplicated: true`). `{"force": true}` always makes a new one. An
  unavailable review can simply be requested again.

### Publishing

`POST /api/v1/reviews/{id}/publish` posts one GitHub PR review through the Phase 16 client
and `tracked_write`, which means a `github.tracked_operations` row (`pr.review.claude_publish`)
and the audit event `github_pr_review_claude_publish`, plus `claude_review_published`.

- The event is **always `COMMENT`**, never `APPROVE` or `REQUEST_CHANGES`, whatever the
  verdict. The body starts with **"Automated review (advisory; human approval still
  required)"**, states the advisory verdict and that it is not an approval, then gives the
  summary.
- `commit_id` is the reviewed head sha.
- A finding whose `file` and `line` are on the new side of the diff (an added or context
  line) becomes an inline comment (`side: RIGHT`, up to 50). The rest are listed in the body.
- Only a finished review with a verdict can be published (a REVIEW_UNAVAILABLE one answers
  409 `review_not_publishable`), and only once (409 `already_published`). The row is claimed
  under a lock before the GitHub call and released again if GitHub refuses, so a failed
  publish can be retried.
- The repo must still be on the allowlist (404 otherwise).

### It never replaces human approval

- Nothing in the review code reads or writes `audit.approval_requests`, and the approval
  gate does not look at reviews.
- A merge stays `202` plus an approval request, and only the approving admin's password
  re-entry executes it. `test_review_never_satisfies_the_merge_approval` publishes an
  APPROVED review first, then checks that no password gives 422, a wrong password gives 403,
  nothing is merged, and only the correct re-auth executes.
- The PR-opened auto-review (`CLAUDE_REVIEW_AUTO`) is **not** built in this phase (D-19-9).

## Endpoints

All are admin only, GETs included. POSTs need CSRF.

| Method | Path | What |
| --- | --- | --- |
| POST | `/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/claude-review` | Body `{"force"?: bool}`. 202 `{id, status, head_sha, deduplicated}`. The run is a background task. 404 for an off-allowlist repo (GitHub is not called), 409 `pr_not_open`, 429 `review_queue_full`, 503 without `GITHUB_TOKEN`. |
| GET | `/api/v1/reviews?repo=&pr=&limit=&offset=` | `Page[Review]`, newest first. `repo` is `owner/name`, case-insensitive. |
| GET | `/api/v1/reviews/{id}` | One review: status, verdict, reason, summary, findings, classification, cli_version, duration_ms, published, `advisory: true`. |
| POST | `/api/v1/reviews/{id}/publish` | Posts the GitHub review (event COMMENT). 200 `{review, github_review_id, event, inline_comments, body_findings}`. 404, 409 `review_not_publishable` or `already_published`, GitHub errors as in Phase 15. |

## Table (migration 0015, additive)

`agents.reviews` has these columns:

- id uuid, repo (lower-case), pr_number, head_sha, base_sha
- status: queued, running or done
- verdict: APPROVED, CHANGES_REQUESTED, BLOCKED or REVIEW_UNAVAILABLE (NULL until done)
- reason, summary, findings jsonb, classification jsonb
- cli_version, duration_ms
- forced, published, published_review_id, published_at
- requested_by → core.users (SET NULL), created_at, finished_at

It has an index on (repo, pr_number, created_at), and the partial unique index
`uq_agents_reviews_repo_pr_head` described under dedupe.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLAUDE_REVIEW_ENABLED` | `false` | Master switch. Off = every review is REVIEW_UNAVAILABLE `disabled`, and nothing is started. |
| `CLAUDE_CLI_PATH` | `claude` | The binary: a name looked up on PATH, or an absolute path. |
| `CLAUDE_CLI_ARGS` | `["-p","--output-format","json","--no-session-persistence","--strict-mcp-config","--tools",""]` | JSON argv after the binary. The prompt always goes on stdin. |
| `CLAUDE_CLI_ENV_PASSTHROUGH` | empty | Comma list of env var names the CLI needs for its own auth (e.g. `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`). Repo-secret names are refused. |
| `CLAUDE_REVIEW_TIMEOUT_SECONDS` | `300` | Hard timeout for one run (10–3600). |
| `CLAUDE_REVIEW_MAX_DIFF_BYTES` | `204800` | A larger diff is never sent (`too_large`). |
| `CLAUDE_REVIEW_MAX_QUEUE` | `5` | Reviews that may wait behind the running one. |
| `CLAUDE_REVIEW_INTERNAL_DOMAINS` | `spasta.online` | Comma list of domains whose host names are masked. |

## Deploy and validate

1. Install and authenticate the CLI, as in
   [manual-setup.md](manual-setup.md#19--claude-review). Check it with
   `claude --version` as the service user.
2. `docker compose run --rm migrate` applies 0015, which adds one table.
3. Set `CLAUDE_REVIEW_ENABLED=true` (plus `CLAUDE_CLI_ENV_PASSTHROUGH` if the CLI
   authenticates through an env var), then run `docker compose up -d api`.
4. As an admin (cookie jar plus CSRF header):
   ```bash
   B=https://api.spasta.online/api/v1
   ID=$(curl -s -b jar -H "x-csrf-token: $CSRF" -H 'content-type: application/json' -d '{}' \
     "$B/github/repositories/OWNER/REPO/pulls/12/claude-review" | jq -r .id)
   sleep 60; curl -s -b jar "$B/reviews/$ID" | jq '{status,verdict,reason,classification}'
   # publish it as a COMMENT review
   curl -s -b jar -H "x-csrf-token: $CSRF" -X POST "$B/reviews/$ID/publish" | jq '.event,.github_review_id'
   ```
   Use a harmless test PR the first time. A PR touching `.env` should come back `BLOCKED`
   at once, without the CLI ever running.
5. Audit: `SELECT event_type, count(*) FROM audit.audit_events WHERE event_type LIKE
   'claude_review_%' GROUP BY 1;`

## Rollback

- **Stop it at once:** set `CLAUDE_REVIEW_ENABLED=false` and restart the API. Running reviews
  are cancelled and marked `cancelled`, and new ones are REVIEW_UNAVAILABLE `disabled`.
- **Code:** redeploy the previous image. No other endpoint changed.
- **Schema:** `docker compose run --rm migrate alembic downgrade 0014` (or the previous head)
  drops only `agents.reviews`. Export it first if you need it:
  `\copy (SELECT * FROM agents.reviews) TO 'reviews.csv' CSV HEADER`. Reviews already
  published on GitHub stay there; delete them on GitHub if needed.

## Tests

- `tests/test_claude_review_unit.py` (no DB; a fake `claude` script written by the test):
  - the setting defaults and env parsing
  - **availability:** unavailable when disabled (nothing started), when missing (a path or a
    name not on PATH), or when the version check fails; available and cached for 5 minutes,
    re-checked after; resolved on PATH
  - **the run:** the exact argv, the prompt on stdin and not in argv, the env limited to
    HOME, PATH and the allowed passthrough (GITHUB_TOKEN and DATABASE_URL refused even when
    listed), an empty cwd that is removed afterwards; a timeout kills the process
  - the env-name rules
  - **sensitive paths:** 21 cases
  - **the filter:** BLOCKED for `.env`, trading paths, a private key, GitHub tokens, an AWS
    key, the password forms, `.env` lines, high-entropy literals and URL credentials, with
    reasons that never contain the value; a secret in the PR body; no false positives for
    `password: str`, `== password`, `password=PASSWORD`, empty or `${}` env values, a hex
    sha or a lock-file hash; `.env.example` is scanned but not path-blocked; redaction of
    emails, IPs and hosts with line counts kept; renames into a blocked path
  - **parsing:** a valid envelope, a fenced result, a bare object, stream-json; garbage,
    prose, an unknown verdict, an extra key, a bad severity and `is_error` are all refused
    with the right reason
  - the prompt nonce; commentable lines and splitting findings; the runner's bound and
    serial execution
- `tests/test_claude_review_db.py` (`-m db`, respx plus the fake CLI):
  - an APPROVED review stored (verdict, findings, classification, cli_version, duration)
    and published: **event COMMENT** with the advisory header, one inline comment, the rest
    in the body, `commit_id` = head; publishing twice is 409; a tracked operation and four
    audit events
  - a failed publish releases the claim, and a retry works
  - CHANGES_REQUESTED
  - **dedupe on head_sha;** force makes a new row; a new head makes a new review; list
    filters
  - an unavailable review does not block a new request
  - **BLOCKED** for a `.env` diff, a private key and a trading path, with **zero CLI
    invocations** (no `--version` either), asserted on the fake CLI's log and the
    invocation counter
  - **redaction:** the CLI's stdin has no email, IP or internal host
  - **REVIEW_UNAVAILABLE** for garbage output (and it cannot be published), a non-zero
    exit, a timeout, too large (nothing started), disabled and a missing CLI
  - a closed PR gives 409
  - **non-admin 403** on all four routes; CSRF required; 404s; an off-allowlist repo with
    no GitHub call
  - a full queue gives 429
  - **the approval gate:** a merge with a published APPROVED review still needs the admin's
    re-auth (no password 422, a wrong one 403, nothing merged until the correct one)
- `tests/test_route_auth.py` pins the four new admin routes.

## Open risks

- **The filter is pattern-based.** It catches the common secret shapes and blocks broadly
  by path, but it cannot prove a diff is clean. A secret in an unusual format (a short
  password in prose, a custom token format) could pass as ALLOW. False positives are the
  safe failure (BLOCKED, and a human reviews). Tighten `sensitive_paths.py` and the rules
  rather than loosen them.
- **IPv6 addresses and hostnames outside `CLAUDE_REVIEW_INTERNAL_DOMAINS` are not masked.**
  Add internal domains to the setting.
- **The diff goes to an external service.** Once classified ALLOW or REDACT, the masked diff
  of a non-sensitive path leaves the server through the CLI. Keep the reviewer disabled
  for repos whose code itself is confidential.
- **The CLI's own behaviour.** Tools, MCP and session persistence are switched off by flags.
  A CLI version that renames or ignores a flag could change that: re-check `claude --help`
  after upgrades and adjust `CLAUDE_CLI_ARGS`. The empty working directory and the minimal
  environment limit what it could touch anyway.
- **Prompt injection in the PR.** The PR's text is wrapped in nonce markers and labelled as
  untrusted, but a model can still be steered by it. The worst case is a wrong advisory
  verdict, and it can never merge anything: the event is COMMENT and the approval gate is
  separate.
- **Stale running rows.** If the API process is killed hard (not a clean shutdown) mid-run,
  the row stays `running` with no verdict and dedupes that head. Re-request with
  `{"force": true}`.
- **The review runs in the API process.** One CLI process at a time, with a timeout. That fits
  the resource rule, but it shares the API container's CPU and memory caps.
