Recorded DhanHQ v2 response fixtures.

These are the documented example responses from the DhanHQ v2 docs and the official SDK
(`dhan-oss/DhanHQ-py`) test data, lightly extended (a second row, realistic ids). They contain
**no real account data and no credentials**. Field names, misspellings (`availabelBalance`,
`receiveableAmount`) and shapes are kept exactly as documented.

`master_real_sample.csv` is 26 rows copied verbatim from Dhan's public instrument master
(`api-scrip-master-detailed.csv`, Last-Modified 2026-11-10), plus one deliberate duplicate of
RELIANCE (the master repeats rows). It covers NSE/BSE equities, a G-sec and an ETF on NSE_EQ,
indices, NSE index and stock futures/options, a BSE SENSEX option, an expired BSE currency
future, and NSE/MCX commodity rows. Public market metadata only. It pins two facts of the real
file: TICK_SIZE is in paise, and index derivatives carry an exchange token (NIFTY: 26000) as
UNDERLYING_SECURITY_ID, not the IDX_I security id (13).
