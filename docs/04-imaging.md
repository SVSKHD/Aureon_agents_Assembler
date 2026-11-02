# 04 — Image generation

**Status:** API, worker, tables (revision `0004`) and tests are done. Hooking up the real
pipeline is **pending**: it needs the `/opt/image-agent` entrypoint from
[00-inspection.md §5](00-inspection.md#5-image-agent-and-tunnel). Until then the worker runs only
with the stub generator, which is for dev and tests.

The API never generates images. It stores a job and returns 202, and a separate worker process
runs one Stable Diffusion job at a time. At about 10 minutes per 512×512 on this CPU, clients
poll for status.

## Endpoints

All require a session. `POST` and `DELETE` also require `X-CSRF-Token`.

| Method | Path | Result |
| --- | --- | --- |
| POST | `/api/images/generate` | 202 job · 422 bad size, steps or prompt · 429 + `Retry-After` when the queue is full · 503 HDD not mounted |
| GET | `/api/images/jobs/{id}` | 200 job with `status`, `progress` 0–100, `queue_position`, `error` and `asset_id` once done |
| POST | `/api/images/jobs/{id}/cancel` | 200 job. A queued job becomes `cancelled` at once; a running one gets `cancel_requested` and stops within about 2 s |
| GET | `/api/images?limit=24&offset=0` | 200 your gallery, newest first, each item with a `url` |
| GET | `/api/images/{asset_id}/file` | 200 `image/png` (private cache, `nosniff`) · 404 |
| DELETE | `/api/images/{asset_id}` | 204, removes the row and the file · 404 |

Another user's job or image returns 404, the same as one that doesn't exist.

Request body:

```json
{"prompt": "a lighthouse at dusk, oil painting", "negative_prompt": "blurry",
 "seed": 42, "steps": 25, "size": "512x512"}
```

- **`size`** must be one of `IMAGE_ALLOWED_SIZES` (default `512x512`, `512x768`, `768x512`).
- **`steps`** defaults to `IMAGE_DEFAULT_STEPS` (25) and may not exceed `IMAGE_MAX_STEPS` (50).
- **`seed`** is random if omitted. It is stored with the job, so an image can be reproduced.
- **Prompts** are at most 1000 characters with no control characters.

## Job lifecycle

```
queued ──claim──▶ running ──▶ done
  │                  ├──▶ failed      (generator error, bad output, timeout, too many restarts)
  └─cancel─▶ cancelled ◀── cancel requested while running (process killed)
            running ──SIGTERM to worker──▶ queued again
```

- **Source of truth:** job state lives in Postgres (`agents.image_jobs`). `POST` also pushes
  the job ID onto a Redis list (`IMAGE_QUEUE_KEY`), but that only wakes the worker. The worker
  claims the oldest queued job with `FOR UPDATE SKIP LOCKED` and re-checks the table every
  5 s, so a lost Redis message only adds delay.
- **One at a time:** the worker processes jobs strictly in sequence. A Redis lock (`aureon:imaging:worker-lock`) stops a second worker instance from starting.
- **Queue cap:** if `IMAGE_QUEUE_MAX` (5) jobs are already `queued`, a new request gets a 429
  with `Retry-After: IMAGE_RETRY_AFTER` (600 s). A Postgres advisory lock means two requests at
  once can't both slip under the cap.
- **Restarts:**
  - On SIGTERM the running job is stopped and put back in the queue (`docker compose stop` allows 30 s).
  - After a crash, jobs left `running` are re-queued at the next start.
  - A job that has already started `IMAGE_MAX_ATTEMPTS` (2) times is marked `failed` instead.
- **Timeout:** a job is killed and marked `failed` after `IMAGE_JOB_TIMEOUT_SECONDS` (1800 s).
- **Progress:** parsed from the entrypoint's output (`12/25`-style tqdm or diffusers counters, or `48%`) and saved every `IMAGE_POLL_SECONDS`.

## Files

- **Location:** `/mnt/spasta-data/images/<user_id>/<job_id>.png`, built only from UUIDs.
- **Writing:** the generator writes a hidden temp file in that directory. The worker checks the PNG
  signature and dimensions, records the SHA-256, then renames it into place.
- **Serving:** the API resolves the stored path and serves it only if it stays inside
  `DATA_ROOT/images` and ends in `.png`. `..`, absolute paths and symlinks pointing outside are refused with 404.
- **HDD check:** the worker refuses to start, and `POST /generate` returns 503, unless the HDD marker
  file exists (`DATA_ROOT_MARKER`, see Phase 1).
- **Permissions:** containers run as UID 10001, so `/mnt/spasta-data/images` must be writable by it (see Deploy).

## Wrapping `/opt/image-agent`

The worker runs the existing entrypoint as a child process. **It never uses a shell**:
`IMAGE_AGENT_COMMAND` is a JSON argv template, and each placeholder fills exactly one argument.
A prompt like `"; rm -rf /"` therefore reaches the script as plain text.

```bash
# .env — example only; replace with the real script and flags from the inspection
IMAGE_GENERATOR=subprocess
IMAGE_AGENT_PATH=/opt/image-agent
IMAGE_AGENT_COMMAND=["python", "/opt/image-agent/generate.py", "--prompt", "{prompt}", "--negative", "{negative_prompt}", "--seed", "{seed}", "--steps", "{steps}", "--width", "{width}", "--height", "{height}", "--output", "{output}"]
```

- **Placeholders:** `{prompt}` `{negative_prompt}` `{seed}` `{steps}` `{width}` `{height}` `{output}`.
  `{output}` is required. An unknown placeholder stops the worker at startup.
- **Contract:** the entrypoint must exit 0 and write a PNG to `{output}`. A non-zero exit fails the job,
  and the error message includes the last output lines.
- **Environment:** the child gets a minimal environment: `PATH`, `HOME`, locale, and
  `OMP_NUM_THREADS`/`MKL_NUM_THREADS` = `IMAGE_THREADS` (3). Database URLs and keys are never passed.
  Names listed in `IMAGE_AGENT_ENV_PASSTHROUGH` (for example `HF_HOME`) are copied from the worker's
  environment.
- **Cancel and timeout:** the child runs in its own process group. Cancelling sends SIGTERM to the
  group, then SIGKILL after 5 s.
- **Models:** none are downloaded. The pipeline uses whatever `/opt/image-agent` already has, mounted read-only.

### Where the worker runs

The worker needs the pipeline's Python dependencies (torch, diffusers and so on), which the API
image doesn't have. Postgres and Redis are reachable only on the private Docker networks,
because their ports are never published. So the worker runs as its own compose service,
`imaging-worker` (profile `imaging`), built from the Dockerfile's `imaging` target:

- **Dependencies:** the target pip-installs `/opt/image-agent/<IMAGE_AGENT_REQUIREMENTS>` (default
  `requirements.txt`) using the PyTorch CPU wheel index. Those are build-time package downloads,
  not models or runtime APIs.
- **Code and models:** `/opt/image-agent` is bind-mounted read-only at the same path.
- **Limits:** `cpus: 3.0` and `mem_limit: 10g` leave a core for Postgres, the API and Ollama. Raise
  the memory limit if SD 1.5 needs more.

**Open questions for the inspection:** the entrypoint and its flags, whether it prints progress,
the requirements file name and Python version, and whether it writes caches inside `/opt/image-agent`.
That directory is mounted read-only, so point caches at `/tmp` or a path on the HDD via
`IMAGE_AGENT_ENV_PASSTHROUGH`.

## Tables (migration `0004`, additive)

| Table | Key columns |
| --- | --- |
| `agents.image_jobs` | `id` UUID, `user_id` → `core.users`, `status` (CHECK), `cancel_requested`, `progress` 0–100, `prompt`, `negative_prompt`, `seed`, `steps`, `width`, `height`, `model`, `error`, `attempts`, timestamps; indexes on (`status`, `created_at`) and (`user_id`, `created_at`) |
| `agents.generated_assets` | `id` UUID, `job_id` (unique) → `image_jobs`, `user_id` → `core.users`, `path` (relative to `DATA_ROOT`), `width`, `height`, `size_bytes`, `sha256`, `metadata` JSONB (prompt, seed, steps, model, seconds) |

The downgrade to `0003` drops both tables. It drops the `agents` schema only if nothing else is in it,
because Phase 5 adds more tables there.

## Deploy and validate

```bash
cd /opt/private-ai/aureon && git pull
docker compose build api && docker compose run --rm migrate          # applies 0004
docker compose up -d api

# Image output dir on the HDD, writable by the container user (UID 10001).
mkdir -p /mnt/spasta-data/images
ls -ld /mnt/spasta-data/images          # if not writable by 10001, ask before using sudo:
# sudo chown 10001:10001 /mnt/spasta-data/images

# After setting IMAGE_AGENT_COMMAND (and IMAGE_AGENT_REQUIREMENTS) in .env:
docker compose --profile imaging build imaging-worker
docker compose --profile imaging up -d imaging-worker
docker compose logs -f imaging-worker        # expect "worker_started", then "recovered"
```

Smoke test, reusing `J`, `B` and `CSRF` from the auth test in [deploy/README.md](../deploy/README.md):

```bash
JOB=$(curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' \
  -d '{"prompt":"a red bicycle, studio photo","steps":20}' $B/api/images/generate \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
watch -n 15 "curl -sS -b $J $B/api/images/jobs/$JOB"       # queued → running (progress) → done
curl -sS -b $J $B/api/images | python3 -m json.tool
curl -sS -b $J -o /tmp/test.png $B/api/images/<asset_id>/file && file /tmp/test.png
```

Checks:

- `docker compose stop imaging-worker` during a run puts the job back to `queued`, and it runs again after `up -d`.
- `POST /jobs/$JOB/cancel` during a run gives `cancelled` within a few seconds, and `top` shows the SD process gone.
- `ss -tlnp` still shows only `127.0.0.1:8000`, because the worker publishes no ports.
