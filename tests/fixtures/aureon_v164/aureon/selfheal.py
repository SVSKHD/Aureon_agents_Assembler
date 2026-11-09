"""Crash -> up to 3 Gemini patch attempts, each a PR gated by pytest + replay. Never touches guard/orders."""
import traceback, hashlib, subprocess, json, datetime as dt, re, os, time
from . import config
from . import db
from .timeutil import now as _utcnow

ATTEMPTS = {}            # service -> count (resets daily)
KNOWN = ("MT5 init failed", "connection", "timeout", "429", "Temporary failure")

def _post(text):
    from .alerter import post; post(text)

def _protected(tb):
    files = re.findall(r'File "(.+?)(aureon/\w+\.py)"', tb)
    return not files or any(f[1] not in config.SELFHEAL_PATCHABLE for f in files)

def _patch_with_gemini(service, tb, prev_diffs):
    from google import genai
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    m = re.findall(r'File "(.+?aureon/\w+\.py)", line (\d+)', tb)
    files = {}
    for path, _ in m:
        rel = path[path.index("aureon/"):]
        if os.path.exists(rel) and rel in config.SELFHEAL_PATCHABLE: files[rel] = open(rel).read()
    if not files: return None, None
    prompt = ("Fix this crash in a small Python trading-assistant service. Return ONLY a unified diff against the files shown, "
              "minimal change, no refactors, keep behaviour identical otherwise.\n\nTRACEBACK:\n" + tb[-3000:] +
              "\n\nPREVIOUS FAILED PATCHES:\n" + "\n---\n".join(prev_diffs) + "\n\nFILES:\n" +
              "\n".join(f"### {k}\n{v}" for k, v in files.items()))
    r = client.models.generate_content(model=config.GEMINI_MODEL, contents=prompt)
    diff = r.text.strip().strip("`")
    return diff, hashlib.sha256(prompt.encode()).hexdigest()[:12]

def _diff_touches_protected(diff):
    """True unless EVERY file in the diff is on the explicit patchable allowlist (and the diff is small)."""
    if not diff or len(diff) > 20000: return True
    files = set(re.findall(r"^\+\+\+ [ab]/(.+)$", diff, flags=re.M)) | set(re.findall(r"^--- [ab]/(.+)$", diff, flags=re.M))
    files = {f.strip() for f in files if f.strip() != "/dev/null"}
    if not files: return True
    PROTECTED_PREFIXES = ("db/", "deploy/", "scripts/", ".env", "aureon/config.py", "aureon/orders.py", "aureon/guard.py", "aureon/risk.py", "assembler/")
    if any(f.startswith(PROTECTED_PREFIXES) for f in files): return True
    return not all(f in config.SELFHEAL_PATCHABLE for f in files)

class SandboxUnavailable(RuntimeError): pass

SANDBOX_IMAGE = "aureon-sandbox:latest"
ISOLATION_FLAGS = ["--network", "none", "--memory", "1g", "--cpus", "1", "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:size=256m",
                   "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--user", "65534:65534"]
FORBIDDEN_ENV_PREFIXES = ("DATABASE_URL", "ASSEMBLER_API", "DISCORD", "GEMINI", "GH_TOKEN", "MT5_", "AUREON_")

def sandbox_available():
    """Runtime capability check used at service start and before every validation. False => self-heal execution is DISABLED."""
    import shutil
    if not config.SELFHEAL_ENABLED: return False, "SELFHEAL_ENABLED=false"
    if not shutil.which("docker"): return False, "docker CLI not available in this runtime"
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0: return False, "docker daemon not reachable"
    if subprocess.run(["docker", "image", "inspect", SANDBOX_IMAGE], capture_output=True).returncode != 0: return False, f"{SANDBOX_IMAGE} image missing"
    return True, "ok"

def sandbox_env():
    """Environment passed INTO the sandbox: nothing from the host environment at all."""
    return ["-e", "DATABASE_URL=postgresql://sandbox", "-e", "STORE_MODE=db", "-e", "PYTHONDONTWRITEBYTECODE=1"]

def sandbox_cmd(workdir, *cmd):
    return ["docker", "run", "--rm", *ISOLATION_FLAGS, *sandbox_env(), "-v", f"{workdir}:/app:ro", "-w", "/app", SANDBOX_IMAGE, *cmd]

def _sandbox_pytest(wt):
    """SANDBOX-ONLY. Generated code runs exclusively inside a disposable container: no network, read-only tree, tmpfs /tmp,
    1 CPU / 1 GB, non-root, no docker socket, no secrets. If Docker or the sandbox image is missing, validation is BLOCKED —
    there is no host-subprocess fallback."""
    ok, why = sandbox_available()
    if not ok: raise SandboxUnavailable(why)
    r = subprocess.run(sandbox_cmd(wt, "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"), capture_output=True, text=True, timeout=900)
    open(os.path.join(wt, "SANDBOX_TEST_REPORT.txt"), "w").write(r.stdout[-8000:] + r.stderr[-2000:])
    return r.returncode == 0

def validation_db_is_isolated(vurl, prod_url):
    """Proves, by querying the validation DB itself, that the role cannot write and that it is not production:
    - connection must be a different database (dbname) or host than production;
    - role must have no CREATE on schema aureon and no INSERT/UPDATE/DELETE on any aureon table;
    - a write attempt inside a rolled-back transaction must be refused by the server.
    URL string comparison alone proves nothing, so it is not used."""
    try:
        import psycopg
        from urllib.parse import urlparse
        v, p = urlparse(vurl), urlparse(prod_url)
        if prod_url and (v.hostname, v.port or 5432, v.path) == (p.hostname, p.port or 5432, p.path): return False, "same host and database as production"
        with psycopg.connect(vurl, connect_timeout=5) as c, c.cursor() as cur:
            cur.execute("select current_user, current_database(), has_schema_privilege(current_user,'aureon','CREATE')")
            user, dbname, can_create = cur.fetchone()
            if can_create: return False, f"role {user} can CREATE in schema aureon"
            cur.execute("""select count(*) from information_schema.table_privileges where grantee=current_user and table_schema='aureon'
                           and privilege_type in ('INSERT','UPDATE','DELETE','TRUNCATE')""")
            if cur.fetchone()[0]: return False, f"role {user} has write privileges on aureon tables"
            cur.execute("begin")
            try:
                cur.execute("insert into aureon.deployments(commit_sha,actor,note) values('isolation-probe','selfheal','must fail')")
                cur.execute("rollback"); return False, f"write probe SUCCEEDED for role {user} — not read-only"
            except Exception:
                cur.execute("rollback")
        return True, f"validation db {dbname} as {user}: read-only verified"
    except Exception as e:
        return False, f"could not verify isolation: {type(e).__name__}: {e}"

def _try_patch(diff):
    """Apply + test in a separate git WORKTREE; the running tree is never touched."""
    import tempfile, shutil
    if _diff_touches_protected(diff): return None
    branch = f"selfheal/{_utcnow().replace(tzinfo=None):%Y%m%d-%H%M%S}"; wt = tempfile.mkdtemp(prefix="aureon-heal-")
    try:
        subprocess.run(["git", "worktree", "add", "-b", branch, wt, "HEAD"], check=True, capture_output=True)
        p = subprocess.run(["git", "apply", "--whitespace=fix", "-"], input=diff, text=True, cwd=wt)
        if p.returncode != 0: return None
        try: ok = _sandbox_pytest(wt)
        except SandboxUnavailable as e: _post(f"⛔ self-heal BLOCKED: {e}. Patch saved on branch only, not validated, not proposed."); return None
        if ok:                                            # replay check: validation role ONLY (read-only, separate DB preferred). No fallback to production credentials.
            vurl = os.environ.get("DATABASE_URL_VALIDATION")
            if not vurl: _post("⛔ self-heal: DATABASE_URL_VALIDATION not set — replay check skipped, patch NOT proposed"); return None
            iso_ok, iso_why = validation_db_is_isolated(vurl, os.environ.get("DATABASE_URL", ""))
            if not iso_ok: _post(f"⛔ self-heal: validation database not isolated — {iso_why}; patch NOT proposed"); return None
            flags = [f if f != "none" else os.environ.get("SANDBOX_DB_NETWORK", "none") for f in ISOLATION_FLAGS]   # only the network may differ, to reach the VALIDATION db
            chk = subprocess.run(["docker", "run", "--rm", *flags, "-e", f"DATABASE_URL={vurl}", "-e", "STORE_MODE=db", "-v", f"{wt}:/app:ro", "-w", "/app", SANDBOX_IMAGE,
                                  "python", "-c", "from aureon import detector; import sys; sys.exit(0 if len(detector.verify_last_days(7))==0 else 1)"], capture_output=True, timeout=900)
            ok = chk.returncode == 0
        if not ok: return None
        subprocess.run(["git", "commit", "-am", "selfheal: auto patch (pytest + 7-day replay passed)"], check=True, cwd=wt, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", branch], check=True, cwd=wt, capture_output=True)
        pr = subprocess.run(["gh", "pr", "create", "--fill", "--title", f"selfheal: {branch}", "--body", "AI-generated patch. Validated ONLY inside the isolated sandbox (no network, read-only, no secrets). REQUIRES HUMAN REVIEW AND MERGE — nothing is applied automatically. Test report: SANDBOX_TEST_REPORT.txt on this branch."], capture_output=True, text=True, cwd=wt)
        return pr.stdout.strip() or branch
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", wt], capture_output=True); shutil.rmtree(wt, ignore_errors=True)

def _tooling_ok():
    import shutil; return shutil.which("git") is not None and shutil.which("gh") is not None

def handle(service, exc):
    tb = traceback.format_exc()
    ok, why = sandbox_available()
    if not ok:
        _post(f"⚠️ {service} crashed — self-heal DISABLED ({why}); manual fix needed:\n```{tb[-1200:]}```"); time.sleep(30); return
    if not _tooling_ok():
        _post(f"⚠️ {service} crashed and git/gh are not installed — self-heal disabled, manual fix needed:\n```{tb[-1200:]}```"); time.sleep(30); return
    if config.STORE_MODE == "api":                      # MT5 box: never patch there, just report
        _post(f"⚠️ {service} (MT5 box) error — restart loop continues: `{type(exc).__name__}: {exc}`"); time.sleep(30); return
    if any(k.lower() in tb.lower() for k in KNOWN):
        _post(f"{service}: transient error, retrying — `{type(exc).__name__}: {exc}`"); return
    if _protected(tb) or not config.GEMINI_API_KEY:
        _post(f"🚨 {service} crashed in protected/unpatchable code — manual fix needed:\n```{tb[-1500:]}```"); return
    n = ATTEMPTS.get(service, 0)
    if n >= config.SELFHEAL_MAX_ATTEMPTS:
        _post(f"🚨 {service}: 3 patch attempts failed — PAUSING this service. Guard stays LIVE. Reply `resume` after a manual fix.\n```{tb[-1200:]}```")
        db.heartbeat(service, "PAUSED", "selfheal exhausted"); raise SystemExit(1)
    ATTEMPTS[service] = n + 1
    prev = ATTEMPTS.get(service + "_diffs", [])
    diff, ph = _patch_with_gemini(service, tb, prev)
    pr = _try_patch(diff) if diff else None
    if pr:
        db.q("insert into deployments(commit_sha,actor,model,prompt_hash,note) values(%s,'selfheal',%s,%s,%s)", (pr, config.GEMINI_MODEL, ph, f"{service} attempt {n+1}"))
        _post(f"🩹 {service} crashed — patch attempt {n+1}/3 passed tests, PR ready: {pr}\n`{type(exc).__name__}: {exc}`")
    else:
        ATTEMPTS[service + "_diffs"] = prev + [diff or "(no diff)"]
        _post(f"🩹 {service} crashed — patch attempt {n+1}/3 did not pass tests. `{type(exc).__name__}: {exc}`")
