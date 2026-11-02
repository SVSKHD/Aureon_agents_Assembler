# 00 — Server inspection (pending: run on the server)

Phase 1 was built in a cloud container with no access to the server (192.168.29.95).
None of the values below have been checked yet. Run these read-only commands as `svskhd8`,
paste the output into the matching section, then fill in `.env`.
Phase 2 (auth + finance) is **blocked** until §4 (real DDL) is filled in.

## 1. Docker networks and containers (blocks Phase 1 deploy)

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Networks}}'
docker network ls
ls /opt/private-ai; cat /opt/private-ai/*compose*.y*ml
```

Record:

| Item | Value | Used in |
| --- | --- | --- |
| Postgres container name | _TBD_ | `DATABASE_URL` host |
| Ollama container name | _TBD_ | `OLLAMA_BASE_URL` host |
| Network shared by both | _TBD_ | `PRIVATE_AI_NETWORK` |
| Postgres published on host? | _TBD_ (must be "no") | Hard rule 5 |
| Ollama published on host? | _TBD_ (must be "no") | Hard rule 5 |
| Open WebUI container / port | _TBD_ | keep running |

## 2. Ollama models

```bash
docker exec <ollama-container> ollama list
```

## 3. HDD mount

```bash
findmnt /mnt/spasta-data
df -h /mnt/spasta-data
ls -la /mnt/spasta-data
```

The API and compose gate on a marker file **on the HDD**. Create it once while the disk is mounted:

```bash
touch /mnt/spasta-data/.spasta-data-mounted
```

## 4. Existing finance schema (blocks Phase 2)

Read-only dump; does not modify anything:

```bash
docker exec <postgres-container> pg_dump -U <user> -d spasta --schema-only \
  -t public.astra_documents -t public.trades -t public.expenses \
  -t public.secured -t public.portfolio_profiles
docker exec <postgres-container> psql -U <user> -d spasta -c '\df+ astra_uid'
docker exec <postgres-container> psql -U <user> -d spasta -c '\dt *.*alembic*'
```

Note for each table: PK (composite?), `astra_uid()` defaults, JSONB columns, session values
(Asia/London/NY), buy/sell and recurrence fields. If the last command shows an existing
`alembic_version`, that is fine: this app uses its own `public.aureon_alembic_version`.

## 5. Image agent and tunnel

```bash
ls -la /opt/image-agent
systemctl cat cloudflared
sudo cat /etc/cloudflared/config.yml   # if readable
```

## 6. Repos (Prompt 1 §2–3)

ASTRA-FRONTEND's Supabase/Firebase/Netlify dependencies and the Aureon_agentic_bot interface
are not mapped yet. They are needed before Phase 6 and Trading Phase C, not before Phase 1.
