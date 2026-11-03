# 03 — Ollama integration

**Status:** done (revision `0003`). It talks only to the existing Ollama container over the private
Docker network (`OLLAMA_BASE_URL`). Ollama is the default and the fallback; OpenRouter is an
opt-in, filtered alternative per conversation ([25-openrouter.md](25-openrouter.md)). Every endpoint below
requires a session cookie. `POST` and `DELETE` also require `X-CSRF-Token`
(see [02-auth-and-finance.md](02-auth-and-finance.md)).

## Endpoints

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/models` | 200 installed models, from `/api/tags` cached for 60 s · 503 if Ollama is unreachable |
| GET | `/api/models/status` | 200 `{reachable, models[{name, size, loaded, size_vram, expires_at, last_used_at}]}`; still 200 when Ollama is down |
| POST | `/api/chat` | 200 `text/event-stream` · 404 unknown model or conversation · 422 bad body · 429 queue full · 503 Ollama down |
| GET | `/api/conversations?limit=20&offset=0` | 200 `{items, total, limit, offset}`, newest first, only your own |
| GET | `/api/conversations/{id}` | 200 conversation and all messages · 404 if missing or not yours |
| DELETE | `/api/conversations/{id}` | 204, its messages are deleted with it · 404 if missing or not yours |

`loaded` comes from Ollama's `/api/ps`. `last_used_at` is when this API last finished a chat
with that model, kept in `ai.model_inventory`; Ollama doesn't record it.

## POST /api/chat

```json
{
  "conversation_id": null,
  "model": "qwen2.5:7b",
  "messages": [{"role": "user", "content": "Summarise my October expenses"}],
  "temperature": 0.7,
  "num_ctx": 4096,
  "max_tokens": 512
}
```

- **`messages`** holds only the *new* turns, with roles `system` or `user`, and the last one must be
  `user`. With a `conversation_id`, the server prepends the stored history before calling
  Ollama. Failed or cancelled assistant replies stay in the history but are not re-sent.
- **`conversation_id`** may be omitted to start a new conversation. Its title is the first 80
  characters of the first user message.
- **`num_ctx`** defaults to `CHAT_DEFAULT_NUM_CTX` (4096). Above `CHAT_MAX_NUM_CTX` (8192) the request gets a 422.
- **History:** with `CHAT_HISTORY_MAX_MESSAGES=N` only the system messages and the newest N
  stored turns are re-sent (the conversation is still stored in full). 0 = everything.

### Speed on a CPU-only box

Generation speed is set by the model size; time to the first token by the model load (cold)
and the prompt length. The server's settings (Ryzen 3, 4 cores, no GPU):

| Setting | Where | Value | Why |
| --- | --- | --- | --- |
| `CHAT_DEFAULT_MODEL` | `.env` | `qwen2.5:3b` | ~3x the tokens/s of a 7B model; listed first, preselected by the SPA |
| `CHAT_WARMUP` | `.env` | `true` | loads the default model at API start (background), so the first chat is warm |
| `CHAT_DEFAULT_NUM_CTX` | `.env` | `2048` | smaller KV cache, less prompt processing |
| `CHAT_NUM_THREAD` | `.env` | `4` | one thread per core |
| `CHAT_HISTORY_MAX_MESSAGES` | `.env` | `12` | bounded prompt per turn |
| `OLLAMA_KEEP_ALIVE` | `.env` and the Ollama container | `24h` | the model stays loaded |
| `OLLAMA_MAX_LOADED_MODELS` / `OLLAMA_NUM_PARALLEL` | Ollama container | `1` / `1` | one resident model, one KV cache |

The warm-up loads with the same `num_ctx`/`num_thread` as chats (a different `num_ctx` makes
Ollama reload the model). Another model requested by a worker (coding, research, voice)
replaces the chat model while `OLLAMA_MAX_LOADED_MODELS=1`; the next chat reloads it.
- **`max_tokens`** maps to Ollama's `num_predict`.
- The model must appear in `/api/tags`. Otherwise the response is a 404 with an `available` list
  and nothing is stored.

### Stream events

Each event is `event: <name>` followed by a one-line JSON `data:`. Lines starting with `:` are
keep-alive comments: ignore them.

| Event | Data | When |
| --- | --- | --- |
| `meta` | `{conversation_id, user_message_ids, model, provider, …route}` | First, always (route fields: [25-openrouter.md](25-openrouter.md#stream-events)) |
| `queued` | `{}` | Another generation holds the slot, so this request waits |
| `start` | `{}` | The slot was acquired and Ollama (or OpenRouter) was called |
| `fallback` | `{provider, model, reason, local_only, reset}` | OpenRouter failed; local Ollama answers instead (OpenRouter routes only) |
| `token` | `{content}` | Each streamed piece of the reply |
| `done` | `{message_id, prompt_tokens, completion_tokens, cost_usd, …route}` | Finished normally |
| `error` | `{message_id, code, message, …route}` | Ended early. `code` is `model_error`, `model_not_found`, `upstream_unavailable`, `timeout`, `queue_full` or `fallback_unavailable` |
| `: ping` | none | After `CHAT_HEARTBEAT_SECONDS` (15 s) of silence, for example during a cold model load |

The stream always ends with exactly one `done` or `error`. Either way, the assistant message
is already stored with status `complete`, `error` or `timeout` and whatever text arrived.

### Cancellation and timeouts

- **Client disconnects** (tab closed, `AbortController.abort()`): the server notices on the next
  event or ping, closes the HTTP stream to Ollama (which stops generating), frees the slot and
  saves the partial reply with status `cancelled`.
- **Server timeout:** `CHAT_TIMEOUT_SECONDS` (900 s) caps the whole request, including time
  spent queued, and ends with `error` code `timeout`.
- **Ollama goes silent:** `OLLAMA_READ_TIMEOUT` (300 s) is the longest allowed gap between chunks.

### Concurrency

At most `OLLAMA_MAX_CONCURRENT` (1) generations run at once, and up to `OLLAMA_MAX_QUEUE` (3)
more wait for a slot. Beyond that, the API returns 429 with `Retry-After: OLLAMA_QUEUE_RETRY_AFTER`
(15 s) before storing anything. The limit is per API process, so the API runs a single uvicorn
worker. Open WebUI talks to Ollama separately and is not counted.

### Vue client sketch

`EventSource` can't POST, so read the stream with `fetch`:

```ts
const ctrl = new AbortController()
const res = await fetch('/api/chat', {
  method: 'POST', credentials: 'include', signal: ctrl.signal,
  headers: { 'content-type': 'application/json', 'x-csrf-token': csrf },
  body: JSON.stringify({ conversation_id, model, messages: [{ role: 'user', content }] }),
})
if (res.status === 429) retryIn(Number(res.headers.get('retry-after')))
const reader = res.body!.pipeThrough(new TextDecoderStream()).getReader()
let buf = ''
for (;;) {
  const { value, done } = await reader.read()
  if (done) break
  buf += value
  let i
  while ((i = buf.indexOf('\n\n')) >= 0) {
    const block = buf.slice(0, i); buf = buf.slice(i + 2)
    if (block.startsWith(':')) continue                       // keep-alive
    const ev = /^event: (.+)$/m.exec(block)?.[1]
    const data = JSON.parse(/^data: (.+)$/m.exec(block)?.[1] ?? '{}')
    if (ev === 'token') append(data.content)
    else if (ev === 'meta') conversation_id = data.conversation_id
    else if (ev === 'error') showError(data.code, data.message)
  }
}
// Stop button: ctrl.abort() → server saves the partial reply as "cancelled".
```

## Tables (migration `0003`, additive, new schema `ai`)

| Table | Key columns |
| --- | --- |
| `ai.conversations` | `id` UUID, `user_id` → `core.users` (on delete cascade), `title`, `model`, `created_at`, `updated_at`; index on (`user_id`, `updated_at`) |
| `ai.messages` | `id` bigint, `conversation_id` → conversations (on delete cascade), `role` (CHECK system, user or assistant), `content`, `model`, `status` (CHECK complete, cancelled, error or timeout), `error`, `prompt_tokens`, `completion_tokens`, `duration_ms` |
| `ai.model_inventory` | `name` PK, `digest`, `size_bytes`, `family`, `parameter_size`, `quantization`, `modified_at`, `first_seen_at`, `last_seen_at`, `last_used_at` |

Messages belong to a user only through their conversation. Every query joins or filters
on `conversations.user_id` in `ConversationService`. Another user's conversation ID returns
404, never 403, so a client can't tell whether that ID exists.

## Deploy and validate

```bash
docker compose build && docker compose run --rm migrate      # applies 0003
docker compose up -d api
docker compose exec -T api python -c "import httpx,os;print(httpx.get(os.environ['OLLAMA_BASE_URL']+'/api/tags').status_code)"
```

Then, with the cookie jar `J`, base URL `B` and `CSRF` value from the auth smoke test in
[deploy/README.md](../deploy/README.md):

```bash
curl -sS -b $J $B/api/models | python3 -m json.tool
curl -sS -b $J $B/api/models/status | python3 -m json.tool
curl -N -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' \
  -d '{"model":"<tag from /api/models>","messages":[{"role":"user","content":"Say hi in 5 words"}]}' \
  $B/api/chat
```

Expect `event: meta`, `event: start`, a run of `event: token` lines and a final `event: done`.
On this CPU-only box the first token can take a minute or more while the model loads,
with `: ping` lines in between.
