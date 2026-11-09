"""The ONLY module that sends position-changing requests to MT5. Never AI-patched. Never opens positions.
Every request: symbol check, account-mode check, broker constraints (digits, volume step, stops/freeze level, filling mode),
retcode + executed-volume verification, and a re-check of position state before any follow-up request.
A timeout / None response is treated as UNKNOWN (caller reconciles), never as 'failed'."""
import time, logging, datetime as dt
from . import config
from .timeutil import now
log = logging.getLogger("orders")
REAL = 2; DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
# MT5 deal types: 0 BUY, 1 SELL, 2 BALANCE, 3 CREDIT, 4 CHARGE, 5 CORRECTION, 6 BONUS, 7 COMMISSION, 8 COMMISSION_DAILY, 9 COMMISSION_MONTHLY,
# 10 COMMISSION_AGENT_DAILY, 11 COMMISSION_AGENT_MONTHLY, 12 INTEREST, 13 BUY_CANCELED, 14 SELL_CANCELED, 15 DIVIDEND, 16 DIVIDEND_FRANKED, 17 TAX
DEAL_TRADE = {0, 1}; DEAL_REFERENCE_ADJ = {2, 3, 6}; DEAL_COST = {4, 5, 7, 8, 9, 10, 11, 12, 15, 16, 17}; DEAL_IGNORE = {13, 14}

def classify_deals(deals, day0_epoch):
    """Split today's deals into: realized trading P&L (profit+commission+swap of closing trade deals), separate cost/fee deals
    (count against the day), reference adjustments (deposits/withdrawals/credits/bonuses: move the baseline), and a completeness
    flag: history is 'suspect' if the window shows no deal at all before day0 although the account clearly existed (MT5 terminals
    can truncate history), in which case the reconstruction cannot be trusted."""
    realized = fees = ref_adj = 0.0
    for d in deals:
        if d.time < day0_epoch: continue
        if d.type in DEAL_TRADE:
            if d.entry in (DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY): realized += d.profit + d.commission + d.swap
            elif d.entry == DEAL_ENTRY_IN: realized += d.commission                       # entry commission is a cost already charged
        elif d.type in DEAL_REFERENCE_ADJ: ref_adj += d.profit
        elif d.type in DEAL_COST: fees += d.profit + d.commission + d.swap
    before = [d for d in deals if d.time < day0_epoch]
    return dict(realized_today=realized + fees, fees_today=fees, deposits_today=ref_adj, history_suspect=(len(before) == 0))
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

class OrderError(RuntimeError): pass
class MT5Unavailable(OrderError): pass
class ExecutionUnknown(OrderError):
    """Request was sent but the outcome could not be verified (timeout / None). Caller must reconcile, not retry blindly."""

def _mt5():
    import MetaTrader5 as mt5
    if not mt5.initialize(): raise MT5Unavailable(f"MT5 init failed: {mt5.last_error()}")
    if not mt5.symbol_select(config.SYMBOL, True): mt5.shutdown(); raise MT5Unavailable(f"symbol_select failed {config.SYMBOL}")
    return mt5

def _refuse_real(mt5):
    info = mt5.account_info()
    if info is None: raise MT5Unavailable("account_info None")
    if config.MT5_PAPER and info.trade_mode == REAL: raise OrderError("MT5_PAPER=true but account is REAL — refusing to modify orders")
    return info

def _sym(mt5):
    s = mt5.symbol_info(config.SYMBOL)
    if s is None: raise MT5Unavailable("symbol_info None")
    return s

def _position(mt5, ticket):
    pos = mt5.positions_get(ticket=ticket)
    if pos is None: raise MT5Unavailable(f"positions_get None: {mt5.last_error()}")
    if not pos: return None
    p = pos[0]
    if p.symbol != config.SYMBOL: raise OrderError(f"ticket {ticket} is {p.symbol}, not {config.SYMBOL}")
    return p

def _filling(mt5, s):
    fm = s.filling_mode
    if fm & 1: return mt5.ORDER_FILLING_FOK
    if fm & 2: return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN

def _round_volume(s, vol):
    step = s.volume_step or 0.01; v = round(round(vol / step) * step, 8)
    if v < s.volume_min or v > s.volume_max: raise OrderError(f"volume {v} outside [{s.volume_min},{s.volume_max}]")
    return v

def account_info():
    mt5 = _mt5()
    try:
        a = mt5.account_info()
        if a is None: raise MT5Unavailable("account_info None")
        return dict(login=a.login, balance=a.balance, equity=a.equity, trade_mode=a.trade_mode, hedging=(a.margin_mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING), currency=a.currency)
    finally: mt5.shutdown()

def open_positions(symbol_only=True):
    mt5 = _mt5()
    try:
        pos = mt5.positions_get(symbol=config.SYMBOL) if symbol_only else mt5.positions_get()
        if pos is None: raise MT5Unavailable(f"positions_get None: {mt5.last_error()}")
        return [dict(ticket=p.ticket, symbol=p.symbol, dir=1 if p.type == 0 else -1, lots=p.volume, price=p.price_open, sl=p.sl, tp=p.tp, time=p.time, profit=p.profit, swap=p.swap) for p in pos]
    finally: mt5.shutdown()

def account_snapshot(day_start_utc_naive):
    """Account-level figures for the risk engine. Every broker call must succeed or MT5Unavailable is raised."""
    mt5 = _mt5()
    try:
        a = mt5.account_info()
        if a is None: raise MT5Unavailable("account_info None")
        from .collector import to_mt5
        deals = mt5.history_deals_get(to_mt5(day_start_utc_naive - dt.timedelta(days=1)), to_mt5(now().replace(tzinfo=None) + dt.timedelta(minutes=5)))
        if deals is None: raise MT5Unavailable(f"history_deals_get None: {mt5.last_error()}")
        pos = mt5.positions_get()
        if pos is None: raise MT5Unavailable(f"positions_get None: {mt5.last_error()}")
        day0_epoch = to_mt5(day_start_utc_naive).replace(tzinfo=dt.timezone.utc).timestamp()
        cls = classify_deals(deals, day0_epoch)
        floating = sum(p.profit + p.swap for p in pos)
        # history_suspect only matters if something could have been open before day0: a fresh/idle account legitimately has no deals
        suspect = cls["history_suspect"] and (any(p.time < day0_epoch for p in pos) or a.balance != a.equity or any(d.time >= day0_epoch and d.entry in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY, DEAL_ENTRY_INOUT) for d in deals))
        return dict(login=str(a.login), equity=a.equity, balance=a.balance, floating=floating, realized_today=cls["realized_today"], fees_today=cls["fees_today"],
                    deposits_today=cls["deposits_today"], history_suspect=suspect,
                    other_symbol_positions=[p.symbol for p in pos if p.symbol != config.SYMBOL],
                    positions_open_at_day_start=any(p.time < day0_epoch for p in pos) or any(d.time >= day0_epoch and d.entry in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY, DEAL_ENTRY_INOUT) and _opened_before(deals, d, day0_epoch) for d in deals))
    finally: mt5.shutdown()

def _opened_before(deals, out_deal, t0):
    """True if the position this closing deal belongs to was opened before t0 (i.e. carried over the day boundary)."""
    ins = [d for d in deals if getattr(d, "position_id", None) == out_deal.position_id and d.entry == DEAL_ENTRY_IN]
    return bool(ins) and min(d.time for d in ins) < t0 or (not ins)        # IN deal not in today's window => opened earlier

def position_state(ticket):
    """Fresh broker read of one position: None if not open, else dict with current sl/tp/volume/dir. Raises MT5Unavailable."""
    mt5 = _mt5()
    try:
        p = _position(mt5, ticket)
        return None if p is None else dict(ticket=p.ticket, dir=1 if p.type == 0 else -1, volume=p.volume, price_open=p.price_open, sl=p.sl, tp=p.tp)
    finally: mt5.shutdown()

def current_price():
    mt5 = _mt5()
    try:
        t = mt5.symbol_info_tick(config.SYMBOL)
        if t is None: raise MT5Unavailable("no tick")
        return dict(bid=t.bid, ask=t.ask, time=t.time)
    finally: mt5.shutdown()

def validate_stop(mt5, s, p, sl):
    point, digits = s.point, s.digits; sl = round(sl, digits)
    tick = mt5.symbol_info_tick(config.SYMBOL)
    if tick is None: raise MT5Unavailable("no tick")
    mkt = tick.bid if p.type == 0 else tick.ask
    if (p.type == 0 and sl >= mkt) or (p.type == 1 and sl <= mkt): raise OrderError(f"SL {sl} on the wrong side of market {mkt}")
    min_dist = max(s.trade_stops_level, s.trade_freeze_level) * point
    if abs(mkt - sl) < min_dist: raise OrderError(f"SL {sl} inside broker stops/freeze level ({min_dist:.2f}) — market {mkt}")
    return sl

def modify_sl(ticket, sl, tp=None):
    """Returns the SL verified on the broker side. Never widens. Raises ExecutionUnknown if the result could not be read back."""
    mt5 = _mt5()
    try:
        _refuse_real(mt5); s = _sym(mt5); p = _position(mt5, ticket)
        if p is None: raise OrderError(f"ticket {ticket} not open")
        sl = validate_stop(mt5, s, p, sl); point = s.point
        if p.sl and ((p.type == 0 and sl < p.sl) or (p.type == 1 and sl > p.sl)): return p.sl
        if p.sl and abs(p.sl - sl) < point: return p.sl
        req = dict(action=mt5.TRADE_ACTION_SLTP, position=ticket, symbol=config.SYMBOL, sl=sl, tp=round(tp if tp else p.tp, s.digits))
        last = None
        for attempt in range(3):
            r = mt5.order_send(req); last = r
            if r is None: break                                                         # unknown outcome -> verify below
            if r.retcode == mt5.TRADE_RETCODE_DONE: break
            if r.retcode in (mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED, mt5.TRADE_RETCODE_CONNECTION, mt5.TRADE_RETCODE_TIMEOUT):
                time.sleep(1); continue
            raise OrderError(f"modify_sl retcode {r.retcode} {r.comment}")
        p2 = _position(mt5, ticket)
        if p2 is None: raise ExecutionUnknown(f"position {ticket} gone during SL modify")
        if abs(p2.sl - sl) > point:
            if last is None or getattr(last, "retcode", None) == mt5.TRADE_RETCODE_TIMEOUT: raise ExecutionUnknown(f"SL modify unverified: broker has {p2.sl}, wanted {sl}")
            raise OrderError(f"SL verify failed: wanted {sl}, broker has {p2.sl}")
        return p2.sl
    finally: mt5.shutdown()

def close(ticket, request_id):
    """Closes the WHOLE position. request_id (stable, from the caller's trade row) is written into the order comment so a
    repeated attempt can be matched in history. Re-checks the position before every send; a None/timeout response raises
    ExecutionUnknown (the caller moves the trade to 'closing' and reconciles) — it is never retried blindly."""
    mt5 = _mt5()
    try:
        _refuse_real(mt5); s = _sym(mt5)
        for attempt in range(3):
            p = _position(mt5, ticket)
            if p is None:
                d = deal_result(ticket, mt5)
                if d and d["complete"]: return d
                raise ExecutionUnknown(f"ticket {ticket} not open but closing deals incomplete/missing")
            tick = mt5.symbol_info_tick(config.SYMBOL)
            if tick is None: raise MT5Unavailable("no tick")
            vol = _round_volume(s, p.volume)
            req = dict(action=mt5.TRADE_ACTION_DEAL, position=ticket, symbol=config.SYMBOL, volume=vol,
                       type=mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY, price=tick.bid if p.type == 0 else tick.ask,
                       deviation=30, comment=f"aureon:{request_id}"[:31], type_filling=_filling(mt5, s))
            r = mt5.order_send(req)
            if r is None or r.retcode == mt5.TRADE_RETCODE_TIMEOUT: raise ExecutionUnknown("close order_send returned None/timeout — outcome unknown")
            if r.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
                time.sleep(1); d = deal_result(ticket, mt5)
                if d and d["complete"]: return d
                if _position(mt5, ticket) is None: raise ExecutionUnknown("position gone but deal history incomplete")
                continue                                                                # partial: close the remainder (re-reads position)
            if r.retcode in (mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED, mt5.TRADE_RETCODE_CONNECTION, mt5.TRADE_RETCODE_PRICE_OFF):
                time.sleep(1); continue
            raise OrderError(f"close retcode {r.retcode} {r.comment}")
        raise ExecutionUnknown("close: retries exhausted without complete evidence")
    finally: mt5.shutdown()

def deal_result(ticket, mt5=None):
    """Netting-aware reconciliation of one position id. complete=True only when remaining volume is zero."""
    own = mt5 is None
    if own: mt5 = _mt5()
    try:
        deals = mt5.history_deals_get(position=ticket)
        if deals is None: raise MT5Unavailable(f"history_deals_get None: {mt5.last_error()}")
        deals = sorted(deals, key=lambda d: (d.time, getattr(d, "ticket", 0)))
        remaining = 0.0; outs = []
        for d in deals:
            if d.entry == DEAL_ENTRY_IN: remaining += d.volume
            elif d.entry in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY): remaining -= d.volume; outs.append(d)
            elif d.entry == DEAL_ENTRY_INOUT: remaining = 0.0; outs.append(d)
        if not outs: return None
        vout = sum(d.volume for d in outs) or 1e-9
        return dict(price=sum(d.price * d.volume for d in outs) / vout, pnl=sum(d.profit for d in outs), commission=sum(d.commission for d in deals),
                    swap=sum(d.swap for d in deals), time=max(d.time for d in outs), complete=(remaining <= 1e-6), closed_volume=vout)
    finally:
        if own: mt5.shutdown()
