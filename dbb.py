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

__version__ = "1.9.0"

# Raw URL of the repo holding dbb.py, used by `dbb update`.
REPO_RAW = os.environ.get("DBB_REPO_RAW", "https://raw.githubusercontent.com/franshiro/db-backup/main")

HOME = Path(os.environ.get("DBB_HOME", Path.home() / ".dbb"))
CONFIG = Path(os.environ.get("DBB_CONFIG", HOME / "databases.json"))
SETTINGS = HOME / "settings.json"
DEFAULT_BACKUP_DIR = HOME / "backups"


def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


# precedence: DBB_BACKUP_DIR env > settings.json (`dbb config backup-dir`) > ~/.dbb/backups
BACKUP_DIR = Path(os.environ.get("DBB_BACKUP_DIR") or load_settings().get("backup_dir") or DEFAULT_BACKUP_DIR)

DEFAULTS = {
    "postgres": {"port": 5432, "user": "postgres", "image": "postgres:17", "ext": ".pg.sql.gz"},
    "mysql": {"port": 3306, "user": "root", "image": "mysql:8", "ext": ".my.sql.gz"},
}
TOOLS = {
    "postgres": {"backup": ["pg_dump"], "restore": ["psql"]},
    "mysql": {"backup": ["mysqldump"], "restore": ["mysql"]},
}
FIELDS = ["name", "group", "stage", "type", "host", "port", "user", "password", "database", "bulk", "version", "docker_image"]

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


def select(title, labels, descs=None, back="back", checked=None):
    """Arrow-key / j-k selector. Returns chosen index; q, Esc or Ctrl-C raise KeyboardInterrupt.
    With checked (list of bools): checklist mode, space toggles in place, Enter returns the list."""
    import termios
    import tty
    n, i, top = len(labels), 0, 0
    h = min(n, 15)  # visible rows; list scrolls past this
    lw = max(len(x) for x in labels) + 2
    if checked is None:
        keys = [("↑↓", "move"), ("enter", "select")] + ([("1-9", "pick")] if n > 1 else [])
    else:
        keys = [("↑↓", "move"), ("space", "toggle"), ("a", "all/none"), ("enter", "save")]
    keys += ([("g/G", "top/bottom")] if n > h else []) + [("q", back)]
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
                if checked is not None:
                    num = c(GREEN, "◉") if checked[j] else c(DIM, "○")
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
            elif checked is not None and k == b" ":
                checked[i] = not checked[i]
            elif checked is not None and k == b"a":
                checked[:] = [not all(checked)] * n
            elif checked is None and k.isdigit() and 1 <= int(k) <= min(n, 9):
                i = int(k) - 1
                return i
            elif k in (b"\r", b"\n"):
                return i if checked is None else checked
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


def pick_db(label, dbs=None):
    """Two steps: group, then a database in it (q goes back to groups). Flat list without a tty."""
    dbs = load() if dbs is None else dbs
    if not dbs:
        die(f"no {label} available")
    groups = list(dict.fromkeys(x["group"] for x in dbs))  # load() keeps entries grouped
    if not interactive() or len(groups) == 1:
        return pick(dbs, label, describe)
    stages = lambda g: " · ".join(x["stage"] or x["name"] for x in dbs if x["group"] == g)
    while True:
        g = groups[select(label, groups, [stages(g) for g in groups], back="cancel")]
        inner = [x for x in dbs if x["group"] == g]
        try:
            return inner[select(f"{label} › {g}", [describe(x) for x in inner])]
        except KeyboardInterrupt:
            print()


def pick_backup(label):
    """Three steps: group, database, file (newest first). q goes back to groups."""
    rows = backup_rows()
    if not rows:
        die("no backup files available")
    if not interactive():
        return pick([r["path"] for r in rows], label,
                    lambda p: f"{p.name:<46} {human(p.stat().st_size):>9}  {ago(p.stat().st_mtime)}")
    group_of = {x["name"]: x["group"] for x in load()}
    for r in rows:  # files of removed databases still get a group from their name
        r["group"] = group_of.get(r["db"]) or split_name(r["db"])[0]
    groups = sorted({r["group"] for r in rows}, key=str.lower)
    count = lambda key, v: f"{sum(1 for r in rows if r[key] == v)} files"
    while True:
        g = groups[select(label, groups, [count("group", g) for g in groups], back="cancel")] if len(groups) > 1 else groups[0]
        names = list(dict.fromkeys(r["db"] for r in rows if r["group"] == g))
        try:
            n = names[select(f"{label} › {g}", names, [count("db", n) for n in names])] if len(names) > 1 else names[0]
            files = [r for r in rows if r["db"] == n]
            return files[select(f"{label} › {n}", [file_label(r) for r in files])]["path"]
        except KeyboardInterrupt:
            if len(groups) == 1:
                raise
            print()


# ---------- database list ----------

STAGES = ["LOCAL", "DEV", "STAGING", "PRODUCTION"]
STAGE_ALIAS = {"LOCAL": "LOCAL", "DEV": "DEV", "DEVELOPMENT": "DEV", "STAGING": "STAGING", "STG": "STAGING",
               "PROD": "PRODUCTION", "PRODUCTION": "PRODUCTION"}


def split_name(name):
    """Guess (group, stage) from an alias like "MES DEV" or "v3 (local)"; stage is "" if none."""
    m = re.match(r"^(.*?)[\s_-]*\(?([A-Za-z]+)\)?$", name.strip())
    if m and m[1] and m[2].upper() in STAGE_ALIAS:
        return m[1], STAGE_ALIAS[m[2].upper()]
    return name, ""


def group_key(db):
    stage = db.get("stage", "")
    return db["group"].lower(), STAGES.index(stage) if stage in STAGES else len(STAGES), db["name"].lower()


def load():
    if not CONFIG.exists():
        return []
    dbs = json.loads(CONFIG.read_text())
    for db in dbs:  # entries saved before groups existed
        if "group" not in db:
            db["group"], db["stage"] = split_name(db["name"])
    return sorted(dbs, key=group_key)


def save(dbs):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(sorted(dbs, key=group_key), indent=2) + "\n")
    os.chmod(CONFIG, 0o600)  # contains passwords


def validate(db):
    for k in ("name", "type", "host", "user", "database"):
        if not db.get(k):
            die(f"entry {db.get('name', '?')!r} missing field {k!r}")
    if db["type"] not in DEFAULTS:
        die(f"entry {db['name']!r}: type must be postgres or mysql")
    db.setdefault("port", DEFAULTS[db["type"]]["port"])
    db.setdefault("password", "")
    if not db.get("group"):
        db["group"], db["stage"] = split_name(db["name"])
    db.setdefault("stage", "")
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


DEFINER_RE = re.compile(rb"DEFINER=`(?:[^`]|``)*`@`(?:[^`]|``)*`")


class MysqlFilter:
    """Read wrapper that makes a mysqldump file load on another server:
    - drops SET @@GLOBAL.GTID_PURGED, which fails on a target with its own GTID history (ERROR 3546)
    - drops DEFINER=`user`@`host` from views, routines, triggers and events, so they belong to the
      restoring user instead of an account that may not exist there (ERROR 1449)"""
    def __init__(self, f):
        self.f, self.skip = f, False

    def read(self, n=-1):
        while True:
            line = self.f.readline()
            if not line:
                return b""
            if line.startswith(b"SET @@GLOBAL.GTID_PURGED"):
                self.skip = True
            if not self.skip:
                return line if line.startswith(b"INSERT INTO") else DEFINER_RE.sub(b"", line)  # never touch row data
            if line.rstrip().endswith(b";"):  # the GTID set can span several lines
                self.skip = False


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


def has_gtid_flag(wrap, env):
    """True when this mysqldump (MySQL, not MariaDB) supports --set-gtid-purged."""
    p = subprocess.run(wrap(["mysqldump", "--help"]), env=env, capture_output=True)
    return b"--set-gtid-purged" in p.stdout


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
                   + (["--set-gtid-purged=OFF"] if has_gtid_flag(wrap, env) else [])  # MariaDB dump has no such flag
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
                rc = stream(wrap(["mysql", *conn, name]), env, sp, src=MysqlFilter(f))

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
    dw = max([8] + [len(db["database"]) for db in dbs])
    last = {}  # db name -> mtime of its newest regular backup (safety backups don't count)
    for r in backup_rows():
        if not r["pre"]:
            last.setdefault(r["db"], r["mtime"])

    def when(db):
        ts = last.get(db["name"])
        if ts is None:
            return c(DIM, "never".ljust(16))
        age = time.time() - ts
        return c(GREEN if age < 86400 else YELLOW if age < 7 * 86400 else RED,
                 datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"))

    print(c(DIM, f"     {'NAME':<{w}} {'SERVER':<20} {'DATABASE':<{dw}} {'LAST BACKUP':<16} CONNECTION"))
    mark = lambda db: c(GREEN, "◉") if db.get("bulk") else c(DIM, "○")
    group = None
    for db in dbs:
        if db["group"] != group:
            group = db["group"]
            print(f"\n {c(ACCENT + ';1', '▸ ' + group)}")
        t, tc = db["type"], TYPE_COLOR[db["type"]]
        conn = f"{db['user']}@{db['host']}:{db['port']}"
        print(f"   {mark(db)} {c(BOLD, db['name'].ljust(w))} {c(tc, pretty(t, db.get('version')).ljust(20))} "
              f"{db['database']:<{dw}} {when(db)} {c(DIM, conn)}")
    print(c(DIM, f"\n {c(GREEN, '◉')} included in bulk backup ({sum(1 for x in dbs if x.get('bulk'))}/{len(dbs)})"
                 "  · change with `dbb mark`"))
    print(c(DIM, f" last backup: {c(GREEN, '< 1 day')}  {c(YELLOW, '< 7 days')}  {c(RED, 'older')}"))


def save_new(db, old=None):
    """Test the connection, then save the entry (asks before overwriting). old: name of the entry being edited."""
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
    dbs = [x for x in load() if x["name"] != old]
    if any(x["name"] == db["name"] for x in dbs):
        if not interactive() or ask(f"{db['name']!r} exists, overwrite? [y/N]").lower() != "y":
            print(c(DIM, f"  {db['name']} already exists, skipped"))
            return
        dbs = [x for x in dbs if x["name"] != db["name"]]
    save(dbs + [db])
    ok(f"saved {c(BOLD, db['name'])}" + (c(DIM, "  · ◉ included in bulk backup") if db.get("bulk") else ""))


def cmd_edit(a):
    if not interactive():
        die("edit is interactive; or edit the JSON directly: " + str(CONFIG))
    old = get_db(a.name) if a.name else pick_db("Edit which database?")
    db = dict(old)
    fields = ["group", "stage", "type", "host", "port", "user", "password", "database"]
    while True:
        print()
        show = lambda f: ("•" * 8 if db[f] else c(DIM, "(empty)")) if f == "password" else str(db.get(f) or "")
        labels = [f"{f:<9} {show(f)}" for f in fields] + ["✓ save", "× cancel"]
        try:
            i = select(f"Edit {old['name']}", labels, back="cancel")
        except KeyboardInterrupt:
            return info("cancelled, nothing changed")
        if i == len(fields):
            break
        if i > len(fields):
            return info("cancelled, nothing changed")
        f = fields[i]
        if f == "stage":
            db[f] = STAGES[select("Stage", STAGES)]
        elif f == "type":
            db[f] = list(DEFAULTS)[select("Database type", list(DEFAULTS))]
            db.pop("version", None)
        elif f == "password":
            db[f] = getpass.getpass(c(ACCENT, "? ") + c(BOLD, "password") + c(DIM, " (enter = keep) ")) or db[f]
        elif f == "port":
            v = ask("port", str(db[f]))
            db[f] = int(v) if v.isdigit() else db[f]
        else:
            db[f] = ask(f, str(db[f])).strip() or db[f]
    if (db["group"], db["stage"]) != (old["group"], old["stage"]):
        db["name"] = f"{db['group']} {db['stage']}".strip()
        info(f"renamed {old['name']} → {db['name']} (existing backup files keep the old name)")
    save_new(db, old=old["name"])


def choose_group(default=""):
    """Pick a group (e.g. MES) and stage (LOCAL/DEV/STAGING/PRODUCTION). Returns (group, stage, name)."""
    dbs = load()
    groups = sorted({x["group"] for x in dbs}, key=str.lower)
    if interactive() and groups:
        new = "+ new group" + (f" ({default})" if default and default not in groups else "")
        i = select("Group", groups + [new], [f"{sum(1 for x in dbs if x['group'] == g)} databases" for g in groups] + [""])
        group = groups[i] if i < len(groups) else ""
    else:
        group = ""
    while not group:
        group = ask("new group name (e.g. MES)", default).strip()
    taken = {x["stage"]: x["name"] for x in dbs if x["group"] == group}
    if interactive():
        stage = STAGES[select(f"Stage for {group}", STAGES, [f"exists: {taken[x]}" if x in taken else "" for x in STAGES])]
    else:
        stage = ""
        while stage not in STAGES:
            stage = ask(f"stage ({'/'.join(STAGES)})").upper()
    return group, stage, taken.get(stage) or f"{group} {stage}"


def cmd_add(a):
    if getattr(a, "env", None):
        return add_from_env(a.env)
    group, stage, name = choose_group()
    t = list(DEFAULTS)[select("Database type", list(DEFAULTS))] if interactive() else ""
    while t not in DEFAULTS:
        t = ask("type (postgres/mysql)").lower()
    d = DEFAULTS[t]
    save_new({
        "name": name,
        "group": group,
        "stage": stage,
        "bulk": stage == "PRODUCTION",  # production is in bulk backup by default
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
            f["group"], f["stage"], f["name"] = choose_group(path.resolve().parent.name)
        save_new({**{k: f[k] for k in ("name", "group", "stage", "type", "host", "port", "user", "password", "database")
                      if k in f}, "bulk": f.get("stage") == "PRODUCTION"})


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
    name = a.name or pick_db("Remove which database?", dbs)["name"]
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


def cmd_mark(a):
    dbs = load()
    if not dbs:
        die("no databases yet — add one with `dbb add`")
    if a.names:
        missing = set(a.names) - {x["name"] for x in dbs}
        if missing:
            die(f"not found: {', '.join(sorted(missing))}")
        for db in dbs:
            if db["name"] in a.names:
                db["bulk"] = not a.off
    elif interactive():
        marks = select("Include in bulk backup", [describe(x) for x in dbs], back="cancel",
                       checked=[bool(x.get("bulk")) for x in dbs])
        for db, m in zip(dbs, marks):
            db["bulk"] = m
    else:
        die("give database names, e.g. `dbb mark prod-pg app-my` (or --off)")
    save(dbs)
    on = [x["name"] for x in dbs if x.get("bulk")]
    ok(f"bulk backup: {len(on)}/{len(dbs)} databases" + (f" — {', '.join(on)}" if on else ""))


def filter_type(names, t=None):
    """Keep only databases of type t; with no t and a tty, ask when the list mixes postgres and mysql."""
    types = {x["name"]: x["type"] for x in load()}
    have = [k for k in DEFAULTS if any(types.get(n) == k for n in names)]
    if not t and len(have) > 1:
        count = lambda k: sum(1 for n in names if types.get(n) == k)
        opts = [None] + have
        t = opts[select("Which database type?", ["All types"] + [pretty(k, "x").split()[0] for k in have],
                        [f"{len(names)} databases"] + [f"{count(k)} databases" for k in have], back="cancel")]
    picked = [n for n in names if not t or types.get(n) == t]
    if not picked:
        die(f"no {t} databases in this selection")
    return picked


def cmd_backup(a):
    if a.all:
        names = [x["name"] for x in load()]
    elif a.bulk:
        names = [x["name"] for x in load() if x.get("bulk")]
        if not names and interactive():
            print(c(YELLOW, "! ") + "no databases marked for bulk backup yet — choose them now")
            cmd_mark(argparse.Namespace(names=[], off=False))
            names = [x["name"] for x in load() if x.get("bulk")]
        if not names:
            die("no databases marked for bulk backup — run `dbb mark`")
    else:
        names = a.names
    if (a.all or a.bulk) and (a.type or interactive()):
        names = filter_type(names, a.type)
    if not names:
        names = [pick_db("Backup which database?")["name"]]
    if a.output and len(names) > 1:
        die("-o only works with a single database")
    if len(names) == 1:
        db = get_db(names[0])
        out, el = backup(db, a.output, a.docker)
        box("✓ Backup complete", [("database", f"{db['name']} ({db['type']})"),
                                  ("file", str(out)), ("size", human(out.stat().st_size)), ("time", el)])
        return

    # several databases: one failure must not stop the rest
    done, failed, t0 = [], [], time.time()
    for n in names:
        try:
            out, el = backup(get_db(n), None, a.docker)
        except SystemExit as e:
            print(c(RED, "✗ ") + c(BOLD, n.ljust(18)) + " " + str(e.code).split("✗ ")[-1])
            failed.append(n)
            continue
        done.append(out)
        ok(f"{c(BOLD, n.ljust(18))} {human(out.stat().st_size):>9}  {c(DIM, el)}  {c(DIM, out.name)}")
    print()
    rows = [("ok", f"{len(done)}/{len(names)}"), ("size", human(sum(p.stat().st_size for p in done))),
            ("time", f"{time.time() - t0:.1f}s")] + ([("failed", ", ".join(failed))] if failed else [])
    box("✓ Bulk backup complete" if not failed else "! Bulk backup finished with errors", rows,
        color=GREEN if not failed else RED)
    if failed:
        sys.exit(1)


def cmd_restore(a):
    if a.file:
        path = Path(a.file).expanduser()
    else:
        path = pick_backup("Restore which backup?")
    if not path.exists():
        die(f"{path} not found")
    kind = file_type(path)
    check_gzip(path)

    db = get_db(a.to) if a.to else pick_db(f"Restore into which {kind} database?",
                                           [x for x in load() if x["type"] == kind])
    if db["type"] != kind:
        die(f"{path.name} is a {kind} backup but {db['name']} is {db['type']}")

    with Spinner(f"connecting to {c(BOLD, db['name'])}"):
        _, exists, err = probe(db, a.docker)
    if err:
        die(f"cannot connect to {db['name']}: {err}")
    skip_safety = a.no_safety_backup or not exists  # nothing to back up when the database is new

    print()
    box("! DANGER · full restore", [
        ("target", f"{db['name']} → {db['database']} @ {db['host']}:{db['port']}"),
        ("action", "DROP database, recreate, load backup" if exists else "CREATE new database, load backup"),
        ("source", path.name),
        ("safety", "skipped" if a.no_safety_backup else "backup current target first" if exists
                   else "skipped (database does not exist yet)"),
    ], color=RED if exists else YELLOW)
    if a.dry_run:
        info("dry-run: nothing changed")
        return
    if not a.yes and ask(f"type {c(RED, db['database'])} to continue") != db["database"]:
        die("aborted")
    if not skip_safety:
        try:
            out, el = backup(db, new_backup_path(db, "_prerestore"), a.docker)
            ok(f"safety backup {c(DIM, out.name)} ({human(out.stat().st_size)}, {el})")
        except SystemExit:
            # e.g. a broken view in the target makes mysqldump stop (error 1356)
            if not interactive() or ask(f"safety backup failed; restore {c(RED, db['database'])} "
                                        "WITHOUT a backup of the current data? [y/N]").lower() != "y":
                die("aborted, target unchanged (or pass --no-safety-backup)")
    el = restore(db, path, a.docker)
    box("✓ Restore complete", [("source", path.name), ("target", f"{db['name']} ({db['database']})"),
                               ("time", el)])


# ---------- backups browser ----------

BACKUP_RE = re.compile(r"^(?P<db>.+?)(?P<pre>_prerestore)?_(?P<ts>\d{8}_\d{6})(?:_\d+)?\.(?:pg|my)\.sql\.gz$")


def tilde(p):
    home = str(Path.home())
    return "~" + str(p)[len(home):] if str(p).startswith(home) else str(p)


def backup_rows(name=None):
    """Backup files (newest first) with the database they belong to."""
    rows = []
    for p in backups():
        m = BACKUP_RE.match(p.name)
        st = p.stat()
        r = {"path": p, "db": m["db"] if m else "?", "pre": bool(m and m["pre"]), "size": st.st_size, "mtime": st.st_mtime}
        if not name or r["db"] == name:
            rows.append(r)
    return rows


def file_label(r):
    tag = "  (prerestore)" if r["pre"] else ""  # plain text: labels are measured/clipped by the selector
    return f"{r['path'].name:<52} {human(r['size']):>9}  {ago(r['mtime']):>9}{tag}"


def open_path(p):
    opener = "open" if platform.system() == "Darwin" else "xdg-open"
    if not shutil.which(opener):
        die(f"cannot open folders here; path: {p}")
    subprocess.run([opener, str(p)])


def show_contents(path):
    """Page the SQL inside a backup (less), or print the first lines when no pager."""
    pager = os.environ.get("PAGER", "less")
    if interactive() and shutil.which(pager.split()[0]):
        p = subprocess.Popen(pager.split() + (["-S"] if pager == "less" else []), stdin=subprocess.PIPE)
        try:
            with gzip.open(path, "rb") as f:
                shutil.copyfileobj(f, p.stdin)
        except BrokenPipeError:  # user quit the pager early
            pass
        finally:
            try:
                p.stdin.close()
            except BrokenPipeError:
                pass
            p.wait()
    else:
        with gzip.open(path, "rt", errors="replace") as f:
            for _, line in zip(range(60), f):
                print(line, end="")


def prune(keep, name=None, yes=False):
    """Delete all but the newest `keep` backups per database (prerestore files counted separately)."""
    groups = {}
    for r in backup_rows(name):
        groups.setdefault((r["db"], r["pre"]), []).append(r)
    victims = [r for g in groups.values() for r in g[keep:]]
    if not victims:
        ok(f"nothing to delete (every database has ≤ {keep} backups)")
        return
    for r in victims:
        print(c(RED, "  − ") + file_label(r))
    total = human(sum(r["size"] for r in victims))
    if not yes and ask(f"delete {len(victims)} files ({total})? [y/N]").lower() != "y":
        die("aborted")
    for r in victims:
        r["path"].unlink()
    ok(f"deleted {len(victims)} old backups, freed {total}")


def print_backups(name=None):
    rows = backup_rows(name)
    print(c(DIM, f"location: {tilde(BACKUP_DIR)}"))
    if not rows:
        print(c(DIM, "no backups yet — run `dbb backup`"))
        return
    groups = {}
    for r in rows:
        groups.setdefault(r["db"], []).append(r)
    for db, rs in sorted(groups.items()):
        print()
        print(f"{c(ACCENT, '●')} {c(BOLD, db)}  " + c(DIM, f"{len(rs)} files · {human(sum(r['size'] for r in rs))}"
                                                           f" · latest {ago(rs[0]['mtime'])}"))
        for r in rs:
            print("   " + file_label(r))
    print(c(DIM, f"\n{len(rows)} files · {human(sum(r['size'] for r in rows))}"))


def browse_backups(name=None):
    """Interactive: pick a backup, then restore / view / reveal / delete it."""
    ns = argparse.Namespace
    while True:
        rows = backup_rows(name)
        extra = ["⌂  Open backup folder", "✂  Prune old backups (keep newest N per database)"]
        labels = extra + [file_label(r) for r in rows]
        print(c(DIM, f"{len(rows)} files · {human(sum(r['size'] for r in rows))} in {tilde(BACKUP_DIR)}"))
        try:
            i = select("Backups (newest first)", labels)
        except KeyboardInterrupt:
            return
        print()
        if i == 0:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            open_path(BACKUP_DIR)
            continue
        if i == 1:
            keep = ask("keep how many per database?", "5")
            if not keep.isdigit() or int(keep) < 1:
                die("enter a number ≥ 1")
            prune(int(keep), name)
            continue
        r = rows[i - len(extra)]
        actions = [("↓  Restore", "drop a target database and load this file"),
                   ("≡  View contents", "page through the SQL (q to exit)"),
                   ("⌂  Show in folder", tilde(r["path"].parent)),
                   ("−  Delete", human(r["size"]))]
        try:
            j = select(r["path"].name, [x[0] for x in actions], [x[1] for x in actions])
        except KeyboardInterrupt:
            continue
        print()
        if j == 0:
            return cmd_restore(ns(file=str(r["path"]), to=None, yes=False, dry_run=False,
                                  no_safety_backup=False, docker=False))
        if j == 1:
            show_contents(r["path"])
        elif j == 2:
            subprocess.run(["open", "-R", str(r["path"])]) if platform.system() == "Darwin" else open_path(r["path"].parent)
        elif ask(f"delete {r['path'].name}? [y/N]").lower() == "y":
            r["path"].unlink()
            ok(f"deleted {r['path'].name}")


def cmd_backups(a):
    if a.open:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        return open_path(BACKUP_DIR)
    if a.prune is not None:
        return prune(a.prune, a.name, a.yes)
    if interactive() and not a.list:
        return browse_backups(a.name)
    print_backups(a.name)


# ---------- settings ----------

def set_backup_dir(value, move=None):
    """Save a new backup location (None/"default" resets). move: True/False, or None to ask."""
    global BACKUP_DIR
    new = (DEFAULT_BACKUP_DIR if value in (None, "", "default") else Path(value).expanduser()).resolve()
    try:
        new.mkdir(parents=True, exist_ok=True)
        probe_file = new / ".dbb-write-test"
        probe_file.write_text("ok")
        probe_file.unlink()
    except OSError as e:
        die(f"cannot use {new}: {e}")

    old, files = BACKUP_DIR, backups()
    if files and new.resolve() != old.resolve():
        if move is None:
            move = interactive() and ask(f"move {len(files)} existing backups "
                                         f"({human(sum(p.stat().st_size for p in files))}) to {tilde(new)}? [y/N]").lower() == "y"
        if move:
            with Spinner(f"moving backups to {tilde(new)}"):
                for f in files:
                    shutil.move(str(f), str(new / f.name))
            ok(f"moved {len(files)} backups")
        else:
            info(f"existing backups stay in {tilde(old)}")

    settings = load_settings()
    if new == DEFAULT_BACKUP_DIR.resolve():
        settings.pop("backup_dir", None)
    else:
        settings["backup_dir"] = str(new)
    HOME.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    BACKUP_DIR = new
    ok(f"backups will be saved to {c(BOLD, tilde(new))}")
    if os.environ.get("DBB_BACKUP_DIR"):
        print(c(YELLOW, "! ") + f"DBB_BACKUP_DIR is set ({os.environ['DBB_BACKUP_DIR']}) and overrides this setting")


def show_settings():
    src = "env DBB_BACKUP_DIR" if os.environ.get("DBB_BACKUP_DIR") else \
        ("settings" if load_settings().get("backup_dir") else "default")
    box("Settings", [("backups", f"{tilde(BACKUP_DIR)}  ({src})"), ("databases", tilde(CONFIG)),
                     ("settings", tilde(SETTINGS))], color=ACCENT)


def cmd_config(a):
    if a.key is None:
        return show_settings()
    if a.key != "backup-dir":
        die(f"unknown setting {a.key!r} (available: backup-dir)")
    if a.value is None:
        print(BACKUP_DIR)
        return
    set_backup_dir(a.value, True if a.move else (False if a.no_move else None))


def settings_menu():
    while True:
        show_settings()
        items = [("⇢  Change backup location", tilde(BACKUP_DIR)), ("↺  Reset to default", tilde(DEFAULT_BACKUP_DIR))]
        try:
            i = select("Settings", [x[0] for x in items], [x[1] for x in items])
        except KeyboardInterrupt:
            return
        print()
        if i == 0:
            path = ask("new backup folder (e.g. /Volumes/Drive/db-backups)")
            if path:
                set_backup_dir(path)
        else:
            set_backup_dir("default")
        print()


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
    bulk = sum(1 for x in dbs if x.get("bulk"))
    print(f"  {c(ACCENT, '●')} {c(BOLD, str(len(dbs)))} databases {c(DIM, f'({bulk} bulk)')}   "
          f"{c(ACCENT, '●')} {c(BOLD, str(len(files)))} backups {c(DIM, human(sum(p.stat().st_size for p in files)))}   "
          f"{c(DIM, 'pg')} {mark(have('postgres'))}  {c(DIM, 'mysql')} {mark(have('mysql'))}  "
          f"{c(DIM, 'docker')} {mark(docker)}")
    print("  " + c(DIM, "─" * min(cols() - 4, 64)))
    print()


def menu():
    ns = argparse.Namespace
    items = [
        ("↑  Backup", "dump one database", lambda: cmd_backup(ns(names=[], all=False, bulk=False, output=None, docker=False, type=None))),
        ("⇈  Bulk backup", "dump every database marked ◉", lambda: cmd_backup(
            ns(names=[], all=False, bulk=True, output=None, docker=False, type=None))),
        ("◉  Bulk set", "choose which databases bulk backup includes", lambda: cmd_mark(ns(names=[], off=False))),
        ("↓  Restore", "drop target & load a backup", lambda: cmd_restore(
            ns(file=None, to=None, yes=False, dry_run=False, no_safety_backup=False, docker=False))),
        ("▤  Backups", "browse, view, restore or delete backup files", lambda: browse_backups()),
        ("≡  Databases", "show saved connections", lambda: cmd_list(None)),
        ("✓  Check", "test connections & detect server versions", lambda: cmd_check(ns(names=[], docker=False))),
        ("+  Add", "save a new connection", lambda: cmd_add(ns(env=None))),
        ("✎  Edit", "change host, port, password, group, ...", lambda: cmd_edit(ns(name=None))),
        ("⇣  Add from .env", "read connection from a .env file", lambda: cmd_add(ns(env=ask(".env file", ".env")))),
        ("−  Remove", "delete a saved connection", lambda: cmd_remove(ns(name=None))),
        ("⇥  Export", "write database list to JSON", lambda: cmd_export(
            ns(file=ask("export to", "dbb-databases.json")))),
        ("⇤  Import", "merge database list from JSON", lambda: cmd_import(ns(file=ask("import from"), replace=False))),
        ("⚙  Settings", "backup location", settings_menu),
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
    s = sub.add_parser("backups", help="list / browse backup files")
    s.add_argument("name", nargs="?", help="only backups of this database")
    s.add_argument("--list", action="store_true", help="print list instead of the interactive browser")
    s.add_argument("--open", action="store_true", help="open the backup folder")
    s.add_argument("--prune", type=int, metavar="N", help="delete all but the newest N backups per database")
    s.add_argument("-y", "--yes", action="store_true", help="skip confirmation (with --prune)")
    s.set_defaults(fn=cmd_backups)

    s = sub.add_parser("config", help="show or change settings, e.g. `dbb config backup-dir /Volumes/Drive/bk`")
    s.add_argument("key", nargs="?", help="backup-dir")
    s.add_argument("value", nargs="?", help="new value (`default` resets)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--move", action="store_true", help="move existing backups to the new location")
    g.add_argument("--no-move", action="store_true", help="leave existing backups where they are")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("mark", help="choose databases included in `backup --bulk`")
    s.add_argument("names", nargs="*", help="database names (empty = interactive checklist)")
    s.add_argument("--off", action="store_true", help="unmark the given databases")
    s.set_defaults(fn=cmd_mark)
    s = sub.add_parser("check", help="test connection & detect server version")
    s.add_argument("names", nargs="*", help="saved database names (empty = all)")
    s.add_argument("--docker", action="store_true", help="force docker even if local tools exist")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("edit", help="edit a saved database (host, password, group, ...)")
    s.add_argument("name", nargs="?"); s.set_defaults(fn=cmd_edit)

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
    s.add_argument("--bulk", action="store_true", help="backup databases marked for bulk backup (see: dbb mark)")
    s.add_argument("--type", choices=list(DEFAULTS), help="with --all/--bulk: only postgres or only mysql")
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
