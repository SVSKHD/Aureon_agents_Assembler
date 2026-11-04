# Runtime settings and secrets (core.app_settings)

Some keys Aureon reads from `.env` can also be set from Spasta. You set them at
**/settings**, which redirects to **/dev/settings**: the Settings module of the admin-only control center.

- The value is stored encrypted in Postgres.
- It overrides `.env` and takes effect without restarting a container.
- The browser never gets a value back.

## What can be set

Only the keys in `REGISTRY` (`app/services/app_settings.py`) can be set. Keys marked
**re-auth** need the admin's password for every set or revert.

| Group      | Keys |
|------------|------|
| OpenRouter | `OPENROUTER_API_KEY` (secret, re-auth), `OPENROUTER_ENABLED`, `OPENROUTER_DEFAULT_MODEL`, `OPENROUTER_DAILY_LIMIT_USD`, `OPENROUTER_ALLOWED_MODELS`, `AI_RESEARCH_EXTERNAL_ALLOWED` |
| Dhan       | `DHAN_CLIENT_ID`, `DHAN_API_KEY`, `DHAN_API_SECRET`, `DHAN_ACCESS_TOKEN` (manual fallback) (secrets, re-auth). The URL keys `DHAN_BASE_URL`, `DHAN_FEED_URL`, `DHAN_INSTRUMENT_CSV_URL`, `DHAN_AUTH_URL` and `DHAN_REDIRECT_URL` also need re-auth, because credentials are sent to them. `DHAN_SESSION_TOKEN` and `DHAN_SESSION_EXPIRES_AT` are **managed**: listed and masked, written only by "Log in to Dhan" (`403 managed_key` here; docs/11-dhan.md). Also settable: every other `DHAN_*` the code reads (timeouts, feed, crons, backfill, `DHAN_STATIC_IP`) |
| GitHub     | `GITHUB_TOKEN` (secret, re-auth), `GITHUB_CODING_REPO_ALLOWLIST` (JSON list) |
| Discord    | `DISCORD_WEBHOOK_URL` (secret, re-auth) |
| MT5        | `MT5_INGEST_TOKEN` (secret, re-auth) |

**Bootstrap keys are `.env` only.** These are `DATABASE_URL`, `MIGRATIONS_DATABASE_URL`, `SECRET_KEY`,
`SETTINGS_MASTER_KEY`, `REDIS_URL`, `TRADING_LIVE_ENABLED` and `PHASE_G_APPROVED`. The API
refuses them with `403 bootstrap_key`. A DB row naming any key outside the registry is ignored.

## Precedence

1. A row in `core.app_settings` wins over everything else.
2. Otherwise the value comes from `.env`, then the code default.

The overlay re-validates the `.env` settings with the DB values on top, using the same pydantic
validators. A DB value is therefore parsed exactly like the env var it replaces: a comma list for
`OPENROUTER_ALLOWED_MODELS`, a JSON list for `GITHUB_CODING_REPO_ALLOWLIST`, a bool, a decimal.

**Revert to .env** deletes the row, and the `.env` value or default applies again.

## Storage and encryption

`core.app_settings` has these columns:

- `key` (PK)
- `value_encrypted`
- `is_secret`
- `category`
- `updated_by`, which references `core.users`
- `updated_at`
- `version`, which increases on every replace

It was added by migration `0021`. The migration is additive, and downgrade drops only this table.

Every value is Fernet ciphertext (AES-128-CBC + HMAC-SHA256), including non-secret ones, so
there is one code path and no plaintext column. The key is `SETTINGS_MASTER_KEY` in `.env`, a
44-character urlsafe-base64 Fernet key. The read-only role can `SELECT` the table so that
backups include it. Without the key the ciphertext is useless.

> **Back up `.env` (or at least `SETTINGS_MASTER_KEY`) separately from the DB backups.** If the
> key is lost, the stored values can't be read. Aureon then logs `app_setting_undecryptable` and
> falls back to `.env`, and each value has to be set again from the UI.

If `SETTINGS_MASTER_KEY` is missing or invalid:

- Aureon runs on `.env` alone.
- `GET /api/v1/settings` reports `encryption_ready: false`.
- Writes return `503 settings_key_missing` or `503 settings_key_invalid`.

### First install

```sh
scripts/ops/rotate-settings-key.sh --init   # adds SETTINGS_MASTER_KEY to .env (mode 600)
```

The next deploy picks the key up. `docker compose up -d` recreates the containers when `.env` changes.

### Rotating the key

```sh
scripts/ops/rotate-settings-key.sh            # 1. SETTINGS_MASTER_KEY="new,old", recreate,
                                              #    re-encrypt every row under "new"
docker compose run --rm --no-deps -T api python -m app.workers.settings_key check
scripts/ops/rotate-settings-key.sh --finish   # 2. drop "old", recreate
```

How rotation works:

- **Two keys during rotation.** While `SETTINGS_MASTER_KEY` holds two comma-separated keys, the first one encrypts and both decrypt (`MultiFernet`).
- **All or nothing.** `settings_key rotate` re-encrypts every row in one transaction. If any row can't be read, it changes nothing.
- **Nothing is printed.** The script never prints a key or a value, and it keeps a timestamped copy of `.env` next to it.
- **Containers are recreated.** Changing the key restarts them because it's a bootstrap key. Changing a setting doesn't.

## Reload without a restart

A successful `PUT` or `DELETE` does three things:

1. Commits the row and an audit event.
2. Re-applies the overlay in the API process. `app.state.settings` is swapped, and `get_settings()` returns the new object. Only the clients whose keys changed are rebuilt: OpenRouter, the GitHub client, and the Dhan adapter. The old clients are closed 60 s later, so requests already in flight can finish.
3. Publishes `{"keys": [...]}` on the Redis channel `aureon:settings:changed`.

Workers build their clients once at startup. Each one watches the keys it uses:

| Worker | Keys |
|--------|------|
| `scheduler` | `DHAN_*`, `GITHUB_*`, `DISCORD_*`, `MT5_*` |
| `dhan-feed` | `DHAN_*` |
| `research-worker` | `OPENROUTER_*`, `AI_RESEARCH_*` |
| `coding-worker`, `github-webhook-worker` | `GITHUB_*` |

When one of its keys changes, the worker stops gracefully (the same path as SIGTERM) and
re-executes itself with `os.execv` inside the same container (`app_settings.run_worker`). The
container keeps running and docker restarts nothing. Changes to other keys don't affect it.

How changes reach each process:

- **Redis pub/sub.** The API and every worker that has Redis subscribe to the channel.
- **Fingerprint fallback.** Every process also checks a DB fingerprint (row count, version sum, latest `updated_at`), every 60 s in the API and every 30 s in workers. This catches a missed publish or a Redis outage.
- **Coding worker.** It has no Redis network, so it relies on the fingerprint alone.

## API (admin only; `/api/v1/settings`)

The routes are under `/api/v1/` like the rest of Aureon's admin API.

| Method | Path | What |
|---|---|---|
| GET | `/api/v1/settings` | Every key: `category`, `is_secret`, `requires_reauth`, `is_set`, `masked`, `source` (`db`/`env`/`default`/`unset`), `updated_at`, `version`. `managed`. `DHAN_ACCESS_TOKEN` also gets `expires_at` (from the JWT `exp`) and `expires_soon` (inside `DHAN_TOKEN_WARN_HOURS`); `DHAN_SESSION_TOKEN` gets them from the login's expiry. Also returns the last test per group and `encryption_ready`. **Never a value.** |
| PUT | `/api/v1/settings/{key}` | `{"value": "...", "password": "..."}` sets or replaces the DB value. `password` is required for re-auth keys. |
| DELETE | `/api/v1/settings/{key}` | `{"password": "..."}` (re-auth keys only) reverts to `.env`. |
| POST | `/api/v1/settings/{key}/test` | Tests the key's group with the values in effect now. See the table below. |

What each group's test does:

| Group | Test |
|---|---|
| OpenRouter | `GET /key`, plus a `/models` check that `OPENROUTER_DEFAULT_MODEL` exists |
| Dhan | `GET /fundlimit` |
| GitHub | `GET /user` |
| Discord | `GET` on the webhook. It reads metadata and posts nothing. |
| MT5 | Checks that the token is present and at least 24 characters long |

The test returns `ok` and a status line, never a response body. The result is kept in Redis for 7 days and shown as a chip.

Guards:

- **Who can call it.** `require_admin` (session plus admin role) and CSRF (`x-csrf-token`) on every write.
- **Re-auth.** A re-auth key needs the account password (argon2 verify) on every set and revert. Five wrong passwords per session lock the endpoint for 15 minutes (`429 too_many_reauth_failures`), and each failure is audited (`settings_reauth_failed`).
- **Rate limits.** Per admin: 20 writes and 20 tests per 10 minutes. If Redis is down they fail closed with `503`.
- **Audit.** These events are recorded: `settings_updated`, `settings_reverted`, `settings_tested` and `settings_reauth_failed`. Their details hold the key, category, `is_secret`, version and outcome, never a value.
- **Validation.** Each kind is normalized first:
  - bools must be true or false
  - numbers must be at least 0
  - URLs must be `https://` or `wss://`
  - the Discord URL must be a `discord.com/api/webhooks/...` URL
  - JSON lists are checked as lists

  The whole overlay is then re-validated. A `422` names the field, never the value. Pydantic's own messages include the input, so they are not passed on.

## Keeping values secret

- **API responses.** They show masks only: `••••` plus the last four characters, and only the bullets for secrets shorter than 16 characters. Tests return outcome lines.
- **Logs, incidents and AI prompts.** Each process registers the exact secret values it holds with the log scrubber (`app/core/logging.py`, `register_secret_values`). This covers every registry secret plus `SECRET_KEY`, the DB URLs and the master key. Every structlog event and every captured incident passes through `scrub()`, and so does the incident brief that self-repair sends to a model. A token is removed even when its shape isn't recognisable. Discord webhook URLs are also redacted by pattern.
- **Discord.** Notices never include settings.
- **Subprocesses.** `SETTINGS_`, `OPENROUTER_` and `DISCORD_` were added to the Claude CLI's `DENIED_ENV_PREFIXES`. The coding sandbox already passes only `PATH`/`LANG`/`LC_ALL`/`TZ`.
- **Browser.** The Settings module keeps a typed value only in the input until it is sent, then clears it. Nothing is written to `localStorage`/`sessionStorage`, and there are no `VITE_` variables.

## Tests

- `tests/test_app_settings.py`: registry versus bootstrap keys, encryption and rotation, normalisation without echo, overlay precedence and parsing, masking, Dhan expiry, the scrubber, CLI env denial, and connection tests with mocked HTTP.
- `tests/test_app_settings_db.py` (db): admin-only access, CSRF, re-auth with audit and lockout, set/replace/revert with ciphertext at rest, live reload in the API, bootstrap/unknown/invalid keys, test results, rate limits, pub/sub delivery, and worker reload firing only for the worker's own keys.
