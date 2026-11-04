# Self-describing API (`x-ui` and the UI manifest)

Every route carries UI metadata, so the frontend can wire screens from the API itself instead
of hand-coding each one. The code lives in `app/api/ui.py`.

## Declaring metadata

Each router module declares its UI module once:

```python
from app.api.ui import ui_module, ui_meta

UI_MODULE = ui_module("news", label="News", group="markets", icon="newspaper", order=40)
router = APIRouter(prefix="/v1/news")

@router.post("/refresh", openapi_extra=ui_meta(label="Refresh now", kind="action"))
async def refresh(...): ...
```

`ui_meta(...)` overrides fields on one route. `ui_meta(hidden=True)` keeps a route out of
every manifest.

## The `x-ui` fields

| Field | Meaning | Default when not set |
|---|---|---|
| `module` | screen it belongs to (`news`, `trading`, …) | the router module's `UI_MODULE` |
| `label` | button or menu text | the route summary, else the function name |
| `kind` | `list` · `detail` · `form` · `action` · `stream` | `…/stream` or `…/events` → stream; GET of a list (`list[X]` or `{items: [X]}`) → list; other GET → detail; POST/PUT/PATCH with a body → form; else action |
| `columns` | list columns | up to 6 scalar fields of the item model |
| `title_field` / `id_field` | what to show and how to address a row | first of `title, name, label, …` / `id, key, number, …` in the model |
| `group`, `icon`, `order` | navigation | from `UI_MODULE` |
| `roles` | who may see it: `public`, `user`, `admin` | from the auth dependency (`require_admin` → admin; a session → user+admin; none → public) |
| `danger` | `none` · `confirm` · `approval` · `hidden` | writing methods `confirm`, reads `none`, raised by the safety floor |
| `danger_reason` | why it is not `none` (set by the safety floor) | |

## Safety by default

`SAFETY_RULES` in `app/api/ui.py` sets a **hard floor**. Explicit metadata can raise it but
never lower it.

| Floor | Routes |
|---|---|
| `hidden` | secrets and settings (`/api/v1/settings*`, Dhan login), LIVE mode (`…/mode`, `live`), `git push`, force push, deploys, approval policies and re-auth, auth flows, machine endpoints (GitHub webhooks, the MT5 push) |
| `approval` | order intents and their approval, paper execution writes, approval decisions, PR merges, code-change and repair approvals, CI dispatch/rerun/cancel, branch protection, repository settings, Actions permissions, branch deletes |
| `confirm` | agent and scheduler control, and every other writing method unless a route sets it lower |

The frontend must never render `approval` as a plain button. It goes through the approval
flow (re-auth). `hidden` routes are not in the manifest at all.

## Endpoints

| Route | Auth | What |
|---|---|---|
| `GET /api/v1/ui/manifest` | signed in | `{schema_hash, modules: [{module, label, group, icon, order, operations: [{method, path, operation_id, x-ui}]}]}` for this viewer. Hidden operations are absent, and admin-only ones appear only for admins |
| `GET /api/openapi.json` | signed in | the OpenAPI document with `x-ui` on every operation. Never public, in any environment |
| `GET /api/docs` | signed in, dev only | Swagger UI over the document above |

## Schema version

- At startup the API hashes the canonical OpenAPI document, including the `x-ui` metadata (`app.state.schema_hash`, 16 hex characters).
- Every `/api` response carries `X-API-Schema: <hash>`.
- `GET /api/trading/stream` sends `schema_hash` in `hello`.
- At startup the API records the hash in Redis (`aureon:api:schema`). When it changed, it also records the previous hash and the time of the change.
- A reconnecting stream within 10 minutes of a change gets one `schema_changed` event: `{hash, previous}`.
- A running stream gets one when another API process announces a newer hash.
- The frontend then refetches the manifest. A deploy restarts the API and drops open streams, so the reconnect is what delivers the event.

## CI

- `tests/test_ui_manifest.py::test_every_route_declares_ui_metadata` fails for a route whose module has no `UI_MODULE`, that has no `ui_meta(...)` and that isn't marked hidden.
- Other tests check:
  - the manifest's shape and its filtering by role
  - that hidden routes are absent
  - that every sensitive route is `approval` or `hidden`
  - that explicit metadata can't lower a floor
  - that the hash changes when the routes change
- CI also exports `openapi.json` as the `openapi` workflow artifact, kept for 30 days. The frontend regenerates its typed client from it (`pnpm api:gen`).
