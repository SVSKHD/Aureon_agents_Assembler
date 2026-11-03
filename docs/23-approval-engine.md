# 23 — Approval engine

Phase 20 of the roadmap. Phases 16 and 17 built a minimal approval gate
(`app/services/approvals.py`, [docs/19](19-github-issues-prs.md)). This phase grows it into
the full engine behind the **same interface** (`request` / `approve` / `deny`, the executor
registry), so every existing caller and test keeps working:

- **policies** per kind (`audit.approval_policies`): required approvals, expiry,
  self-approval, re-auth, notification channels, risk level. The most specific pattern wins.
- **artifacts** (`audit.approval_artifacts`): several named artifacts per request. Drift in
  any of them invalidates the request.
- **multi-approval**: a policy can require N distinct admins.
- **server-side re-auth**: the password, or a short-lived session re-auth stamp kept in Redis.
- **expiry**: lazy, plus the scheduler sweep `approval_expire`.
- **hash-chained events**: every state change is an event, and each event stores
  sha256(prev hash + itself). The API reports whether the chain verifies.
- **Spasta endpoints**: list with counts, inbox, detail, approve, deny, SSE stream, policies.
- **Discord notifications**: requested / approved / denied / expired, with a deep link only.
- **voice navigation**: a transcript becomes a route. It never approves anything.
- **wiring**: PR merge (repair PRs get their own policy), prod dispatch, and **paper trading
  intents** (`trading.paper_intent`).
- `GET /api/v1/audit/events` for the frontend audit module.

## What it does

```
caller ──► gate.request(kind, target, payload, artifact, artifacts={name: …}, summary=…)
             subject = spec.policy_subject(kind, payload) or kind   (repair PR → repair.pr_merge)
             policy  = most specific enabled audit.approval_policies row matching subject
             request: status pending, policy snapshot, required_approvals, risk, expires_at
             artifacts: one row per name (sha256, size, capped content)
             event "requested" (hash-chained) + audit row + Discord notice (after commit)

POST /approvals/{id}/approve  {password} | {use_reauth: true}
   ├─ password wrong ─► 403 reauth_failed (counted; N failures deny the request)
   ├─ no password, policy needs re-auth and no young-enough stamp ─► 403 reauth_required
   ├─ expired ─► 409 approval_expired      ├─ not pending ─► 409 approval_already_used / _not_pending
   ├─ dependency missing ─► 503 (still pending)
   ├─ own request and the policy does not allow it ─► 403 self_approval_not_allowed
   ├─ already approved by this admin ─► 409 already_approved_by_you
   ├─ received < required ─► event approval_recorded, 200 status pending
   └─ quorum ─► status approved (single use) ─► recompute every artifact
          ├─ any sha256 differs ─► 409 approval_invalidated (status invalidated, nothing runs)
          └─ executor ─► executed | failed (200)
```

### Policies

`audit.approval_policies.kind_pattern` is an fnmatch pattern matched against the request's
*policy subject*. That subject is the kind, except for merges of self-repair PRs, which use
`repair.pr_merge`. A PR counts as a repair PR when its head branch starts with `repair/` or
it is labelled `repair`. Among the enabled policies that match, the most specific one wins:

1. an exact pattern beats any wildcard
2. then the pattern with more literal characters
3. then the one with fewer wildcards
4. on a full tie, the lowest id

If nothing matches, or the table is empty, the built-in Phase 16 rules apply: one approval,
`APPROVALS_TTL_MINUTES`, and the password every time.

The policy is **snapshotted onto the request** when it is filed. Editing a policy never
changes the rules of requests that are already pending.

| Column | Meaning |
| --- | --- |
| `required_approvals` | 1–5 distinct admins. |
| `expiry_seconds` | NULL = `APPROVALS_TTL_MINUTES`. |
| `allow_self_approval` | true = the D-16-3 rule: the requester may approve only while they are the only active admin, and only with `APPROVALS_ALLOW_SELF_APPROVAL=true`. false = never. |
| `require_reauth` | false = the admin session alone is enough. |
| `reauth_max_age_seconds` | A session re-auth stamp younger than this counts as the password. 0 = the password every time. |
| `notify_channels` | `["discord"]` or `[]`. |
| `risk_level` | low / medium / high / critical, shown in the UI and notifications. |
| `enabled` | Disabled rows are ignored. Disable a policy rather than delete it. |

Seeded defaults (the migration inserts them with `ON CONFLICT DO NOTHING`, so your edits are
kept):

| Pattern | Approvals | Stamp max age | Risk |
| --- | --- | --- | --- |
| `*` | 1 | 300 s | medium |
| `github.*` | 1 | 300 s | medium |
| `github.pr.merge` | 1 | 300 s | high |
| `github.ref.force_update` | 1 | 0 (password) | critical |
| `github.branch_protection.*`, `github.branch.delete`, `github.repo.update_settings`, `github.actions.set_permissions` | 1 | 0 | high |
| `github.workflow_dispatch_prod` | 1 | 0 | critical |
| `repair.*` (self-repair PR merges) | 1 | 0 | high |
| `trading.paper_intent` (expiry 900 s) | 1 | 300 s | medium |

### Artifacts and drift

`gate.request(..., artifact, artifacts={...})` stores one `audit.approval_artifacts` row per
name. The single `artifact` argument is stored under the name `primary`. Each row holds the
sha256 and size of the canonical form (JSON with sorted keys), plus the text capped at
`APPROVALS_ARTIFACT_MAX_CHARS` (`truncated` when cut). Binary artifacts store only
`content_ref = sha256:<hex>`.

The request's `artifact_sha256` is computed as follows:

- with only a `primary` artifact, it is that artifact's hash, unchanged from Phase 16
- otherwise, it is the hash of the sorted `{name: sha256}` map

Before execution, `ExecutorSpec.current_artifacts` (or the Phase 16 `current_artifact`)
recomputes every artifact. Any missing, extra or different name invalidates the request, and
the event lists the names that drifted. `GET /approvals/{id}?check_drift=true` runs the same
comparison without consuming anything. That call may hit GitHub, so it is opt-in. Requests
filed before this phase have no artifact rows and are compared by `artifact_sha256`, as
before.

### Re-auth

- **Password**: `{"password": …}`, verified with Argon2 in a thread and never stored. This is
  unchanged from Phase 16, including the failure counter.
- **Session stamp**: `POST /api/v1/approvals/reauth {"password"}` verifies the password once
  and writes `approvals:reauth:<session id>` to Redis as `{user_id, at}`. Its TTL is the
  largest `reauth_max_age_seconds` among the enabled policies, capped by
  `APPROVALS_REAUTH_STAMP_MAX_SECONDS`. To use it, approve with `{"use_reauth": true}`. The
  stamp counts only when it belongs to this session and this user, and only if it is younger
  than **the request's own** policy max age. Five wrong passwords in 15 minutes lock the
  endpoint for that session (429). `DELETE /reauth` drops the stamp. Nothing is put in a
  cookie.
- `{}` is still a 422, so a session cookie alone never approves.

### Events and the hash chain

`audit.approval_events` gained `prev_hash` and `hash`:

```
hash = sha256(prev_hash + canonical_json({request_id, event, actor, details, at}))
```

`at` is written with microsecond precision. The request row keeps `event_head_hash`, which
anchors the chain so that a deleted tail is detected too. `verify_chain` reports:

- `ok`
- `broken`, with `broken_at` and `reason` (`hash mismatch`, `prev_hash mismatch`,
  `head hash mismatch`, or `unhashed event in chain`)
- `legacy`: only events from before this phase
- `empty`

Events from before this phase (hash NULL) may only come before the first hashed event.

The events are: `requested`, `approval_recorded` (a partial approval), `approved`, `denied`,
`expired`, `invalidated`, `executed`, `failed`, `reauth_failed` and `self_approval_refused`.
Each one is also written as an `audit.audit_events` row named `approval_<event>`.

### Expiry

Expiry is lazy, as before: a due request is expired when it is read, listed, approved or
denied. In addition, the scheduler job `approval-expire` (task `approval_expire`, cron
`APPROVALS_EXPIRE_CRON`, every minute by default) marks every due request in batches of 200.
Each expiry emits an event, an audit row and a Discord notice.

### Notifications (Discord)

`app/integrations/discord.py`. After the transaction **commits**, the engine queues the
notices (an SQLAlchemy `after_commit` hook; a rollback drops them) and
`notify_background` posts them from a background task. Notices go out for the
`requested`, `approved` (quorum reached), `denied` and `expired` events, when the policy's
`notify_channels` include `discord`.

- **Body:** an allowlist of fields: kind, target, summary, risk, approvals n/m when more
  than one is needed, and the link `${PUBLIC_APP_URL}/dev/approvals/{id}`. The payload, the
  artifacts, results and actors are never included. Text is passed through the credential
  redactor and capped, `@` is defused, and `allowed_mentions` is empty.
- **No way to approve from Discord:** there are no components or buttons, no interaction
  endpoint, and no token in the link. The link only opens the app, where the admin signs in
  and re-authenticates.
- **Delivery:** at most `DISCORD_MAX_ATTEMPTS` tries. 429 (honouring `retry_after`, at most
  10 s), 5xx and network errors are retried with exponential backoff. Other 4xx are not
  retried. Failures are logged without the URL and never reach the approval flow. At most
  100 deliveries are pending at once; anything more is dropped with a log line.

### Voice navigation

`app/integrations/voice.py` and `POST /api/v1/voice/intent {"transcript"}` (admin, CSRF).
Speech-to-text happens on the client. A deterministic grammar returns
`{intent, route, params, source, note}`:

| Intent | Route | Example |
| --- | --- | --- |
| `open_approvals_inbox` | `/dev/approvals` | "open approvals", "show pending" |
| `open_approval` | `/dev/approvals/{ref}` | "approve request 123", "deny request abc12" |
| `open_repairs` | `/dev/repairs` | "open repairs" |
| `open_pr` | `/dev/prs/{n}` | "merge PR 42", "open pull request 9" |
| `show_agents` | `/dev/agents` | "show agents" |
| `unknown` | null | anything else |

If the grammar finds nothing and `VOICE_OLLAMA_MODEL` is set, the local Ollama model may
choose one of the same five intents. Its answer is validated, and the route is always built
by the code (a model cannot invent a route or an id format).

Action words (approve, deny, merge, execute, run, deploy, and the trading/write verbs buy,
sell, trade, order, close, cancel, delete, create, write, push) only add the note "navigation
only: review and act in the app". There is no trading or write intent: "buy 100 shares" is
`unknown`, "approve request 5" opens request 5. The module imports no database, approval, GitHub or trading
code, and the endpoint writes nothing, not even an audit row. The transcript is never logged.

**In the browser.** The control center header has a mic button (admin only). One tap starts
one utterance with the browser's Web Speech API (a user gesture, `continuous = false`, which is
what iOS Safari needs); the final transcript goes to this endpoint, and the SPA navigates only
if the returned route matches the same five patterns (it re-checks; it never calls any other
endpoint). There is no server-side speech-to-text: Android Chrome sends the audio to Google's
recogniser and iOS Safari to Apple's (or on-device); only the transcript reaches Aureon. A
local path (MediaRecorder upload to a whisper.cpp worker) would keep audio on the box but needs
a new upload endpoint and model; it is not built.

**Headers.** The SPA's Permissions-Policy must allow `microphone=(self)`
(deploy/nginx/security-headers.conf). With `microphone=()` the browser refuses the mic before
any prompt (Chrome: SpeechRecognition `not-allowed`), which is why voice "did nothing". The
Web Speech API needs no CSP change. Apply: `sudo nginx -t && sudo systemctl reload nginx`
(the sites are symlinked from this checkout, so a deploy pull plus a reload is enough).

**Tailscale.** `https://svskhd8.taila439d5.ts.net` is served by `tailscale serve`, which must
own :443 on the tailnet addresses. Any other process listening on `*:443` (e.g. a Caddy
install) wins the bind at boot and answers the ts.net name with a TLS alert. Check
`ss -ltnp 'sport = :443'` and `journalctl -u tailscaled | grep "address already in use"`.

### Wiring

- **PR merge** (`github.pr.merge`): `request_merge` adds `head_ref` and `labels` to the
  approval payload. These are policy metadata only and are never sent to GitHub.
  `merge_policy_subject` maps `repair/*` branches, or the `repair` label, to
  `repair.pr_merge`, so the `repair.*` policy applies: the password every time, by default
  one approval, and risk high. Phase 18 opens repair PRs from `repair/<id>` and merges them
  through this same endpoint.
- **Prod dispatch / re-runs** (`github.workflow_dispatch_prod`): unchanged. They now resolve
  their own policy (password every time, risk critical).
- **Paper trading intents** (`trading.paper_intent`, registered in `app/api/v1/paper.py`):
  - **Artifacts:** `intent` (the intent id, account, instrument, side, type, quantity,
    limit and stop prices, product, mode and status) and `risk_decision` (the id and
    `approved` flag of the latest risk decision). A cancel, a status change or a new risk
    decision between filing and approval invalidates the request.
  - **Executing** runs the unchanged `paper.approve_and_execute`: risk is re-run, then the
    simulated fill. A refusal is audited as `order_intent_approve_denied`, as before.
  - **Requester:** the intent's creator.
  - **Scope:** PAPER only. LIVE intents or accounts are refused before anything is filed,
    and there is no live kind. Risk logic and limits are untouched.

### Frontend compatibility (Paper tab → inbox)

`POST /api/trading/order-intents/{id}/approve` is now a thin, backward-compatible wrapper:

| Call | Answer |
| --- | --- |
| no body / `{}` | **202** `{approval_id, status: "pending", kind: "trading.paper_intent", target, artifact_sha256, expires_at, required_approvals, received_approvals}`. A repeat returns the same pending request. |
| `{"password": "…"}` and the policy needs 1 approval | **200** with the intent (FILLED, SUBMITTED or REJECTED by risk), as before. |
| `{"password": "…"}` and the policy needs more than 1 | 202, as above (approve from the inbox). |
| wrong password | 403 `reauth_failed` (with `approval_id`). |
| not PENDING_RISK, or LIVE | 409 `not_pending` / `live_unreachable`, as before. |
| drift | 409 `approval_invalidated`. Call again to file a fresh request. |

What the Phase 21 Paper tab should do:

1. Call approve with no body, then send the user to `/dev/approvals/{approval_id}`, or show
   it in the inbox (`GET /api/v1/approvals/inbox`, kept live by
   `GET /api/v1/approvals/stream`).
2. Approve through `POST /api/v1/approvals/{id}/approve`. The result carries
   `result.intent_status`.
3. Re-fetch the intent.

The old one-call flow keeps working while the tab is migrated, but it now requires
`{"password"}`.

## Endpoints

All of them are admin only. Unsafe methods need CSRF.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/approvals?status=&kind=&limit=&offset=` | Page plus `counts` by status (for the kind filter). Expires due requests first. |
| GET | `/api/v1/approvals/inbox` | Pending requests this admin can act on (not already approved by them, not their own unless allowed), with `reauth`: `password`, `session_stamp` or `none`, plus `reauth_stamp_expires_at`. |
| GET | `/api/v1/approvals/stream` | SSE. Sends `hello`, then `approval` events (id = the event id; kind, target, status, approvals, risk, summary, never the payload), then `bye`. `: ping` heartbeats. Resumes with `Last-Event-ID` or `?last_event_id=`. Max `APPROVALS_STREAM_MAX_SECONDS`. |
| GET | `/api/v1/approvals/{id}?check_drift=` | The request with `events`, `artifacts`, `policy` (snapshot), `drift` (`ok`/`drifted`/`unknown`/`not_checked`/`no_artifact`), `chain` and `approvers`. |
| GET | `/api/v1/approvals/{id}/events` | The events plus `chain` (`ok`/`broken`/`legacy`/`empty`). |
| POST | `/api/v1/approvals/{id}/approve` | `{"password"}` or `{"use_reauth": true}`. Answers 200 with `pending` (more approvals needed), `executed` or `failed`. 403 `reauth_failed`, `reauth_required` or `self_approval_not_allowed`. 409 `approval_expired`, `approval_already_used`, `approval_not_pending`, `already_approved_by_you` or `approval_invalidated`. 503. |
| POST | `/api/v1/approvals/{id}/deny` | `{"reason"}`. 409 unless pending. |
| POST / DELETE | `/api/v1/approvals/reauth` | Stamp this session (`{"password"}`, giving `{stamped_at, expires_at, ttl_seconds}`; 403 / 409 `reauth_stamp_disabled` / 429), or drop the stamp (204). |
| GET | `/api/v1/approvals/policies` | All policies. |
| PUT | `/api/v1/approvals/policies` | `{"password", "policies": [...]}`, upserted by `kind_pattern` (`[a-z0-9_.*?-]`). Rows that are not listed are kept. Audited as `approval_policies_updated` with before and after. |
| POST | `/api/v1/voice/intent` | `{"transcript"}`, returning `{intent, route, params, source, note}`. Navigation only. |
| GET | `/api/v1/audit/events?event_type=&actor_user_id=&since=&until=&limit=&offset=` | `audit.audit_events`, newest first. `event_type` is a literal prefix. Each row has id, event_type, actor_user_id, actor_email, target_user_id, ip, user_agent, request_id, details (already redacted by the writers) and created_at. `limit` ≤ 200. |
| POST | `/api/trading/order-intents/{id}/approve` | The wrapper above. |

## Tables (migration 0017, additive)

- **`audit.approval_policies`:** id, kind_pattern (unique), required_approvals,
  expiry_seconds, allow_self_approval, require_reauth, reauth_max_age_seconds,
  notify_channels jsonb, risk_level, description, enabled, updated_by (a uuid with no FK,
  because policies are configuration), created_at and updated_at. Seeded as above.
- **`audit.approval_artifacts`:** id, request_id (FK, cascade), name, sha256, size, content,
  content_ref, truncated and captured_at. Unique on (request_id, name).
- **`audit.approval_requests`** gains: policy_id (FK, SET NULL), policy (jsonb snapshot),
  policy_subject, required_approvals (default 1), received_approvals (default 0), risk_level
  (default medium), summary, requester_ip and event_head_hash.
- **`audit.approval_events`** gains prev_hash and hash.

## Config

| Variable | Default | Meaning |
| --- | --- | --- |
| `APPROVALS_TTL_MINUTES` | `30` | Expiry when the policy has none (Phase 16). |
| `APPROVALS_ALLOW_SELF_APPROVAL` | `true` | Global switch for the self-approval rule (Phase 16). |
| `APPROVALS_MAX_REAUTH_FAILURES` | `5` | Wrong passwords before a request is denied, and before `/reauth` locks the session for 15 min. |
| `APPROVALS_REAUTH_STAMP_MAX_SECONDS` | `900` | Cap on the stamp TTL. `0` disables stamps. |
| `APPROVALS_ARTIFACT_MAX_CHARS` | `65536` | Artifact text stored per row. |
| `APPROVALS_EXPIRE_CRON` | `* * * * *` | The `approval-expire` job. |
| `APPROVALS_STREAM_POLL_SECONDS` / `_HEARTBEAT_SECONDS` / `_MAX_SECONDS` | `2` / `15` / `1800` | SSE. |
| `PUBLIC_APP_URL` | `https://spasta.online` | Base for deep links. |
| `DISCORD_WEBHOOK_URL` | empty | The webhook (secret). Empty disables notifications. |
| `DISCORD_MAX_ATTEMPTS` / `DISCORD_REQUEST_SECONDS` / `DISCORD_RETRY_BASE_SECONDS` | `3` / `5` / `1` | Delivery. |
| `VOICE_OLLAMA_MODEL` | empty | Optional local model for the voice fallback. |

## Deploy and validate

1. Pull, then `docker compose run --rm migrate` (0017). Check with
   `SELECT kind_pattern FROM audit.approval_policies;`, which should list the 11 defaults.
2. Optionally set `DISCORD_WEBHOOK_URL` and `PUBLIC_APP_URL` (see manual-setup), then
   `docker compose up -d api scheduler`. The scheduler creates the `approval-expire` job on
   start.
3. Validate:
   ```bash
   curl -s -b jar https://api.spasta.online/api/v1/approvals/policies | jq length          # 11
   curl -s -b jar https://api.spasta.online/api/v1/approvals | jq .counts
   # file a paper approval, then look for it in the inbox
   curl -s -b jar -H "x-csrf-token: $CSRF" -X POST \
     https://api.spasta.online/api/trading/order-intents/$INTENT/approve | jq .approval_id
   curl -s -b jar https://api.spasta.online/api/v1/approvals/inbox | jq '.items[].kind'
   curl -s -b jar https://api.spasta.online/api/v1/approvals/$ID/events | jq .chain.status  # "ok"
   ```
   A Discord message should arrive for the request, and its link should open the app.

## Rollback

- **Code:** redeploy the previous image. The Phase 16 gate ignores the new columns.
  Old-style requests keep working, and requests filed by the engine stay approvable by the
  old code under Phase 16 rules.
- **Migration:** `alembic downgrade 0016` drops only what 0017 added: the two new tables and
  the new columns. This deletes the policies, artifacts and hashes. Export them first if
  needed: `\copy (SELECT * FROM audit.approval_policies) TO 'policies.csv' CSV HEADER`.
- **Notifications off:** unset `DISCORD_WEBHOOK_URL`.
- **Paper tab:** the old one-call approve still works with `{"password"}`.

## Tests

- `tests/test_approval_engine_unit.py`:
  - policy resolution and specificity, snapshot round trip, combined sha
  - hash-chain verification: ok, tampered, prev mismatch, deleted tail, legacy prefix
  - Discord: the payload is an allowlist (no payload or tokens, no mentions, no components,
    a deep link), and retries are capped (429/5xx retried, 4xx not), never raising
  - voice: the grammar table, action words flagged, the Ollama fallback validated (a model
    cannot invent intents or routes), and the voice modules import nothing stateful
  - the repair-PR policy subject
- `tests/test_approval_engine_db.py`:
  - the most specific policy wins (disabled rows skipped), policy expiry, seeded defaults
  - PUT policies: password, audit, validation, admin only
  - two-admin approval: pending after the first, `already_approved_by_you`, executed after
    the second, single use, inbox per admin, a chain of 4 that verifies
  - tampering and a deleted tail are detected
  - drift on one of two artifacts (`check_drift`, then invalidated with `drifted: ["b"]`)
  - the `approval_expire` sweep, with the events and Discord notices
  - the stamp TTL (300 s from the policies), the stamp's max age per policy, per-session
    stamps, max age 0, DELETE
  - Discord failure (500 / network) never blocks, and the posted bodies hold no secrets
  - the SSE stream and resuming it, list counts, the audit API (filters, literal prefix,
    admin only, limit), and voice changing nothing
- `tests/test_paper_db.py`:
  - the existing tests go through the wrapper with `{"password"}`
  - new: 202 filing and reuse, approve from the inbox, single use, wrong password, drift
    when the risk decision changes (then a fresh request fills), and no live kind
- All Phase 16, 17 and 19 approval tests pass unchanged.

## Open risks

- The hash chain detects edits made through the database, but someone with DB write access
  could rewrite a whole chain consistently. Anchoring the head hash outside the database
  (for example in a periodic Discord message or an off-box log) would close that gap.
- `required_approvals` greater than the number of active admins can never be met. Such
  requests simply expire, so keep N ≤ the number of admins.
- Policy edits apply only to new requests. To tighten rules for pending requests, deny them.
- The SSE stream polls with a 1 s lag. A transaction that commits more than 1 s after its
  events were stamped could be missed by a live stream. The inbox list is the source of
  truth.
- Voice routes such as `/dev/prs/{n}` assume the Phase 21 frontend route names.
