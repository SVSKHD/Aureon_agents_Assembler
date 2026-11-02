# Manual setup

Steps that need the production server, secrets or a human decision. Each phase appends a
section.

## D-backend — Frontend support APIs

- **Migration:** run `alembic upgrade 0011` (additive: `core.finance_tombstones`,
  `core.finance_attachments`).
- **HDD directory:** `${DATA_ROOT}/finance-attachments/` is created on first upload. The API
  container's user needs write access to `DATA_ROOT`, the same as for imaging. If the mount is
  root-owned, create the directory and `chown` it to the container user (sudo).
- **Proxy body size:** set the reverse proxy / Cloudflare request body limit to about
  `FINANCE_ATTACHMENT_MAX_BYTES` + 1 MiB (default 11 MiB), so oversize uploads stop at the edge.
- **News feeds (optional, off by default):** set `NEWS_FEED_URLS` to a one-line JSON list of
  RSS/Atom URLs, e.g. `NEWS_FEED_URLS=["https://example.com/fx.rss"]`. Restart the scheduler, so
  the `news-pull` job is created. Outbound HTTPS from the scheduler container must be allowed.
- **Frontend:** create shares with unguessable doc ids (UUIDs) in namespace `aureon-shares`.

## 15 — GitHub foundation

See [18-github-foundation.md](18-github-foundation.md). Without these steps the integration
stays off: `GET /api/v1/github/connection` says `configured: false` and the other GitHub
endpoints answer 503 `github_not_configured`.

1. **Create a fine-grained personal access token.** On GitHub, go to Settings → Developer
   settings → Personal access tokens → Fine-grained tokens → Generate new token.
   - **Resource owner:** your user, or the organisation that owns the repos (an org may
     have to approve fine-grained tokens first).
   - **Expiration:** 90 days at most. Put a calendar reminder a week before it expires.
     Rotating means a new token in `.env` plus a restart. The old connection row stays as
     history under its own fingerprint.
   - **Repository access:** "Only select repositories". Pick exactly the repos Aureon should
     see (e.g. `Aureon_agents_Assembler` and the frontend repo), never "All repositories".
   - **Repository permissions:**
     - Metadata: **Read** (mandatory)
     - Contents: **Read and write**
     - Pull requests: **Read and write**
     - Issues: **Read and write**
     - Actions: **Read and write**
     - Workflows: **Read and write**, only if workflows will be dispatched or edited (Phase 17);
       otherwise leave it at no access
     - Everything else: no access. This phase only reads; the write permissions are for
       Phases 16–17, so they are asked for once.
   - **Account permissions:** none.
2. **Put it on the server only:** in the backend `.env` next to `compose.yaml`
   (`chmod 600 .env`):
   ```
   GITHUB_TOKEN=github_pat_...
   GITHUB_ALLOWED_OWNERS=["SVSKHD"]
   # optional, an exact list:
   GITHUB_ALLOWED_REPOS=["SVSKHD/Aureon_agents_Assembler"]
   ```
   Never put it in the frontend: no `VITE_*` variable, nothing in the browser, nothing in Git.
   The frontend talks only to `/api/v1/github/*`, which never returns the token.
3. **Restart:** run `docker compose up -d api scheduler`, then, as an admin,
   `POST /api/v1/github/connection/check`. Expect `ok: true` and your login. Then
   `GET /api/v1/github/repositories?refresh=true`.
4. **If it leaks** (it shows up in a log, a paste or a commit): revoke it on GitHub
   immediately, create a new one and restart. Every log line and error is redacted, but
   revoking is the only real fix.
