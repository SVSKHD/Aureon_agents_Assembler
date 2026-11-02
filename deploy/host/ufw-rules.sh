#!/usr/bin/env bash
# Firewall for the Aureon box: allow only SSH, HTTP and HTTPS in; everything else denied.
# Run ONCE as root (docs/manual-setup.md, step 5). Review before running; it does not
# remove existing rules. Docker-published ports bypass ufw, which is why compose publishes
# only 127.0.0.1:8000 (never 0.0.0.0).
set -euo pipefail
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'ssh'
ufw allow 80/tcp comment 'http'
ufw allow 443/tcp comment 'https'
ufw --force enable
ufw status verbose
