"""Aureon V1.6.3 integration (docs/34-aureon-v163.md).

The Assembler is the database, background analysis, job queue, reporting and coordination
side of Aureon V1.6.3. The Windows box keeps MT5: the collector uploads bars and broker deal
history and runs the jobs it claims; the guard owns every open position, its stops and exits.

Execution boundary (enforced by tests): no module here imports MetaTrader5, sends an order,
modifies a stop or closes a position, and nothing the Windows box needs to protect a position
depends on this API being up — the guard keeps managing positions when it is unreachable and
re-synchronizes its trade and risk rows when it returns.
"""
