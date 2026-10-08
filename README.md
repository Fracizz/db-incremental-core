# db-incremental-core

**English** | [简体中文](README.zh-CN.md)

Framework-independent MySQL/MariaDB incremental backup and restore planning, extracted from NEMPanel. Supports Python 3.11+ with no runtime dependencies.

## Features and scope

- Parse binlog coordinates from dump snapshots instead of using a later server position as the full-backup baseline.
- Plan a fixed incremental interval `[start, end)` and check log availability, coordinate order, and rotation policy.
- Decide whether to use an incremental or full backup based on chain depth, age, and coordinate availability.
- Validate restore chains: full-backup root, parent relationships, chain IDs, source database, successful status, and continuous coordinates.
- Generate vendor-specific binlog reader arguments for MySQL and MariaDB without connection details or credentials.

The library returns plans and arguments. The calling application handles database connections, archive integrity checks, command execution, authorization, auditing, and task records. The planner does not execute mysqlbinlog or restore SQL.

## Installation

Download the wheel from [Releases](https://github.com/Fracizz/db-incremental-core/releases), then install it:

```bash
python -m pip install --no-index ./db_incremental_core-0.1.1-py3-none-any.whl
```

Alternatively, clone the repository and install from source. Build dependencies use the Aliyun PyPI mirror:

```bash
git clone https://github.com/Fracizz/db-incremental-core.git
cd db-incremental-core
python -m pip install --index-url https://mirrors.aliyun.com/pypi/simple/ .
```

Source code and packages are currently distributed through GitHub. The package is not published on PyPI.

## Incremental backup example

```python
from db_incremental_core import (
    BinlogPosition,
    build_binlog_read_args,
    plan_incremental_backup,
)

plan = plan_incremental_backup(
    BinlogPosition("mysql-bin.000001", 100),
    BinlogPosition("mysql-bin.000001", 200),
    available_files=["mysql-bin.000001"],
    allow_rotation=False,
)
assert plan.files == ("mysql-bin.000001",)

args = build_binlog_read_args("mysql", plan)
assert args == [
    "--read-from-remote-server",
    "--skip-gtids",
    "--start-position=100",
    "--stop-position=200",
    "mysql-bin.000001",
]
```

The caller must supply `available_files` in server binlog order. The planner relies on this list; it does not query the server or independently verify the completeness of its logs.

## Restore chain example

```python
from db_incremental_core import BackupPoint, BinlogPosition, plan_restore

baseline = BinlogPosition("mysql-bin.000001", 100)
full = BackupPoint(
    id=1, kind="full", database="business", parent_id=None,
    start=None, end=baseline, status="success", archive="full.sql.gz",
    chain_id="example-chain",
)
incremental = BackupPoint(
    id=2, kind="incremental", database="business", parent_id=1,
    start=baseline, end=BinlogPosition("mysql-bin.000001", 200),
    status="success", archive="incremental.sql.gz", chain_id="example-chain",
)
restore = plan_restore([full, incremental], selected_id=2, target_database="business")
assert [point.id for point in restore] == [1, 2]
```

`archive` is an archive identifier supplied by the caller. The planner does not inspect archive contents. Before executing a restore, the caller must verify that every archive exists and passes integrity checks.

Incremental ROW events contain the source database name, so incremental chains can only be restored to a database with the same name. Use an empty database on another instance to avoid writing to the source database. Full-only restores may use a different database name.

## Public API

| API | Purpose |
| --- | --- |
| `BinlogPosition` | Immutable binlog filename and nonnegative position |
| `IncrementalPlan` | Start and end coordinates with the required log files |
| `BackupPoint` | Restore node: kind, parent, coordinates, status, archive, and chain ID |
| `IncrementalChainBrokenError` | A `ValueError` subclass raised when incremental continuity cannot be established |
| `parse_snapshot_position(output)` | Parse SOURCE / MASTER coordinates from a snapshot; return `None` when absent |
| `decide_backup_mode(...)` | Return an incremental/full decision and a Chinese explanation; raise a chain error when automatic full fallback is disabled |
| `plan_incremental_backup(...)` | Validate the interval and log list, then return an incremental plan |
| `binlog_reader_flags(db_type)` | Return vendor-specific remote binlog reader flags |
| `build_binlog_read_args(db_type, plan)` | Return reader arguments without credentials |
| `plan_restore(...)` | Validate a restore chain and return its nodes in restore order |
| `positions_contiguous(configs, ...)` | Check coordinate continuity for legacy dictionary records |
| `validate_restore_target(...)` | Reject database renaming for incremental restores |

The conservative policy in `decide_backup_mode` switches to a full backup when the binlog filename changes. Callers that support rotation can use `allow_rotation=True` in the planning functions and must record the complete `files` list for restore nodes spanning multiple logs.

## Development and validation

Run these commands from the repository root:

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# On Windows PowerShell, use .venv\Scripts\Activate.ps1
python -m pip install --index-url https://mirrors.aliyun.com/pypi/simple/ -e '.[dev]'
python -m pytest -q
```

Tests run without database connections or external services. They cover interval validation, vendor-specific arguments, full-backup fallback, a 50-node restore chain, gaps within a chain, and isolation between chains for the same database. Passing unit tests does not establish that backups or restores work against a live database.

Build the wheel and source distribution with uv. The Aliyun dependency index is configured in `pyproject.toml`:

```bash
uv build --wheel --sdist
```

When reporting an issue, include your Python version and a reproducible, sanitized example. Do not submit passwords, tokens, connection strings, or real business data.

## Migrating from v0.1.0

Version 0.1.1 renames the distribution from `nem-db-incremental-core` to `db-incremental-core` and the Python import from `nem_db_incremental_core` to `db_incremental_core`. Update your dependency declarations and imports when upgrading. The planning API and behavior are unchanged.

The existing v0.1.0 release assets retain their original names and checksums.

## License

[MIT](LICENSE) © 2026 Fracizz
