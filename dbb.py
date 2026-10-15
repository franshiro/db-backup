#!/usr/bin/env python3
"""dbb - backup & restore PostgreSQL / MySQL.

Uses local client tools when installed, otherwise falls back to Docker.
Run `dbb` without arguments for the interactive menu.
Data lives in ~/.dbb (override with DBB_HOME, DBB_CONFIG, DBB_BACKUP_DIR).
"""
import argparse
import getpass
import gzip
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

__version__ = "1.4.0"

# Raw URL of the repo holding dbb.py, used by `dbb update`.
REPO_RAW = os.environ.get("DBB_REPO_RAW", "https://raw.githubusercontent.com/franshiro/db-backup/main")

HOME = Path(os.environ.get("DBB_HOME", Path.home() / ".dbb"))
CONFIG = Path(os.environ.get("DBB_CONFIG", HOME / "databases.json"))
BACKUP_DIR = Path(os.environ.get("DBB_BACKUP_DIR", HOME / "backups"))

DEFAULTS = {
    "postgres": {"port": 5432, "user": "postgres", "image": "postgres:17", "ext": ".pg.sql.gz"},
    "mysql": {"port": 3306, "user": "root", "image": "mysql:8", "ext": ".my.sql.gz"},
}
TOOLS = {
    "postgres": {"backup": ["pg_dump"], "restore": ["psql"]},
    "mysql": {"backup": ["mysqldump"], "restore": ["mysql"]},
}
FIELDS = ["name", "type", "host", "port", "user", "password", "database", "version", "docker_image"]

# ---------- output ----------

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
ACCENT, DIM, BOLD, GREEN, RED, YELLOW = "38;5;45", "2", "1", "38;5;78", "38;5;203", "38;5;221"
TYPE_COLOR = {"postgres": "38;5;75", "mysql": "38;5;214"}
LOGO = [
    "██████╗ ██████╗ ██████╗ ",
    "██╔══██╗██╔══██╗██╔══██╗",
    "██║  ██║██████╔╝██████╔╝",
    "██║  ██║██╔══██╗██╔══██╗",
    "██████╔╝██████╔╝██████╔╝",
    "╚═════╝ ╚═════╝ ╚═════╝ ",
]
GRADIENT = ["38;5;51", "38;5;45", "38;5;39", "38;5;33", "38;5;27", "38;5;63"]


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if COLOR else s


def ok(msg):
    print(c(GREEN, "✓ ") + msg)


def info(msg):
    print(c(ACCENT, "→ ") + c(DIM, msg), file=sys.stderr)


def die(msg):
    sys.exit(c(RED, "✗ ") + msg)


def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def cols():
    return shutil.get_terminal_size().columns or 80


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def ago(ts):
    s = time.time() - ts
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return "just now"


def clip(s, n):
    return s if len(s) <= n else s[: max(n - 1, 0)] + "…"


def box(title, rows, color=GREEN):
    """Rounded box with a title and aligned key/value rows."""
    w = min(max([len(title) + 4] + [len(v) + 12 for _, v in rows]), cols() - 4)  # total width = w + 3
    print(c(color, "╭─ ") + c(color + ";1", title) + c(color, " " + "─" * (w - len(title) - 2) + "╮"))
    for k, v in rows:
        v = clip(v, w - 12)
        print(c(color, "│ ") + c(DIM, f"{k:<10}") + " " + v + " " * (w - 11 - len(v)) + c(color, "│"))
    print(c(color, "╰" + "─" * (w + 1) + "╯"))


def ask(label, default=""):
    hint = c(DIM, f" ({default})") if default else ""
    return input(c(ACCENT, "? ") + c(BOLD, label) + hint + " ").strip() or default


class Spinner:
    """Animated status line on stderr; set .n to show bytes processed."""
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text):
        self.text, self.n, self.t0 = text, 0, time.time()
        self.on = sys.stderr.isatty()
        self.stop = threading.Event()

    def __enter__(self):
        if self.on:
            threading.Thread(target=self._loop, daemon=True).start()
        return self

    def _loop(self):
        i = 0
        while not self.stop.wait(0.08):
            el = time.time() - self.t0
            extra = f"{human(self.n)} · {int(el // 60):02d}:{int(el % 60):02d}"
            sys.stderr.write(f"\r\033[2K{c(ACCENT, self.FRAMES[i % 10])} {self.text}  {c(DIM, extra)}")
            sys.stderr.flush()
            i += 1

    def __exit__(self, *exc):
        self.stop.set()
        if self.on:
            sys.stderr.write("\r\033[2K")
            sys.stderr.flush()

    @property
    def elapsed(self):
        return f"{time.time() - self.t0:.1f}s"


def legend(keys):
    """Horizontal shortcut bar: [(key, action), ...], trailing items dropped if the terminal is narrow."""
    out, width = [], 0
    for k, action in keys:
        width += len(k) + len(action) + 5
        if width > cols() - 2:
            break
        out.append(c("38;5;16;48;5;45;1", f" {k} ") + " " + c(DIM, action))
    return "  ".join(out)


def select(title, labels, descs=None, back="back"):
    """Arrow-key / j-k selector. Returns chosen index; q, Esc or Ctrl-C raise KeyboardInterrupt."""
    import termios
    import tty
    n, i, top = len(labels), 0, 0
    h = min(n, 12)  # visible rows; list scrolls past this
    lw = max(len(x) for x in labels) + 2
    keys = [("↑↓", "move"), ("enter", "select")] + ([("1-9", "pick")] if n > 1 else []) \
        + ([("g/G", "top/bottom")] if n > h else []) + [("q", back)]
    print(c(BOLD, title))
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write("\033[?25l")
    try:
        tty.setcbreak(fd)
        while True:
            top = min(top, i) if i < top else max(top, i - h + 1)
            for j in range(top, top + h):
                label = clip(labels[j], cols() - 8)
                desc = clip(descs[j], max(cols() - lw - 8, 0)) if descs else ""
                num = str(j + 1) if j < 9 else " "
                if j == i:
                    line = c(ACCENT + ";1", f" ❯ {num} {label:<{lw}}") + desc
                else:
                    line = "   " + c(DIM, num) + f" {label:<{lw}}" + c(DIM, desc)
                sys.stdout.write("\033[2K" + line + "\n")
            more = c(DIM, f"   {i + 1}/{n}") if n > h else ""
            sys.stdout.write("\033[2K" + more + "\n\033[2K" + legend(keys) + "\n")
            sys.stdout.flush()
            k = os.read(fd, 3)
            if k in (b"\x1b[A", b"k"):
                i = (i - 1) % n
            elif k in (b"\x1b[B", b"j"):
                i = (i + 1) % n
            elif k == b"g":
                i = 0
            elif k == b"G":
                i = n - 1
            elif k.isdigit() and 1 <= int(k) <= min(n, 9):
                i = int(k) - 1
                return i
            elif k in (b"\r", b"\n"):
                return i
            elif k in (b"q", b"\x1b", b"\x03"):
                raise KeyboardInterrupt
            sys.stdout.write(f"\033[{h + 2}A")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def pick(items, label, fmt=str):
    if not items:
        die(f"no {label} available")
    if interactive():
        return items[select(label, [fmt(x) for x in items])]
    for i, it in enumerate(items, 1):
        print(f"  {i}) {fmt(it)}")
    while True:
        s = input(f"{label} [1-{len(items)}]: ").strip()
        if s.isdigit() and 1 <= int(s) <= len(items):
            return items[int(s) - 1]


# ---------- database list ----------

def load():
    if not CONFIG.exists():
        return []
    return json.loads(CONFIG.read_text())


def save(dbs):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(dbs, indent=2) + "\n")
    os.chmod(CONFIG, 0o600)  # contains passwords


def validate(db):
    for k in ("name", "type", "host", "user", "database"):
        if not db.get(k):
            die(f"entry {db.get('name', '?')!r} missing field {k!r}")
    if db["type"] not in DEFAULTS:
        die(f"entry {db['name']!r}: type must be postgres or mysql")
    db.setdefault("port", DEFAULTS[db["type"]]["port"])
    db.setdefault("password", "")
    return db


def get_db(name):
    for db in load():
        if db["name"] == name:
            return db
    die(f"database {name!r} not found (see: dbb list)")


def describe(db):
    return f"{db['name']:<18} {db['type']:<9} {db['database']:<16} {db['user']}@{db['host']}:{db['port']}"


def backups():
    return sorted(BACKUP_DIR.glob("*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True)


# ---------- running tools (local or docker) ----------

def client(db, step, force_docker, image=None):
    """Return (wrap, env, host, image). image is None when local tools are used."""
    t = db["type"]
    pw_var = "PGPASSWORD" if t == "postgres" else "MYSQL_PWD"
    env = dict(os.environ, **{pw_var: str(db.get("password", ""))})
    host = db["host"]

    if not force_docker and all(shutil.which(x) for x in TOOLS[t][step]):
        return (lambda cmd: cmd), env, host, None

    if not shutil.which("docker"):
        die(f"{', '.join(TOOLS[t][step])} not installed and docker not found")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        die("docker is installed but daemon is not running")
    image = image or DEFAULTS[t]["image"]
    base = ["docker", "run", "--rm", "-i", "-q", "-e", pw_var]
    if platform.system() == "Linux":
        base += ["--network", "host"]
    elif host in ("localhost", "127.0.0.1"):
        host = "host.docker.internal"
    # mariadb images (11+) ship mariadb-dump / mariadb instead of the mysql names
    rename = {"mysqldump": "mariadb-dump", "mysql": "mariadb"} if image.startswith("mariadb") else {}
    return (lambda cmd: base + [image, rename.get(cmd[0], cmd[0])] + cmd[1:]), env, host, image


def probe(db, force_docker=False, image=None):
    """Connect to the server. Returns (version, database_exists, error)."""
    wrap, env, host, _ = client(db, "restore", force_docker, image)
    name = db["database"]
    if db["type"] == "postgres":
        lit = "'" + name.replace("'", "''") + "'"
        cmd = ["psql", "-h", host, "-p", str(db["port"]), "-U", db["user"], "-d", "postgres", "-Atc",
               f"SELECT current_setting('server_version'), EXISTS(SELECT 1 FROM pg_database WHERE datname = {lit})"]
        sep = "|"
    else:
        lit = "'" + name.replace("\\", "\\\\").replace("'", "''") + "'"
        cmd = ["mysql", "-h", host, "-P", str(db["port"]), "-u", db["user"], "-N", "-B", "-e",
               f"SELECT VERSION(), (SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = {lit})"]
        sep = "\t"
    try:
        r = subprocess.run(wrap(cmd), env=env, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None, False, "timed out"
    if r.returncode != 0:
        lines = [x for x in r.stderr.splitlines() if x.strip()]
        return None, False, lines[-1].strip() if lines else f"exit {r.returncode}"
    ver, exists = r.stdout.strip().split(sep)[:2]
    return ver.split()[0], exists in ("t", "1"), None


def pretty(t, ver):
    if not ver:
        return "unknown"
    if t == "postgres":
        return f"PostgreSQL {ver}"
    return f"MariaDB {ver.split('-')[0]}" if "mariadb" in ver.lower() else f"MySQL {ver.split('-')[0]}"


def image_for(t, ver):
    """Docker client image matching the server version."""
    nums = [int(x) for x in ver.split("-")[0].split(".")[:2]]
    if t == "postgres":
        return f"postgres:{nums[0]}" if nums[0] >= 10 else f"postgres:{nums[0]}.{nums[1]}"
    if "mariadb" in ver.lower():
        return "mariadb:11"
    # mysql:5.7 has no arm64 image; the 8.0 client handles 5.x servers (see --column-statistics in backup)
    return "mysql:8.4" if nums >= [8, 4] else "mysql:8.0"


def runner(db, step, force_docker):
    """Return (wrap, env, host, version). Picks local tools, else a docker image matching the server."""
    t = db["type"]
    pinned = str(db.get("docker_image") or "")
    if pinned[:1].isdigit():  # bare version like "16" -> "postgres:16"
        pinned = f"{DEFAULTS[t]['image'].split(':')[0]}:{pinned}"
    wrap, env, host, image = client(db, step, force_docker, pinned or None)
    if image is None:
        info(f"engine: local {', '.join(TOOLS[t][step])}")
        return wrap, env, host, None
    ver = None
    if not pinned:
        ver, _, _ = probe(db, force_docker=True)
        if ver:
            wrap, env, host, image = client(db, step, True, image_for(t, ver))
    info(f"engine: docker {image}" + (f" · server {pretty(t, ver)}" if ver else ""))
    return wrap, env, host, ver


def run(argv, env, stdin=None, stdout=None, check=True):
    p = subprocess.run(argv, env=env, stdin=stdin, stdout=stdout)
    if check and p.returncode != 0:
        die(f"command failed ({p.returncode}): {argv[-1] if argv else ''}")
    return p.returncode


def copy(src, dst, sp):
    while True:
        b = src.read(1 << 16)
        if not b:
            return
        dst.write(b)
        sp.n += len(b)


def stream(argv, env, sp, src=None, dst=None):
    """Pipe src (file obj) -> process -> dst (file obj), counting bytes on the spinner."""
    p = subprocess.Popen(argv, env=env,
                         stdin=subprocess.PIPE if src else None,
                         stdout=subprocess.PIPE if dst else None)
    if src:
        copy(src, p.stdin, sp)
        p.stdin.close()
    if dst:
        copy(p.stdout, dst, sp)
    return p.wait()


# ---------- backup ----------

def new_backup_path(db, tag=""):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{db['name']}{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    ext = DEFAULTS[db["type"]]["ext"]
    out, n = BACKUP_DIR / f"{stem}{ext}", 1
    while out.exists():
        n += 1
        out = BACKUP_DIR / f"{stem}_{n}{ext}"
    return out


def backup(db, out, force_docker):
    """Dump db to a gzip file. Returns (path, elapsed)."""
    wrap, env, host, ver = runner(db, "backup", force_docker)
    out = Path(out) if out else new_backup_path(db)
    tmp = out.with_name(out.name + ".part")

    if db["type"] == "postgres":
        cmd = wrap(["pg_dump", "-h", host, "-p", str(db["port"]), "-U", db["user"],
                    "--no-owner", "--no-acl", db["database"]])
    else:
        cmd = wrap(["mysqldump", "-h", host, "-P", str(db["port"]), "-u", db["user"],
                    "--single-transaction", "--routines", "--triggers", "--events",
                    "--no-tablespaces"]
                   + (["--column-statistics=0"] if ver and ver.startswith("5.") else [])  # 8.x client vs 5.x server
                   + [db["database"]])
    with Spinner(f"backing up {c(BOLD, db['name'])}") as sp, gzip.open(tmp, "wb") as f:
        rc = stream(cmd, env, sp, dst=f)

    if rc != 0:
        tmp.unlink(missing_ok=True)
        die(f"backup failed (exit {rc})")
    tmp.rename(out)
    return out, sp.elapsed


# ---------- restore ----------

def file_type(path):
    for t, d in DEFAULTS.items():
        if path.name.endswith(d["ext"]):
            return t
    die("unknown backup format (expected .pg.sql.gz or .my.sql.gz)")


def check_gzip(path):
    try:
        with Spinner(f"verifying {path.name}") as sp, gzip.open(path, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                sp.n += len(b)
    except (OSError, EOFError) as e:
        die(f"{path} is corrupt: {e}")


def restore(db, path, force_docker):
    """Drop + recreate target database, then load backup. Returns elapsed."""
    wrap, env, host, _ = runner(db, "restore", force_docker)
    name = db["database"]

    with Spinner(f"restoring {c(BOLD, path.name)} → {c(BOLD, db['name'])}") as sp:
        if db["type"] == "postgres":
            conn = ["-h", host, "-p", str(db["port"]), "-U", db["user"]]
            ident = '"' + name.replace('"', '""') + '"'
            lit = "'" + name.replace("'", "''") + "'"
            run(wrap(["psql", *conn, "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-q",
                      "-c", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            f"WHERE datname = {lit} AND pid <> pg_backend_pid();",
                      "-c", f"DROP DATABASE IF EXISTS {ident};",
                      "-c", f"CREATE DATABASE {ident};"]), env, stdout=subprocess.DEVNULL)
            with gzip.open(path, "rb") as f:
                rc = stream(wrap(["psql", *conn, "-d", name, "-q", "-o", "/dev/null"]), env, sp, src=f)
        else:
            conn = ["-h", host, "-P", str(db["port"]), "-u", db["user"]]
            ident = "`" + name.replace("`", "``") + "`"
            run(wrap(["mysql", *conn, "-e",
                      f"DROP DATABASE IF EXISTS {ident}; CREATE DATABASE {ident};"]), env)
            with gzip.open(path, "rb") as f:
                rc = stream(wrap(["mysql", *conn, name]), env, sp, src=f)

    if rc != 0:
        die(f"restore finished with errors (exit {rc}); check output above")
    return sp.elapsed


# ---------- commands ----------

def cmd_list(a):
    dbs = load()
    if not dbs:
        print(c(DIM, f"no databases yet — add one with `dbb add`  (config: {CONFIG})"))
        return
    w = max([18] + [len(db["name"]) for db in dbs])
    print(c(DIM, f"   {'NAME':<{w}} {'SERVER':<20} {'DATABASE':<16} CONNECTION"))
    for db in dbs:
        t, tc = db["type"], TYPE_COLOR[db["type"]]
        conn = f"{db['user']}@{db['host']}:{db['port']}"
        print(f" {c(tc, '●')} {c(BOLD, db['name'].ljust(w))} {c(tc, pretty(t, db.get('version')).ljust(20))} "
              f"{db['database']:<16} {c(DIM, conn)}")


def save_new(db):
    """Test the connection, then save the entry (asks before overwriting)."""
    validate(db)
    with Spinner(f"connecting to {db['host']}:{db['port']}"):
        ver, exists, err = probe(db)
    if err:
        print(c(RED, "✗ ") + f"cannot connect: {err}")
        if not interactive() or ask("save anyway? [y/N]").lower() != "y":
            print(c(DIM, f"  {db['name']} not saved"))
            return
    else:
        db["version"] = ver
        box(f"✓ Connected · {db['name']}", [
            ("server", pretty(db["type"], ver)),
            ("database", db["database"] + ("" if exists else "  (not found, created on restore)"))])
    dbs = load()
    if any(x["name"] == db["name"] for x in dbs):
        if not interactive() or ask(f"{db['name']!r} exists, overwrite? [y/N]").lower() != "y":
            print(c(DIM, f"  {db['name']} already exists, skipped"))
            return
        dbs = [x for x in dbs if x["name"] != db["name"]]
    save(dbs + [db])
    ok(f"saved {c(BOLD, db['name'])}")


def cmd_add(a):
    if getattr(a, "env", None):
        return add_from_env(a.env)
    t = list(DEFAULTS)[select("Database type", list(DEFAULTS))] if interactive() else ""
    while t not in DEFAULTS:
        t = ask("type (postgres/mysql)").lower()
    d = DEFAULTS[t]
    save_new({
        "name": ask("name (alias)"),
        "type": t,
        "host": ask("host", "localhost"),
        "port": int(ask("port", str(d["port"]))),
        "user": ask("user", d["user"]),
        "password": getpass.getpass(c(ACCENT, "? ") + c(BOLD, "password") + " "),
        "database": ask("database name"),
    })


# ---------- .env import ----------

# key suffix -> field; matched longest first so DB_USERNAME is "user", not "name"
ENV_SUFFIX = {"HOST": "host", "HOSTNAME": "host", "SERVER": "host", "PORT": "port",
              "USER": "user", "USERNAME": "user", "PASSWORD": "password", "PASS": "password", "PWD": "password",
              "NAME": "database", "DATABASE": "database", "DB": "database", "DBNAME": "database",
              "CONNECTION": "type", "TYPE": "type", "DIALECT": "type", "DRIVER": "type", "ENGINE": "type"}
ENV_SKIP = ("REDIS", "MONGO", "CACHE", "QUEUE", "MAIL", "SMTP", "ELASTIC", "RABBIT", "AMQP", "KAFKA", "AWS", "S3")


def read_env(path):
    """Active KEY=VALUE pairs of a .env file. Lines starting with # are inactive and ignored."""
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        if v[:1] in ("'", '"') and v.find(v[0], 1) > 0:
            v = v[1:v.find(v[0], 1)]
        else:
            v = re.split(r"\s+#", v)[0].strip()  # inline comment
        env[k] = v
    # ${VAR} references to other keys in the same file
    return {k: re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), v) for k, v in env.items()}


def db_type(s):
    s = (s or "").lower()
    if "postgres" in s or s in ("pgsql", "pg"):
        return "postgres"
    if "mysql" in s or "maria" in s:
        return "mysql"
    return None


def env_candidates(env):
    """Database configs found in env: connection URLs and PREFIX_HOST/PORT/USER/... groups."""
    found = []
    for k, v in env.items():
        t = db_type(v.split("://")[0].split("+")[0]) if "://" in v else None
        if not t:
            continue
        u = urllib.parse.urlsplit(v)
        found.append({"src": k, "prefix": re.sub(r"_?(URL|URI|DSN)$", "", k), "type": t,
                      "host": u.hostname or "localhost", "port": u.port or DEFAULTS[t]["port"],
                      "user": urllib.parse.unquote(u.username or "") or DEFAULTS[t]["user"],
                      "password": urllib.parse.unquote(u.password or ""),
                      "database": urllib.parse.unquote(u.path.lstrip("/"))})

    groups = {}
    for k, v in env.items():
        for suf in sorted(ENV_SUFFIX, key=len, reverse=True):
            prefix = k[: -len(suf)]
            if k.endswith(suf) and (prefix == "" or prefix.endswith("_") or prefix == "PG"):
                groups.setdefault(prefix.rstrip("_"), {}).setdefault(ENV_SUFFIX[suf], v)
                break
    for prefix, g in groups.items():
        if any(x in prefix for x in ENV_SKIP) or not g.get("database") or not (g.get("host") or g.get("user")):
            continue
        port = g.get("port", "")
        t = db_type(g.get("type")) or db_type(prefix) or {"5432": "postgres", "3306": "mysql"}.get(port)
        found.append({"src": f"{prefix}_*" if prefix else "*", "prefix": prefix, "type": t,
                      "host": g.get("host") or "localhost",
                      "port": int(port) if port.isdigit() else (DEFAULTS[t]["port"] if t else None),
                      "user": g.get("user") or (DEFAULTS[t]["user"] if t else ""),
                      "password": g.get("password", ""), "database": g["database"]})

    seen, unique = set(), []
    for f in found:
        key = (f["host"], f["port"], f["user"], f["database"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return sorted(unique, key=lambda f: not f["src"].endswith("*"))  # PREFIX_* groups first, then URLs


def add_from_env(path):
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / ".env"
    if not path.is_file():
        die(f"{path} not found")
    found = env_candidates(read_env(path))
    if not found:
        die(f"no active database settings in {path} (lines starting with # are ignored)")

    # default alias: <project folder>[-<env variant>][-<prefix>]
    variant = path.name[4:].lstrip(".") if path.name.startswith(".env") else path.stem
    base = "-".join(x for x in (path.resolve().parent.name, variant) if x)
    for f in found:
        extra = re.sub(r"_?(DATABASE|DB)$", "", f["prefix"]).lower().replace("_", "-")
        f["name"] = "-".join(x for x in (base, extra) if x)

    info(f"{len(found)} database config(s) found in {path}")
    label = lambda f: (f"{f['name']:<22} {f['type'] or '?':<9} {f['database']:<14} "
                       f"{f['user']}@{f['host']}:{f['port'] or '?'}  ← {f['src']}")
    if interactive() and len(found) > 1:
        i = select("Which one?", [label(f) for f in found] + ["All of them"])
        found = found if i == len(found) else [found[i]]
    elif not interactive():
        for f in found:
            print("  " + label(f))

    for f in found:
        print()
        if not f["type"]:
            if not interactive():
                print(c(YELLOW, "! ") + f"{f['name']}: cannot tell postgres or mysql, skipped")
                continue
            f["type"] = list(DEFAULTS)[select(f"Type for {f['name']} ({f['src']})?", list(DEFAULTS))]
            f["port"] = f["port"] or DEFAULTS[f["type"]]["port"]
            f["user"] = f["user"] or DEFAULTS[f["type"]]["user"]
        if interactive():
            print(c(DIM, f"  from {f['src']}: {f['type']} {f['user']}@{f['host']}:{f['port']}/{f['database']}"))
            f["name"] = ask("name (alias)", f["name"])
        save_new({k: f[k] for k in ("name", "type", "host", "port", "user", "password", "database")})


def cmd_check(a):
    dbs = load()
    targets = [x for x in dbs if not a.names or x["name"] in a.names]
    missing = set(a.names) - {x["name"] for x in targets}
    if missing:
        die(f"not found: {', '.join(sorted(missing))}")
    if not targets:
        die("no databases yet — add one with `dbb add`")
    for db in targets:
        with Spinner(f"checking {c(BOLD, db['name'])}") as sp:
            ver, exists, err = probe(db, a.docker)
        name = c(BOLD, db["name"].ljust(18))
        if err:
            print(f"{c(RED, '✗')} {name} {c(RED, clip(err, max(cols() - 22, 20)))}")
            continue
        db["version"] = ver
        state = c(GREEN, "db ✓") if exists else c(YELLOW, "db not found")
        print(f"{c(GREEN, '✓')} {name} {pretty(db['type'], ver):<22} {state}  {c(DIM, sp.elapsed)}")
    save(dbs)


def cmd_remove(a):
    dbs = load()
    name = a.name or pick(dbs, "Remove which database?", describe)["name"]
    left = [x for x in dbs if x["name"] != name]
    if len(left) == len(dbs):
        die(f"{name!r} not found")
    save(left)
    ok(f"removed {c(BOLD, name)}")


def cmd_export(a):
    data = json.dumps(load(), indent=2) + "\n"
    if a.file == "-":
        sys.stdout.write(data)
    else:
        Path(a.file).write_text(data)
        os.chmod(a.file, 0o600)
        ok(f"exported {len(load())} databases to {c(BOLD, a.file)}")


def cmd_import(a):
    incoming = json.loads(sys.stdin.read() if a.file == "-" else Path(a.file).expanduser().read_text())
    if not isinstance(incoming, list):
        die("import file must be a JSON array of database entries")
    incoming = [validate({k: v for k, v in x.items() if k in FIELDS}) for x in incoming]
    names = {x["name"] for x in incoming}
    current = [] if a.replace else [x for x in load() if x["name"] not in names]
    save(current + incoming)
    ok(f"imported {len(incoming)} databases ({'replaced list' if a.replace else 'merged'})")


def cmd_backup(a):
    names = [x["name"] for x in load()] if a.all else a.names
    if not names:
        names = [pick(load(), "Backup which database?", describe)["name"]]
    if a.output and len(names) > 1:
        die("-o only works with a single database")
    for n in names:
        db = get_db(n)
        out, el = backup(db, a.output, a.docker)
        if len(names) == 1:
            box("✓ Backup complete", [("database", f"{db['name']} ({db['type']})"),
                                      ("file", str(out)), ("size", human(out.stat().st_size)), ("time", el)])
        else:
            ok(f"{c(BOLD, n.ljust(18))} {human(out.stat().st_size):>9}  {c(DIM, el)}  {c(DIM, out.name)}")


def cmd_restore(a):
    if a.file:
        path = Path(a.file).expanduser()
    else:
        path = pick(backups(), "Restore which backup?",
                    lambda p: f"{p.name:<46} {human(p.stat().st_size):>9}  {ago(p.stat().st_mtime)}")
    if not path.exists():
        die(f"{path} not found")
    kind = file_type(path)
    check_gzip(path)

    db = get_db(a.to) if a.to else pick([x for x in load() if x["type"] == kind],
                                        f"Restore into which {kind} database?", describe)
    if db["type"] != kind:
        die(f"{path.name} is a {kind} backup but {db['name']} is {db['type']}")

    print()
    box("! DANGER · full restore", [
        ("target", f"{db['name']} → {db['database']} @ {db['host']}:{db['port']}"),
        ("action", "DROP database, recreate, load backup"),
        ("source", path.name),
        ("safety", "skipped" if a.no_safety_backup else "backup current target first"),
    ], color=RED)
    if a.dry_run:
        info("dry-run: nothing changed")
        return
    if not a.yes and ask(f"type {c(RED, db['database'])} to continue") != db["database"]:
        die("aborted")
    if not a.no_safety_backup:
        out, el = backup(db, new_backup_path(db, "_prerestore"), a.docker)
        ok(f"safety backup {c(DIM, out.name)} ({human(out.stat().st_size)}, {el})")
    el = restore(db, path, a.docker)
    box("✓ Restore complete", [("source", path.name), ("target", f"{db['name']} ({db['database']})"),
                               ("time", el)])


def installed_file():
    """Path of this executable when installed by install.sh (file named 'dbb'), else None (pip/source)."""
    f = Path(__file__).resolve()
    return f if f.name == "dbb" else None


def cmd_update(a):
    target = installed_file()
    if not target:
        die("not installed via install.sh; use `pip install --upgrade .` or git pull instead")
    url = f"{REPO_RAW}/dbb.py"
    try:
        with Spinner(f"downloading {url}"):
            code = urllib.request.urlopen(url, timeout=30).read()
        compile(code, "dbb.py", "exec")  # refuse to install something broken
    except Exception as e:
        die(f"update failed: {e}")
    tmp = target.with_name(".dbb.new")
    tmp.write_bytes(code)
    tmp.chmod(0o755)
    tmp.replace(target)
    ok(f"updated {target}")


def cmd_uninstall(a):
    target = installed_file()
    if not target:
        die("not installed via install.sh; use `pip uninstall dbb` instead")
    if not a.yes and ask(f"remove {target}? [y/N]").lower() != "y":
        die("aborted")
    target.unlink()
    ok(f"removed {target}")
    if HOME.exists():
        info(f"config and backups kept in {HOME} (delete manually if not needed)")


# ---------- interactive menu ----------

def header():
    print("\033[2J\033[H", end="")  # clear screen
    print()
    for line, color in zip(LOGO, GRADIENT):
        print("  " + c(color + ";1", line))
    print("  " + c(DIM, "backup & restore · PostgreSQL / MySQL") + "  " + c(ACCENT, f"v{__version__}"))
    print()

    dbs, files = load(), backups()
    have = lambda t: all(shutil.which(x) for x in TOOLS[t]["backup"] + TOOLS[t]["restore"])
    docker = bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    mark = lambda yes: c(GREEN, "✓") if yes else c(RED, "✗")
    print(f"  {c(ACCENT, '●')} {c(BOLD, str(len(dbs)))} databases   "
          f"{c(ACCENT, '●')} {c(BOLD, str(len(files)))} backups {c(DIM, human(sum(p.stat().st_size for p in files)))}   "
          f"{c(DIM, 'pg')} {mark(have('postgres'))}  {c(DIM, 'mysql')} {mark(have('mysql'))}  "
          f"{c(DIM, 'docker')} {mark(docker)}")
    print("  " + c(DIM, "─" * min(cols() - 4, 64)))
    print()


def menu():
    ns = argparse.Namespace
    items = [
        ("↑  Backup", "dump one database", lambda: cmd_backup(ns(names=[], all=False, output=None, docker=False))),
        ("⇈  Backup all", "dump every saved database", lambda: cmd_backup(ns(names=[], all=True, output=None, docker=False))),
        ("↓  Restore", "drop target & load a backup", lambda: cmd_restore(
            ns(file=None, to=None, yes=False, dry_run=False, no_safety_backup=False, docker=False))),
        ("≡  Databases", "show saved connections", lambda: cmd_list(None)),
        ("✓  Check", "test connections & detect server versions", lambda: cmd_check(ns(names=[], docker=False))),
        ("+  Add", "save a new connection", lambda: cmd_add(ns(env=None))),
        ("⇣  Add from .env", "read connection from a .env file", lambda: cmd_add(ns(env=ask(".env file", ".env")))),
        ("−  Remove", "delete a saved connection", lambda: cmd_remove(ns(name=None))),
        ("⇥  Export", "write database list to JSON", lambda: cmd_export(
            ns(file=ask("export to", "dbb-databases.json")))),
        ("⇤  Import", "merge database list from JSON", lambda: cmd_import(ns(file=ask("import from"), replace=False))),
        ("×  Quit", "", None),
    ]
    while True:
        header()
        try:
            label, _, fn = items[select("What do you want to do?", [x[0] for x in items], [x[1] for x in items], back="quit")]
        except KeyboardInterrupt:
            return
        if fn is None:
            return
        print()
        try:
            fn()
        except SystemExit as e:  # die() inside an action: show it, stay in the menu
            if e.code not in (0, None):
                print(e.code, file=sys.stderr)
        except KeyboardInterrupt:
            print()
            info("cancelled")
        print()
        try:
            input(c(DIM, "press enter to return to menu "))
        except (KeyboardInterrupt, EOFError):
            return


def main():
    p = argparse.ArgumentParser(prog="dbb", description="Backup & restore PostgreSQL / MySQL. "
                                "Run without arguments for the interactive menu.")
    p.add_argument("--version", action="version", version=f"dbb {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="show saved databases").set_defaults(fn=cmd_list)
    s = sub.add_parser("add", help="add database (interactive, or from a .env file)")
    s.add_argument("--env", nargs="?", const=".env", metavar="FILE",
                   help="read connection from a .env file or project folder (default ./.env); # lines are ignored")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("check", help="test connection & detect server version")
    s.add_argument("names", nargs="*", help="saved database names (empty = all)")
    s.add_argument("--docker", action="store_true", help="force docker even if local tools exist")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("remove", help="remove saved database")
    s.add_argument("name", nargs="?"); s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("export", help="export database list to JSON")
    s.add_argument("file", help="output file, or - for stdout"); s.set_defaults(fn=cmd_export)
    s = sub.add_parser("import", help="import database list from JSON (merge by name)")
    s.add_argument("file", help="input file, or - for stdin")
    s.add_argument("--replace", action="store_true", help="replace whole list instead of merging")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("backup", help="backup database(s)")
    s.add_argument("names", nargs="*", help="saved database names (empty = choose)")
    s.add_argument("--all", action="store_true", help="backup every saved database")
    s.add_argument("-o", "--output", help="output file (default: ~/.dbb/backups/<name>_<time>.<ext>)")
    s.add_argument("--docker", action="store_true", help="force docker even if local tools exist")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("restore", help="FULL restore: drop target database, then load backup")
    s.add_argument("file", nargs="?", help="backup file (empty = choose from backups)")
    s.add_argument("--to", help="target database name (empty = choose)")
    s.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    s.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    s.add_argument("--no-safety-backup", action="store_true",
                   help="do not backup target before dropping it (e.g. target db does not exist yet)")
    s.add_argument("--docker", action="store_true", help="force docker even if local tools exist")
    s.set_defaults(fn=cmd_restore)

    sub.add_parser("update", help="update dbb to the latest version").set_defaults(fn=cmd_update)
    s = sub.add_parser("uninstall", help="remove dbb (keeps ~/.dbb)")
    s.add_argument("-y", "--yes", action="store_true"); s.set_defaults(fn=cmd_uninstall)

    a = p.parse_args()
    try:
        if a.cmd:
            a.fn(a)
        elif interactive():
            menu()
        else:
            p.print_help()
    except KeyboardInterrupt:
        print()
        sys.exit(130)


if __name__ == "__main__":
    main()
