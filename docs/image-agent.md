# Image agent (local Stable Diffusion 1.5)

Images are generated on this server only, by the `imaging-worker` container (compose profile
`imaging`). Nothing goes to a cloud service unless `OPENROUTER_IMAGE_MODEL` is set; it is
off by default and never used for personal or finance prompts (docs/model-routing.md).

## Engine and models

- **Engine**: the existing image agent's [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) build, `/opt/image-agent/sdcpp/src/build/bin/sd-cli`, mounted read-only into the worker. It's CPU-only and needs no Python packages, so the image is built with `IMAGE_AGENT_PIP=false`. The worker image adds the OpenMP runtime, `libgomp1`.
- **Models**: already on the HDD. Nothing is downloaded.

| Name (`IMAGE_MODELS`) | File | Steps / CFG / sampler | Time per 512×512 on this CPU |
|---|---|---|---|
| `dreamshaper8-lcm` (default) | `checkpoints/dreamshaper8_lcm.q8_0.gguf` (1.8 GB) | 4 / 1.5 / `lcm` | about 4 min (measured 3 min 45 s while the CPU was shared) |
| `dreamshaper8` | `checkpoints/dreamshaper8.q8_0.gguf` (1.8 GB) | 20 / 7.0 / `euler_a` | roughly 15–20 min |

Every job decodes through TAESD (`vae/taesd.safetensors`), the tiny autoencoder. On this CPU
that took the decode from 119 s to 7 s, with slightly softer detail. The safetensors copies
of both checkpoints and the LCM LoRA are on disk too, but unused.

- `GET /api/images/models` lists the models with their default steps.
- `POST /api/images/generate` takes `"model": "<name>"`, and the default is `IMAGE_MODEL_NAME`.
- The job's model fills the command's `{model_path}`, `{cfg}` and `{sampler}` placeholders.

## Job lifecycle

The job lifecycle is unchanged from Phase 4 (docs/04-imaging.md):

1. `POST /api/images/generate` → 202 with the job (`queued`). The queue is capped at `IMAGE_QUEUE_MAX` (429 beyond it).
2. The worker runs one job at a time (`cpus: 3.0`, `mem_limit: 10g`). Progress comes from sd-cli's `n/N` steps. `POST /api/images/jobs/{id}/cancel` stops a queued or running job.
3. The PNG is written to `/mnt/spasta-data/images/<user>/…` and appears in `GET /api/images`, the gallery. `GET /api/images/{id}/file` serves it, and `DELETE /api/images/{id}` removes it.
4. A shutdown re-queues a running job, and a crashed one is recovered at the next start.

## Yielding to interactive work and trading windows

- **Chat and voice first.** While a chat (and, with the voice agent, a voice request) is running, the API keeps a mark in Redis (`app/services/interactive.py`, `aureon:ai:interactive`). Each mark expires on its own, so a crashed request can't hold images forever. While any mark exists, the worker sends the running generation `SIGSTOP`: it keeps its memory and resumes exactly where it was (`SIGCONT`) when the request ends. No new job starts in the meantime. Turn this off with `IMAGE_PAUSE_FOR_INTERACTIVE=false`.
- **Trading windows.** No new job starts inside `IMAGE_PAUSE_WINDOWS` (Asia/Kolkata, Monday to Friday; default `09:00-09:35,15:00-15:35`: the open and the close). A running job finishes.
- **Visible in the agent registry.** The `imaging` agent's heartbeat shows `holding` (`interactive` or `market_window`) and `suspended`.

## Starting it

```sh
cd /opt/private-ai/aureon
# .env: IMAGE_AGENT_COMMAND / IMAGE_MODELS / IMAGE_MODEL_NAME / IMAGE_AGENT_PIP=false (see .env.example)
# and COMPOSE_PROFILES=coding,imaging so deploys rebuild and update the worker too.
docker compose --profile imaging up -d --build --no-deps imaging-worker
docker compose logs -f imaging-worker
```

No sudo is needed.
