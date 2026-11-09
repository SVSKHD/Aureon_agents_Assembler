"""Forward-only, additive migrations. `python -m aureon.migrate` applies db/migrations/*.sql in order, once each.
Rollback policy: migrations never drop or rewrite columns; rolling back the APPLICATION image is always safe against a newer schema.
Destructive changes (if ever needed) get their own numbered file, a pg_dump beforehand, and a manual runbook — never autopull."""
import os, glob, sys
from . import db

def pending():
    db.q("create schema if not exists aureon"); db.q("create table if not exists aureon.schema_migrations(version text primary key, applied_at timestamptz default now())")
    done = {r["version"] for r in db.q("select version from aureon.schema_migrations")}
    files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "db", "migrations", "*.sql")))
    return [f for f in files if os.path.basename(f).split("_")[0] not in done]

def apply(dry=False):
    for f in pending():
        v = os.path.basename(f).split("_")[0]
        print(("would apply " if dry else "applying ") + os.path.basename(f))
        if dry: continue
        with db.conn() as c, c.cursor() as cur:
            cur.execute("begin"); cur.execute(open(f).read()); cur.execute("insert into aureon.schema_migrations(version) values(%s)", (v,)); cur.execute("commit")
    print("migrations up to date")

if __name__ == "__main__": apply(dry="--dry" in sys.argv)
