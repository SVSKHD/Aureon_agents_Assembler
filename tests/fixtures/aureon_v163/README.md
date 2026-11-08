# Aureon V1.6.3 Windows client, as shipped

These files are copied **unmodified** from the V1.6.3 package (`aureon_v1/aureon/*.py`) so
`tests/test_aureon_client_compat.py` can drive the real collector, guard and API client
against the Assembler over HTTP. Do not edit them: a change here would test something other
than what runs on the Windows box. Only `MetaTrader5` is replaced, by V1.6.3's own test fake.
