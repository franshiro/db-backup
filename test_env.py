"""Self-check for .env parsing and dump filtering. Run: python3 test_env.py"""
import tempfile
from pathlib import Path

import io

from dbb import NoGtid, env_candidates, read_env

ENV = """
# Laravel style; commented lines are inactive
APP_NAME=mes
DB_CONNECTION=pgsql
#DB_HOST=old-server
DB_HOST=10.0.0.5
DB_PORT=5433
DB_DATABASE=mes_dev
DB_USERNAME=mes
DB_PASSWORD="p@ss#word"   # quoted, inline comment
# DATABASE_URL=postgres://ignored:x@nowhere/ignored
REPORT_DATABASE_URL=mysql://rep:s%40cret@db.local:3307/reports?ssl=true
export PGHOST=pg.local
PGUSER=${DB_USERNAME}
PGDATABASE=analytics
REDIS_HOST=127.0.0.1
REDIS_DB=0
MAIL_HOST=smtp
MAIL_USERNAME=x
"""

with tempfile.TemporaryDirectory() as d:
    f = Path(d) / ".env"
    f.write_text(ENV)
    env = read_env(f)

assert "DATABASE_URL" not in env, "commented line must be ignored"
assert env["DB_HOST"] == "10.0.0.5", "active line wins over commented one"
assert env["DB_PASSWORD"] == "p@ss#word"
assert env["PGUSER"] == "mes", "${VAR} reference"

found = {f["src"]: f for f in env_candidates(env)}
assert set(found) == {"DB_*", "REPORT_DATABASE_URL", "PG_*"}, found.keys()

db = found["DB_*"]
assert (db["type"], db["host"], db["port"], db["user"], db["password"], db["database"]) == \
    ("postgres", "10.0.0.5", 5433, "mes", "p@ss#word", "mes_dev")

rep = found["REPORT_DATABASE_URL"]
assert (rep["type"], rep["host"], rep["port"], rep["user"], rep["password"], rep["database"]) == \
    ("mysql", "db.local", 3307, "rep", "s@cret", "reports")

pg = found["PG_*"]
assert (pg["type"], pg["host"], pg["port"], pg["user"], pg["database"]) == \
    ("postgres", "pg.local", 5432, "mes", "analytics")

dump = (b"SET @@SESSION.SQL_LOG_BIN= 0;\n"
        b"SET @@GLOBAL.GTID_PURGED=/*!80000 '+'*/ 'a:1-5,\n"
        b"b:1-9';\n"
        b"CREATE TABLE t (x int);\n"
        b"SET @@GLOBAL.GTID_PURGED='c:1';\n"
        b"INSERT INTO t VALUES (1);\n")
out = b"".join(iter(NoGtid(io.BytesIO(dump)).read, b""))
assert out == b"SET @@SESSION.SQL_LOG_BIN= 0;\nCREATE TABLE t (x int);\nINSERT INTO t VALUES (1);\n", out

print("ok")
