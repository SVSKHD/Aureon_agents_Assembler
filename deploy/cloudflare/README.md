# Cloudflare Tunnel + Access for spasta.online and api.spasta.online (Phase 22)

Nothing on the server listens publicly. cloudflared opens an outbound tunnel; Cloudflare
Access decides who gets through; nginx on `127.0.0.1:8088` is the only origin; the API stays
on `127.0.0.1:8000` (compose publishes nothing else).

```
browser / GitHub ──TLS──▶ Cloudflare edge ── Access policy ──▶ tunnel ──▶ cloudflared
                                                                       (access.required: verifies the JWT again)
                                                                              │
                                     nginx 127.0.0.1:8088  ◀───────────────────┘
                                     (spasta.online: SPA + /api/ ; api.spasta.online: /api/ only,
                                      Cf-Access-Jwt-Assertion required except the bypass paths)
                                              │
                                     Aureon API 127.0.0.1:8000
                                     (optional CF_ACCESS_* JWT check, then session + CSRF as always)
```

| File | What |
| --- | --- |
| `config.yml.example` | cloudflared ingress: both hostnames → `http://127.0.0.1:8088`, bypass paths first, `access.required` on the rest, `http_status:404` catch-all |
| `access.tf.example` | Access applications and policies (Terraform-style reference; the same as the steps below) |
| `../nginx/api-spasta-online.conf` | the `api.spasta.online` server block (requires the Access header) |
| `../nginx/aureon-api.conf` | the existing `/api/` include for the `spasta.online` server block (unchanged) |

## Policies

| Application (domain) | Policy | Session |
| --- | --- | --- |
| `spasta.online` | Allow: email = the owner (one address) | 24 h |
| `api.spasta.online` | Allow: email = the owner; Service Auth: service token `aureon-ops` | 24 h |
| `api.spasta.online/api/v1/github/webhooks` | **Bypass** (GitHub cannot send Access headers; the app verifies the HMAC signature and dedups on the delivery id) | – |
| `spasta.online/api/public`, `api.spasta.online/api/public` | **Bypass** (read-only, rate-limited public endpoints) | – |

Nothing else is bypassed. Cloudflare picks the most specific path, so the bypass apps only
cover their own paths. `/api/health` is not exposed through Access at all: the healthchecks
run on the box against `127.0.0.1:8000`, and nginx answers 404 to `/api/health/ready`.

## Steps (dashboard: Zero Trust)

1. **Tunnel.** `cloudflared tunnel login` (once), `cloudflared tunnel create spasta`, then
   `cloudflared tunnel route dns spasta spasta.online` and `... api.spasta.online`.
   If a tunnel for spasta.online already exists, reuse it and only add the api hostname.
2. **Identity.** Settings → Authentication: one-time PIN (email) or your IdP.
3. **Applications.** Access → Applications → Add → Self-hosted, for each row of the table:
   name, domain (with the path for the bypass rows), session duration 24 h, "HTTP Only" and
   "SameSite Strict" cookies, binding cookie on. Policies as in the table:
   - *Owner only*: Action Allow, Include → Emails → the owner's address.
   - *Ops service token*: Access → Service Auth → Create `aureon-ops`; then on
     `api.spasta.online` a policy with Action **Service Auth**, Include → Service Token.
     Store the client id/secret in a password manager; never in the repo.
   - *Bypass*: Action Bypass, Include → Everyone (only on the three path applications).
4. **AUD tags.** Copy each protected application's *Application Audience (AUD) Tag* (Overview
   tab). They go into `config.yml` (`audTag`) and into `.env` as
   `CF_ACCESS_AUD=<web aud>,<api aud>` with `CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com`.
5. **cloudflared.** Copy `config.yml.example` to `/etc/cloudflared/config.yml`, fill the
   placeholders, `sudo cloudflared tunnel ingress validate`, `sudo systemctl restart cloudflared`.
6. **nginx.** Install `deploy/nginx/api-spasta-online.conf` (sudo; manual-setup.md "22"),
   `sudo nginx -t && sudo systemctl reload nginx`.
7. **App check (optional, defence in depth).** Set the two `CF_ACCESS_*` variables and
   `docker compose up -d api`. The API then refuses (403 `cf_access_required`) any request
   without a valid Access token, except `/api/v1/github/webhooks`, `/api/public/*` and
   `/api/health*`.
8. **GitHub.** Webhook payload URL `https://api.spasta.online/api/v1/github/webhooks` (see
   docs/20-github-actions-webhooks.md for the secret).

## Validate

```bash
# From anywhere (no Access session): the protected hosts redirect/deny, the bypasses answer.
curl -s -o /dev/null -w '%{http_code}\n' https://api.spasta.online/api/auth/me          # 302/403 (Access)
curl -s -o /dev/null -w '%{http_code}\n' https://api.spasta.online/api/public/forex     # 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://api.spasta.online/api/v1/github/webhooks  # 401/400 from the app (bad HMAC), never an Access page
# With the service token:
curl -s -H "CF-Access-Client-Id: $CF_ID" -H "CF-Access-Client-Secret: $CF_SECRET" \
  https://api.spasta.online/api/health/ready -o /dev/null -w '%{http_code}\n'           # 404 (nginx: on-box only)
curl -s -H "CF-Access-Client-Id: $CF_ID" -H "CF-Access-Client-Secret: $CF_SECRET" \
  https://api.spasta.online/api/auth/me -o /dev/null -w '%{http_code}\n'                # 401 (past Access; no app session)
# On the box: no public listener.
ss -tlnp | grep -E ':(8000|8088|5432|6379|11434)\b'    # all on 127.0.0.1 only
```

## Rollback

- Access: disable a policy or delete the application in the dashboard (takes effect at once).
- App check: unset `CF_ACCESS_TEAM_DOMAIN` / `CF_ACCESS_AUD`, `docker compose up -d api`.
- nginx: remove the `api-spasta-online.conf` link, `sudo nginx -t && sudo systemctl reload nginx`.
- cloudflared: restore the previous `/etc/cloudflared/config.yml`, restart the service.
