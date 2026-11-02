#!/usr/bin/env python3
"""Static-IP preflight for DhanHQ.

Dhan requires orders to come from a static IP that is whitelisted on the Dhan account. This
script only REPORTS. It never changes network settings and never tries to get around that
requirement:

- the current public egress IP, asked of two independent echo services, several times
- whether the IP stayed the same across samples
- whether it matches DHAN_STATIC_IP (the IP you whitelisted), if that is set

Exit code: 0 if stable and matching DHAN_STATIC_IP, 1 if not, 2 if the IP couldn't be read.

    python scripts/dhan_preflight.py [--samples 3] [--interval 5] [--json]

Note for this server: JioFiber home connections normally get a dynamic, often CGNAT, IP, and a
Cloudflare Tunnel only carries INBOUND traffic. Neither gives a whitelistable static egress IP.
Live order placement (Trading Phase G) needs a real static IP (an ISP add-on or a static-IP
egress you control) and must pass this preflight first. Read-only data APIs don't need it.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
import urllib.request

ECHO_SERVICES = ("https://api.ipify.org", "https://ifconfig.me/ip")
NOTE = (
    "JioFiber (dynamic/CGNAT) + Cloudflare Tunnel (inbound only) do NOT provide a "
    "whitelistable static egress IP. Read-only data does not need one; orders do."
)


def fetch(url: str, timeout: float = 5.0) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": "aureon-dhan-preflight/1"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URLs
            ip = resp.read(64).decode().strip()
        ipaddress.ip_address(ip)
        return ip
    except (OSError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    seen: list[dict[str, str | None]] = []
    for i in range(max(1, args.samples)):
        seen.append({url: fetch(url) for url in ECHO_SERVICES})
        if i + 1 < args.samples:
            time.sleep(args.interval)

    ips = {ip for sample in seen for ip in sample.values() if ip}
    expected = os.environ.get("DHAN_STATIC_IP", "").strip() or None
    report: dict[str, object] = {
        "samples": seen,
        "egress_ips": sorted(ips),
        "stable": len(ips) == 1,
        "expected_static_ip": expected,
        "matches_expected": bool(expected) and ips == {expected},
        "note": NOTE,
    }
    if ips:
        ip = ipaddress.ip_address(next(iter(ips)))
        # 100.64.0.0/10 is carrier-grade NAT: definitely not whitelistable.
        report["carrier_grade_nat"] = ip in ipaddress.ip_network("100.64.0.0/10")
        report["is_global"] = ip.is_global

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"egress IP(s): {', '.join(sorted(ips)) or 'unknown'}")
        print(f"stable across {len(seen)} samples: {report['stable']}")
        print(f"DHAN_STATIC_IP: {expected or '(not set)'} -> match: {report['matches_expected']}")
        print(f"note: {NOTE}")

    if not ips:
        return 2
    return 0 if report["stable"] and report["matches_expected"] else 1


if __name__ == "__main__":
    sys.exit(main())
