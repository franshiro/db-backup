# dbb

CLI to back up and restore PostgreSQL / MySQL / MariaDB databases.
Uses the locally installed client tools when available, otherwise runs them in Docker
with an image matched to the server version.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/franshiro/db-backup/main/install.sh | sh
# or into another directory
curl -fsSL https://raw.githubusercontent.com/franshiro/db-backup/main/install.sh | sh -s -- --prefix /usr/local/bin
```

Installs to `~/.local/bin/dbb`. Requires only `python3` (3.8+), plus Docker if the
`pg_dump`/`psql` or `mysqldump`/`mysql` clients are not installed.

## Usage

```bash
dbb                       # interactive menu
dbb add                   # add a connection: pick a group (e.g. MES) + stage LOCAL/DEV/STAGING/PRODUCTION
dbb add --env [FILE|DIR]  # add connections found in a .env file; lines starting with # are ignored
dbb edit [NAME]           # change a saved connection (host, port, password, group, stage, ...)
dbb list | check | remove
dbb backup [NAME...] [--all | --bulk] [--type postgres|mysql]
dbb mark [NAME...] [--off]  # choose databases for `backup --bulk` (no names = checklist)
dbb restore [FILE] [--to NAME] [--dry-run]
dbb export FILE | import FILE [--replace]
dbb backups [NAME] [--list | --open | --prune N]   # browse, view, restore, delete backup files
dbb config backup-dir PATH|default [--move]       # where backups are saved
dbb update | uninstall
```

Restore is a **full restore**: the target database is dropped, recreated and loaded from
the backup. A safety backup of the target is taken first (`--no-safety-backup` to skip).

Data lives in `~/.dbb/` (`databases.json`, `settings.json`, `backups/`). Change the backup
location with `dbb config backup-dir` or the Settings menu (`DBB_BACKUP_DIR` env overrides it). The database list contains
passwords in plain text and is created with mode 600 — do not commit exported lists.

## Test

```bash
python3 test_env.py
```
