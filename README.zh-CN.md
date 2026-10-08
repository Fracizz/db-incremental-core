# db-incremental-core

[English](README.md) | **简体中文**

MySQL / MariaDB 增量备份与还原链的规划和校验库，从 NEMPanel（NEM运维平台）提取。支持 Python 3.11+，运行时仅依赖标准库。

## 能力与边界

- 从导出快照文本解析 binlog 坐标，避免使用事后查询的位置充当全量基线。
- 固定增量区间 `[start, end)`，检查日志是否存在、坐标是否倒退及是否允许轮转。
- 按增量次数、链龄和坐标状态决定执行增量或自动转全量。
- 校验恢复链的全量根节点、父子关系、链 ID、源库、成功状态和连续坐标。
- 为 MySQL / MariaDB 生成各自的 binlog 读取参数，不包含连接信息或凭据。

本库返回规划数据和参数，不连接数据库、不执行 mysqlbinlog、不读取归档、不还原 SQL。数据库连接、归档完整性检查、命令执行、权限、审计和任务记录由调用方负责。

## 安装

从本仓库的 [Releases](https://github.com/Fracizz/db-incremental-core/releases) 下载 wheel 后安装：

```bash
python -m pip install --no-index ./db_incremental_core-0.1.1-py3-none-any.whl
```

也可以克隆源码后安装。构建依赖使用阿里云源：

```bash
git clone https://github.com/Fracizz/db-incremental-core.git
cd db-incremental-core
python -m pip install --index-url https://mirrors.aliyun.com/pypi/simple/ .
```

当前通过 GitHub 发布源码与制品，未发布到 PyPI。

## 增量规划示例

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

`available_files` 必须由调用方提供，并按服务端 binlog 顺序排列；规划器以这份清单为依据，不自行查询或证明服务端日志完整性。

## 恢复链示例

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

`archive` 仅表示调用方保存的归档标识，规划器不检查归档实物。执行恢复前，调用方必须检查每份归档的存在性和完整性。

增量 ROW 事件内嵌源库名，因此增量链只允许还原到同名数据库；调用方应选择其他实例上的同名空库，避免误写源库。仅全量恢复允许指定其他库名。

## 对外接口

| 接口 | 用途 |
| --- | --- |
| `BinlogPosition` | 不可变的 binlog 文件名和非负位置 |
| `IncrementalPlan` | 起点、终点与所需日志文件清单 |
| `BackupPoint` | 恢复节点：类型、父节点、坐标、状态、归档与链 ID |
| `IncrementalChainBrokenError` | 无法证明增量链连续时抛出的 `ValueError` 子类 |
| `parse_snapshot_position(output)` | 解析快照里的 SOURCE / MASTER 坐标；缺失返回 `None` |
| `decide_backup_mode(...)` | 返回是否执行增量及中文原因；禁止自动转全量时可抛出断链异常 |
| `plan_incremental_backup(...)` | 检查区间和日志清单，返回增量计划 |
| `binlog_reader_flags(db_type)` | 返回厂商对应的远程读取参数 |
| `build_binlog_read_args(db_type, plan)` | 返回不包含凭据的读取参数列表 |
| `plan_restore(...)` | 校验恢复链，返回按恢复顺序排列的节点元组 |
| `positions_contiguous(configs, ...)` | 兼容旧字典记录的坐标连续性检查 |
| `validate_restore_target(...)` | 拒绝增量恢复时改名目标库 |

`decide_backup_mode` 的保守策略遇到 binlog 文件变化会转全量；需要允许轮转的调用方可使用规划函数的 `allow_rotation=True`，同时为跨文件恢复节点保存完整 `files` 清单。

## 开发与验证

在本仓库根目录执行：

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell 使用 .venv\Scripts\Activate.ps1
python -m pip install --index-url https://mirrors.aliyun.com/pypi/simple/ -e '.[dev]'
python -m pytest -q
```

测试不连接数据库或外部服务，覆盖区间校验、厂商参数、自动转全量策略、50 节点恢复链、中间断链及同库多链隔离。单元测试结果不代表真实数据库备份或恢复验收。

使用 uv 构建 wheel 和源码包（`pyproject.toml` 已配置阿里云依赖源）：

```bash
uv build --wheel --sdist
```

报告问题时请提供 Python 版本和可复现的脱敏样例，不提交密码、Token、连接串或真实业务数据。

## 许可证

[MIT](LICENSE) © 2026 Fracizz
