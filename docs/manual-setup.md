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
