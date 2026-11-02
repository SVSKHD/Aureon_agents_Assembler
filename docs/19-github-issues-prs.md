# 19 — GitHub issues and pull requests

Phase 16 of the roadmap. It builds on the Phase 15 client ([18-github-foundation.md](18-github-foundation.md))
and adds:

- full issue operations
- links between GitHub issues and Spasta tasks
- pull requests: reads, diffs, comments, reviews, create and update
- a minimal **approval gate**. Merge, force-push, branch-protection changes, branch delete and
  repo-config changes never run directly. They need an approved request, and the approving
  admin must re-enter their password.
- `github.tracked_operations`, which records every write sent to GitHub, or held for
  approval

## What it does

- **Same client, same rules.** Everything goes through `GitHubClient` (token from the
  environment, redaction, retry and rate limits) and is checked against the allowlist first:
  a repo outside it is a 404 and GitHub is never called. The client gained these:
  - `request(..., accept=..., raw=True, max_bytes=N)` returns raw bytes (the unified diff) and
    streams at most N+1 bytes.
  - A **POST is never retried** after a 5xx or a network error, because GitHub may already
    have acted on it (that would mean a second issue or a second comment). PUT, PATCH and
    DELETE are retried like GETs.
  - Any write clears the in-memory GET cache.
  - A 405 from GitHub ("not mergeable") maps to 409 `github_conflict`.
- **Tracked writes:** every direct write (an issue, a comment, a label, a lock, a PR, a review)
  goes through `github_ops.tracked_write`, which does three things:
  - writes a `github.tracked_operations` row: `running`, then `succeeded` or `failed`. The
    payload is token-redacted and capped at 4,000 characters per string, and the row keeps a
    small response summary or the redacted error.
  - writes an audit event `github_<op>`, e.g. `github_issue_create` or
    `github_pr_review_create`, whatever the outcome
  - returns GitHub's error unchanged (with the Phase 15 status mapping)
- **Bodies are redacted on the way out.** Issue, comment, review and PR bodies, commit
  messages, file patches and diffs all pass through the client's `redact()`.
- **Issue ↔ task links:** these live in `github.issue_links`. `task_ref` is an opaque string
  the frontend supplies, because Spasta tasks live in the frontend workspace document
  (`public.astra_documents`, namespace `aureon-notes`), not in a backend table (D-16-1).
  - Creating a link checks that the issue exists on GitHub and stores its `node_id`.
  - Linking the same issue to the same task again is a no-op that answers 200 (a new link
    answers 201).
  - Links are stored with a lower-case repo name.
- **Diffs:** `GET …/pulls/{n}/diff` asks for `Accept: application/vnd.github.diff` and reads at
  most `GITHUB_DIFF_MAX_BYTES`. A larger diff comes back cut, with `truncated: true`. When
  GitHub itself refuses the diff as too large (406), the answer is an empty diff with
  `truncated` and `too_large_upstream` both true. In `…/files`, each file's `patch` is cut at
  100,000 characters, with `patch_truncated`.

### The approval gate (`app/services/approvals.py`)

```
gated endpoint ──► GETs only (compute the artifact) ──► audit.approval_requests (pending)
                                                        github.tracked_operations (pending_approval)
                                                        202 {approval_id, artifact_sha256, expires_at}
POST /api/v1/approvals/{id}/approve {password}
   ├─ wrong password ─► 403 reauth_failed (event + audit; Nth failure auto-denies)
   ├─ expired ─► 409 approval_expired          (status expired)
   ├─ not pending ─► 409 approval_already_used / approval_not_pending
   ├─ requester == approver and not allowed ─► 403 self_approval_not_allowed
   ├─ GitHub not configured ─► 503 (request NOT consumed)
   └─ status approved (single use) ─► re-check allowlist ─► recompute artifact
          ├─ sha256 differs ─► 409 approval_invalidated (status invalidated, nothing written)
          └─ execute ─► 200 status executed | failed (result recorded)
```

- **Interface:** this is what Phase 20 keeps.
  - `ApprovalGate.request(kind, target, payload, artifact, *, requested_by, client, db)`
    returns an `ApprovalRequest`.
  - `approve(id, admin, reauth_password, *, deps, client)` executes the request.
  - `deny(id, admin, *, client, reason=None)` denies it.
  - It also has `get`, `list_requests` and `expire_due`.
- **Executor registry:** `REGISTRY` maps each kind to an `ExecutorSpec`, which holds
  `execute`, `current_artifact`, `on_status` and `requires`. The GitHub kinds are registered
  by `app/services/github_ops.py` when it is imported:
  - `github.pr.merge`
  - `github.ref.force_update`
  - `github.branch_protection.update`
  - `github.branch_protection.delete`
  - `github.branch.delete`
  - `github.repo.update_settings`
  - `github.actions.set_permissions`

  `on_status` mirrors every status change into the tracked operation, in the same
  transaction.
- **Artifact:** a canonical JSON of what the approver is shown, hashed with sha256. The
  artifact is stored in `payload.artifact`. The same function computes it at request time and
  again right before execution.

  | Kind | Artifact |
  | --- | --- |
  | merge | `head_sha` + `diff_sha256` (sha256 of the unified diff) + `base_ref` + `state` |
  | force-push | the branch's current sha + the target sha |
  | branch delete | the branch's current sha |
  | protection update / delete | the current protection JSON (`null` when unprotected) |
  | repo settings | the current values of the settings keys |
  | Actions | the current `actions/permissions` |

- **Merge specifics:** the request must carry `expected_head_sha`. If the PR's head differs
  at request time, or the PR is not open, the answer is 409 and nothing is filed. The
  executor sends `sha` = the approved head, so GitHub refuses too if the head moved in the
  last instant.
- **Re-auth:** the admin's password goes in the body and is verified with the existing Argon2
  hasher, in a worker thread. It is never stored or logged. A session cookie plus CSRF alone
  is not enough. After `APPROVALS_MAX_REAUTH_FAILURES` wrong passwords, the request is denied.
- **Expiry:** expiry is lazy. A due request is marked `expired` when it is read, approved,
  denied or listed (`GET /api/v1/approvals` expires all due requests first).
- **Single use:** the approve call moves the row out of `pending` under a row lock before
  anything runs. A second approve answers 409, even if the first approve failed or crashed
  mid-way.
- **Self-approval:** the requester may approve their own request only when
  `APPROVALS_ALLOW_SELF_APPROVAL=true` and they are the only active admin (D-16-3).

## Endpoints

All are admin only, GETs included. Non-GET endpoints need CSRF. Without `GITHUB_TOKEN`, the
repo endpoints answer 503 `github_not_configured`. The error shapes are the ones in
[18-github-foundation.md](18-github-foundation.md#endpoints).

`R` = `/api/v1/github/repositories/{owner}/{repo}`

### Issues and links

| Method | Path | What |
| --- | --- | --- |
| GET | `R/issues?state=&labels=&assignee=&since=&sort=&direction=&per_page=&page=&include_pulls=` | One page of issues (`{items, page, per_page, has_next}`). PRs are left out unless `include_pulls=true`. |
| POST | `R/issues` | Create: title, body, labels, assignees, milestone (201). |
| GET | `R/issues/{n}` | Detail with comments (up to 300, `comments_truncated`) and task links. |
| PATCH | `R/issues/{n}` | Update: title, body, state, state_reason, labels, assignees, milestone (`null` clears it). |
| POST | `R/issues/{n}/close`, `R/issues/{n}/reopen` | Close (`state_reason` completed or not_planned) or reopen. |
| POST | `R/issues/{n}/comments` | Comment (201). |
| PATCH / DELETE | `R/issues/comments/{comment_id}` | Edit or delete a comment. |
| POST | `R/issues/{n}/labels` | Add labels; returns the issue's labels. |
| DELETE | `R/issues/{n}/labels/{name}` | Remove a label. |
| PUT / DELETE | `R/issues/{n}/lock` | Lock (`lock_reason`) or unlock (204). |
| POST | `R/issues/{n}/links` | Link to a task: `{task_ref, task_kind?}` (201 new, 200 existing). |
| DELETE | `R/issues/{n}/links?task_ref=` | Unlink (204, or 404). |
| GET | `/api/v1/github/links?task_ref=&repo=` | Links for a task or a repo (one filter required). |
| GET | `/api/v1/github/operations?repo=&op=&status=&limit=&offset=` | `Page[TrackedOperation]`, newest first. |

### Pull requests

| Method | Path | What |
| --- | --- | --- |
| GET | `R/pulls?state=&head=&base=&sort=&direction=&per_page=&page=` | One page of PRs. |
| POST | `R/pulls` | Create: head (`branch` or `owner:branch`), base, title, body, draft (201). |
| GET / PATCH | `R/pulls/{n}` | Detail (mergeable, counts) / update: title, body, base, state. |
| GET | `R/pulls/{n}/files` | Changed files with patches (each capped). |
| GET | `R/pulls/{n}/diff` | Unified diff, up to `GITHUB_DIFF_MAX_BYTES` (`truncated`, `too_large_upstream`). |
| GET | `R/pulls/{n}/commits` | Commits. |
| GET | `R/pulls/{n}/merge-status` | state, merged, draft, mergeable, mergeable_state, rebaseable, head/base. |
| GET / POST | `R/pulls/{n}/comments` | Review comments. Create needs commit_id, path and line, or `in_reply_to`. |
| GET / POST | `R/pulls/{n}/issue-comments` | Conversation comments. |
| GET / POST | `R/pulls/{n}/reviews` | Reviews. Create takes an `event` of `COMMENT`, `APPROVE` or `REQUEST_CHANGES` (the last needs a body), with optional line comments. |

### Gated operations (202, nothing written until approved)

| Method | Path | Body | Kind |
| --- | --- | --- | --- |
| POST | `R/pulls/{n}/merge` | `merge_method` (merge, squash or rebase), `expected_head_sha`, `commit_title?`, `commit_message?` | `github.pr.merge` |
| POST | `R/force-push` | `branch`, `sha`: a forced ref update. The sha must exist in the repo, or 422 `unknown_sha`. | `github.ref.force_update` |
| PUT | `R/branch-protection/{branch}` | GitHub's protection body, passed through | `github.branch_protection.update` |
| DELETE | `R/branch-protection/{branch}` | none | `github.branch_protection.delete` |
| DELETE | `R/branches/{branch}` | none | `github.branch.delete` |
| PATCH | `R/settings` | visibility, default_branch, allow_merge_commit, allow_squash_merge, allow_rebase_merge, allow_auto_merge, allow_update_branch, delete_branch_on_merge, has_issues, has_wiki, has_projects, archived | `github.repo.update_settings` |
| PUT | `R/actions-permissions` | `enabled`, `allowed_actions?` | `github.actions.set_permissions` |

The answer is `{approval_id, status: "pending", kind, target, artifact_sha256, expires_at,
operation_id, artifact}`.

### Approvals

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/v1/approvals?status=&kind=&limit=&offset=` | `Page[Approval]`, newest first. Expires due requests first. |
| GET | `/api/v1/approvals/{id}` | The request with its `events`. |
| POST | `/api/v1/approvals/{id}/approve` | `{"password": "..."}`. Answers 200 with `executed` or `failed`. Also 403 `reauth_failed` or `self_approval_not_allowed`; 409 `approval_expired`, `approval_already_used`, `approval_not_pending` or `approval_invalidated`; 503. |
| POST | `/api/v1/approvals/{id}/deny` | `{"reason"?}`. 409 unless pending. |

## Tables (migration 0013, additive)

- **`audit.approval_requests`:** id uuid, kind, target, payload jsonb, artifact_sha256,
  status (pending, approved, denied, expired, invalidated, executed or failed),
  requested_by, decided_by, decided_at, expires_at, executed_at, result jsonb, created_at.
- **`audit.approval_events`:** id, request_id, event, actor, details, at. The events are
  requested, approved, denied, expired, invalidated, executed, failed, reauth_failed and
  self_approval_refused. Each event is also written as an `audit.audit_events` row
  `approval_<event>`.
- **`github.issue_links`:** id, repo_full_name, issue_number, issue_node_id, task_ref (text),
  task_kind (text, default `spasta_task`), created_by, created_at. Unique on
  (repo_full_name, issue_number, task_ref).
- **`github.tracked_operations`:** id, op, repo, target, request_payload (redacted), status
  (pending_approval, running, succeeded, failed, denied, expired or invalidated),
  approval_request_id, github_response_summary, error, actor, created_at, finished_at.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `GITHUB_DIFF_MAX_BYTES` | `2097152` | Largest unified diff returned (1 KiB to 50 MiB). |
| `APPROVALS_TTL_MINUTES` | `30` | A pending request expires after this many minutes. |
| `APPROVALS_ALLOW_SELF_APPROVAL` | `true` | The requester may approve their own request while they are the only active admin. Set it to `false` to always require another admin. |
| `APPROVALS_MAX_REAUTH_FAILURES` | `5` | Wrong passwords before a request is denied automatically. |

All the Phase 15 `GITHUB_*` settings still apply.

## Deploy and validate

1. Give the PAT the extra permissions listed in
   [manual-setup.md](manual-setup.md#16--github-issues-and-prs): Administration read and
   write, for branch protection, repo settings and Actions permissions.
2. `docker compose run --rm migrate` applies 0013, which adds four tables.
3. `docker compose up -d api`.
4. As an admin (cookie jar plus CSRF header):
   ```bash
   B=https://api.spasta.online/api/v1/github/repositories/OWNER/REPO
   curl -s -b jar "$B/issues?state=open" | jq '.items[].number'
   curl -s -b jar "$B/pulls/1/merge-status" | jq
   curl -s -b jar "$B/pulls/1/diff" | jq '.truncated,.bytes_returned'
   # a gated merge: 202 plus an approval id. Nothing is merged yet.
   HEAD=$(curl -s -b jar "$B/pulls/1" | jq -r .head.sha)
   curl -s -b jar -H "x-csrf-token: $CSRF" -H 'content-type: application/json' \
     -d "{\"merge_method\":\"squash\",\"expected_head_sha\":\"$HEAD\"}" "$B/pulls/1/merge" | jq
   curl -s -b jar https://api.spasta.online/api/v1/approvals?status=pending | jq '.items[].id'
   # approve with your password. Use a test PR the first time.
   curl -s -b jar -H "x-csrf-token: $CSRF" -H 'content-type: application/json' \
     -d '{"password":"…"}' https://api.spasta.online/api/v1/approvals/$ID/approve | jq .status
   curl -s -b jar https://api.spasta.online/api/v1/github/operations | jq '.items[0]'
   ```
5. Check the audit log: `SELECT event_type, count(*) FROM audit.audit_events WHERE event_type
   LIKE 'approval_%' OR event_type LIKE 'github_%' GROUP BY 1;`

## Rollback

- **Code:** redeploy the previous image. The Phase 15 endpoints are unchanged.
- **Schema:** `docker compose run --rm migrate alembic downgrade 0012` drops only the four new
  tables. The `github` and `audit` schemas stay.
  - The audit trail in `audit.audit_events` is not touched.
  - Export `audit.approval_requests` and `audit.approval_events` first if you need them:
    `\copy (SELECT * FROM audit.approval_requests) TO 'approvals.csv' CSV HEADER`.
- **Stop every gated operation at once:** remove `GITHUB_TOKEN`. Approve then answers 503
  without consuming the request.

## Tests

- `tests/test_github_ops_unit.py` (no DB, respx):
  - client write semantics: a POST is not retried after a 5xx or a network error; a PUT is
    retried; a write clears the cache
  - the raw capped body and the Accept header
  - 405 → 409
  - canonical artifact hashing
  - registry registration and replacement
  - head and branch validation
  - payload redaction and the summary
  - the setting defaults
- `tests/test_github_ops_db.py` (`-m db`, respx):
  - **issues:** list params forwarded, PRs filtered, `has_next`; detail with redacted
    comments; create, update (milestone `null`), empty update 422, close and reopen;
    comment create, edit and delete; label add and remove (URL-encoded name); lock and
    unlock; a failed write tracked as `failed` and redacted; audit counts; the
    `/operations` filters and paging; the tracked payload redacted
  - **links:** create 201, duplicate 200, missing issue 404, bad `task_ref` 422; links in
    the issue detail; list by `task_ref`; filter required; non-admin 403; delete 204 and
    404; audited
  - **PRs:** list params and a bad base 400; detail; files with a capped patch; commits
    redacted; review and issue comments; reviews; merge-status; diff; zero writes for reads
  - **diff cap:** cut at 1 KiB with `truncated`; GitHub's 406 → `too_large_upstream`
  - **PR writes:** create (draft), a bad head 400, update, review comment create and reply,
    conversation comment, review validation (REQUEST_CHANGES needs a body, an unknown
    event is 422), a review with line comments, non-admin 403
  - **every gated endpoint (7):** 202 with an approval id, **zero GitHub write calls**
    (asserted on respx calls), non-admin 403, a `pending_approval` tracked operation, a
    `requested` event
  - **merge:** head mismatch 409 at request time; the artifact sha matches the canonical
    payload artifact; approve → executed with exactly one PUT carrying the approved sha;
    a second approve 409 `approval_already_used`; the events requested, approved, executed
  - **expiry:** an expired approval → 409 `approval_expired`, no write, the operation
    `expired`; listing expires due requests
  - **drift:** the head sha changed → 409 `approval_invalidated`, no write, the operation
    `invalidated`, no retry; the diff changed → invalidated too
  - **re-auth:** a wrong password → 403, a `reauth_failed` event and audit row, the request
    still pending; no password → 422; the Nth failure denies it; passwords never in the
    audit log
  - **deny:** deny → approve 409; deny twice 409; non-admin 403 on every approvals route;
    CSRF required; unknown id 404
  - **allowlist:** a 404 on every gated, issue and PR route with **zero** GitHub calls;
    the executor re-checks the allowlist at approval time (failed, no write)
  - **execution:** a failure is recorded as `failed` with a redacted error; each of the six
    other kinds executes the exact GitHub call once after approval
  - force-push to an unknown sha 422 (nothing filed)
  - **self-approval:** refused once a second admin exists, and that admin can approve;
    with the setting off it is always refused
  - no token at approval time → 503, and the request stays pending
- `tests/test_route_auth.py` pins the 42 new admin routes.

## Open risks

- **The re-auth password crosses the wire again on each approval.** That is by design: the
  server checks it. Only use it over HTTPS (production already enforces that). A
  compromised browser that can read the password can approve; Phase 20 can add WebAuthn
  behind the same interface.
- **Approved but never finished.** If the API dies between `approved` and the GitHub call,
  the request stays `approved` and cannot be re-run (single use). File a new request.
  Phase 20 can add a sweeper that marks stale `approved` rows `failed`.
- **The merge artifact covers the first 32 MiB of the diff.** The head sha covers any
  change beyond that. A diff GitHub refuses (406) hashes as `too_large`, so only the head
  sha binds it.
- **Protection and settings passthrough.** The branch-protection body is passed to GitHub as
  given (validated keys, free-form values), and GitHub validates it. The approver sees it
  in `payload` before approving.
- **Lists are one GitHub page per call.** `has_next` comes from the Link header. There is no
  total count, because GitHub does not give one for issues or PRs.
- **`task_ref` is not verified.** The backend cannot see the frontend workspace document, so a
  link can point at a task that was since deleted. The frontend owns cleaning those up.
