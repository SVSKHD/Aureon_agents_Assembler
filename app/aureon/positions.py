"""Positions from MT5 deal history: `build_positions` from Aureon V1.6.3 aureon/mt5_history.py,
copied verbatim. The MT5 fetch (`fetch_deals`) stays on the Windows box: the Assembler only
receives deals through POST /api/v1/ingest/mt5_deals and never imports MetaTrader5."""
from collections import defaultdict
from . import config

DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
TRADE_TYPES = {0, 1}

def build_positions(deals, symbol=None):
    """Group deals by position_id -> completed/open positions. Netting (INOUT reversal) closes the prior id; hedging just pairs IN/OUT.
    An open position (remaining volume > 0) is returned with status 'open' and never counted as a completed trade."""
    by_pos = defaultdict(list)
    for d in deals:
        if d["type"] not in TRADE_TYPES: continue
        if symbol and d["symbol"] != symbol: continue
        by_pos[d["position_id"]].append(d)
    out = []
    for pid, ds in by_pos.items():
        ds.sort(key=lambda x: (x["time_utc"], x["deal_ticket"]))
        ins = [d for d in ds if d["entry"] == DEAL_ENTRY_IN]; outs = [d for d in ds if d["entry"] in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY, DEAL_ENTRY_INOUT)]
        if not ins and not outs: continue
        vin = sum(d["volume"] for d in ins); vout = sum(d["volume"] for d in outs)
        direction = (1 if ins[0]["type"] == 0 else -1) if ins else (-1 if outs[0]["type"] == 0 else 1)
        entry_px = sum(d["price"] * d["volume"] for d in ins) / vin if vin else None
        exit_px = sum(d["price"] * d["volume"] for d in outs) / vout if vout else None
        gross = sum(d["profit"] for d in outs); costs = sum(d["commission"] + d["swap"] + d["fee"] for d in ds)
        is_inout = any(d["entry"] == DEAL_ENTRY_INOUT for d in outs)
        remaining = 0.0 if is_inout else max(0.0, vin - vout)
        status = "open" if (remaining > 1e-9 or not outs) else ("partial_entry_missing" if not ins else "closed")
        comments = " ".join(d["comment"] for d in ds)
        origin = "aureon" if "aureon:" in comments or any(d["magic"] == config.AUREON_MAGIC for d in ds if config.AUREON_MAGIC) else "discretionary"
        out.append(dict(position_id=pid, account_id=ds[0]["account_id"], symbol=ds[0]["symbol"], dir=direction, entry_time=ins[0]["time_utc"] if ins else None,
                        exit_time=outs[-1]["time_utc"] if outs else None, entry_price=entry_px, exit_price=exit_px, volume_in=vin, volume_out=vout,
                        fills_in=len(ins), fills_out=len(outs), partial_close=(len(outs) > 1), gross_pnl=gross, costs=costs, net_pnl=gross + costs,
                        holding_min=((outs[-1]["time_utc"] - ins[0]["time_utc"]).total_seconds() / 60) if ins and outs else None,
                        status=status, origin=origin, request_ids=[c.split("aureon:")[1].split()[0] for c in [d["comment"] for d in ds] if "aureon:" in c],
                        order_tickets=sorted({d["order_ticket"] for d in ds}), deal_tickets=[d["deal_ticket"] for d in ds]))
    return sorted(out, key=lambda p: (p["entry_time"] or p["exit_time"]))
