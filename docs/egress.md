# Outbound allowlist (egress)

The AI stack is Claude-only (docs/26-ai-providers.md): the only outbound AI traffic is HTTPS to
`api.anthropic.com`, and only `app/ai/gateway.py` makes it (a test enforces that). This page
lists every outbound host Aureon uses, so the server can deny everything else.

**Nothing here is applied automatically.** Review it, then apply it yourself with `ufw` or
`iptables`. Apply it from the console or with a rollback timer (below), so a mistake cannot
lock you out of SSH.

## What must stay reachable

| Group | Host (port 443 unless noted) | Used for | Needed? |
|---|---|---|---|
| AI | `api.anthropic.com` | Claude: chat, agents, reports, research, voice | yes |
| Broker | `api.dhan.co` | Dhan REST (read-only) | yes |
| Broker | `api-feed.dhan.co` | Dhan live feed (WebSocket over 443) | yes |
| Broker | `auth.dhan.co` | Dhan login | yes |
| Broker | `images.dhan.co` | Dhan instrument master CSV | yes |
| Broker | the MT5 broker's servers | MT5 bot (runs on the Windows box, not here) | not on this server |
| Ops | `api.github.com`, `github.com`, `codeload.github.com`, `objects.githubusercontent.com` | GitHub ops, deploys (`git fetch`), Actions logs | yes, unless you stop using them |
| Ops | `discord.com` | Discord alerts | only if `DISCORD_WEBHOOK_URL` is set |
| Ops | the hosts in `NEWS_FEED_URLS` | news pull | only if set |
| Access | Cloudflare Tunnel: `region1.v2.argotunnel.com`, `region2.v2.argotunnel.com` (TCP/UDP **7844**), `api.cloudflare.com`, `*.cftunnel.com` | spasta.online / api.spasta.online | yes (or the site goes offline) |
| Build | `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com`, `pypi.org`, `files.pythonhosted.org` | `docker compose build` | only while building; open temporarily |
| System | DNS (UDP/TCP 53) to your resolver, NTP (UDP 123), Ubuntu mirrors (80/443) | name lookups, clock, `apt` | yes / yes / while updating |

Not needed any more: Gemini (`generativelanguage.googleapis.com`), OpenRouter
(`openrouter.ai`) and the local Ollama container's port (the AI stack no longer calls it).

The Claude Code CLI (PR reviews, docs/22) also talks to `api.anthropic.com` (and
`statsig.anthropic.com` / `sentry.io` for its telemetry unless you disable it with
`DISABLE_TELEMETRY=1` and `DISABLE_ERROR_REPORTING=1` in its environment).

## Hostnames vs firewall rules

`ufw` and `iptables` match IP addresses, not names, and these services sit behind CDNs whose
addresses change. Two workable options:

1. **Recommended: an egress proxy.** Run a small forward proxy (e.g. Squid or tinyproxy) that
   allows only the hostnames above (SNI / CONNECT host), set `HTTPS_PROXY` for the Aureon
   containers, and let the firewall allow outbound 443 **only from the proxy**.
2. **IP sets refreshed by a timer.** Resolve the hostnames every few minutes into an `ipset`
   and allow 443 to that set. Simpler, but a CDN address change between refreshes drops
   traffic until the next run.

## Option 2: commands (review before running)

Docker writes its own iptables rules and bypasses `ufw` for container traffic, so container
egress has to be filtered in the `DOCKER-USER` chain.

```bash
# 0. Safety net: undo everything in 10 minutes unless you cancel it.
sudo sh -c 'sleep 600 && iptables -F DOCKER-USER && iptables -A DOCKER-USER -j RETURN && ufw --force disable' &
ROLLBACK_PID=$!

# 1. The allowlist as an ipset, refreshed from DNS.
sudo apt-get install -y ipset dnsutils
sudo ipset create aureon-egress hash:ip -exist
cat <<'EOF' | sudo tee /usr/local/sbin/aureon-egress-refresh
#!/bin/sh
set -eu
HOSTS="api.anthropic.com api.dhan.co api-feed.dhan.co auth.dhan.co images.dhan.co
api.github.com github.com codeload.github.com objects.githubusercontent.com discord.com
api.cloudflare.com region1.v2.argotunnel.com region2.v2.argotunnel.com"
ipset create aureon-egress-new hash:ip -exist
ipset flush aureon-egress-new
for h in $HOSTS; do
  for ip in $(dig +short A "$h" | grep -E '^[0-9.]+$'); do ipset add aureon-egress-new "$ip" -exist; done
done
ipset swap aureon-egress-new aureon-egress
ipset destroy aureon-egress-new
EOF
sudo chmod 755 /usr/local/sbin/aureon-egress-refresh
sudo /usr/local/sbin/aureon-egress-refresh
echo '*/5 * * * * root /usr/local/sbin/aureon-egress-refresh' | sudo tee /etc/cron.d/aureon-egress

# 2. Containers (DOCKER-USER): replies, DNS, the allowlist; drop other new outbound traffic.
sudo iptables -I DOCKER-USER 1 -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
sudo iptables -I DOCKER-USER 2 -p udp --dport 53 -j RETURN
sudo iptables -I DOCKER-USER 3 -p tcp --dport 53 -j RETURN
sudo iptables -I DOCKER-USER 4 -p tcp --dport 443 -m set --match-set aureon-egress dst -j RETURN
sudo iptables -I DOCKER-USER 5 -d 10.0.0.0/8 -j RETURN       # container-to-container
sudo iptables -I DOCKER-USER 6 -d 172.16.0.0/12 -j RETURN
sudo iptables -I DOCKER-USER 7 -d 192.168.0.0/16 -j RETURN
sudo iptables -I DOCKER-USER 8 -o eth0 -m conntrack --ctstate NEW -j DROP   # eth0 = your WAN NIC

# 3. The host itself (cloudflared, git, apt): ufw default-deny outbound with the same rules.
sudo ufw default deny outgoing
sudo ufw allow out 53
sudo ufw allow out 123/udp
sudo ufw allow out 7844                       # Cloudflare Tunnel (TCP and UDP)
sudo ufw allow out to any port 443 proto tcp  # narrow this to the proxy in option 1
sudo ufw allow out 80/tcp                     # apt mirrors; remove if you use HTTPS mirrors
sudo ufw allow in 22/tcp                      # keep SSH
sudo ufw enable

# 4. Check, then cancel the rollback.
curl -sS -o /dev/null -w '%{http_code}\n' https://api.anthropic.com/v1/models   # 401 = reachable
docker compose exec api python -c "import urllib.request as u; u.urlopen('https://openrouter.ai', timeout=5)"  # must fail
kill "$ROLLBACK_PID"
```

`ufw` cannot match hostnames, so step 3 still allows any 443 from the host; the container
rule in step 2 is the one that enforces the allowlist for Aureon. Use option 1 for a
host-wide hostname allowlist. Persist the iptables rules with `iptables-persistent`
(`sudo netfilter-persistent save`) once you are happy with them.

## When you add a connection

Aquakart and Pawsattva are not connected yet (their tools are hidden from Claude).
Connecting one adds its API host here. Todos, tasks and reminders need no egress: the app sends
a read-only summary with each chat message, and saves Claude's suggestions itself after the
user confirms them (docs/26-ai-providers.md, "The workspace").
