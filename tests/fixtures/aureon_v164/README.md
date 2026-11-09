# Aureon V1.6.4 Windows package, as shipped

`aureon/` is copied **unmodified** from the V1.6.4 package (`aureon_v1/aureon/`), so
`tests/test_aureon_watchdog_compat.py` runs the real watchdog (`aureon/watchdog.py`) and its
`ApiStore.health_summary()` against the Assembler over HTTP. Do not edit these files. Only
the child processes (`subprocess.Popen`) are faked, as V1.6.4's own tests do.
