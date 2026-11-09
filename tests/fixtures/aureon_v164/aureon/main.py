"""python -m aureon.main <collector|detector|alerter|guard|review|scheduler|weekend-review> [args]
  weekend-review [--last-week] [--week YYYY-MM-DD] [--force]   (read-only; also auto-triggered by the scheduler on a closed weekend)
Single-command Windows start: python -m aureon.watchdog (supervises the services listed in WATCHDOG_SERVICES)."""
import sys, runpy, os

SERVICES = ("collector", "detector", "alerter", "guard", "review", "scheduler", "weekend_review")   # discovered by aureon.watchdog
ALIASES = {"weekend-review": "weekend_review"}

def run(argv):
    svc = ALIASES.get(argv[1] if len(argv) > 1 else "", argv[1] if len(argv) > 1 else "")
    if svc not in SERVICES:
        print(__doc__); return 1
    os.environ.setdefault("AUREON_SERVICE", svc)
    sys.argv = [f"aureon.{svc}"] + argv[2:]
    runpy.run_module(f"aureon.{svc}", run_name="__main__"); return 0

if __name__ == "__main__":
    sys.exit(run(sys.argv))
