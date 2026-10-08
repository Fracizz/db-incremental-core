"""只规划备份区间和还原链；数据库连接、存储、执行和审计由宿主实现。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import pairwise


class IncrementalChainBrokenError(ValueError):
    """binlog 链无法证明连续，不能继续增量。"""


@dataclass(frozen=True)
class BinlogPosition:
    file: str
    position: int

    def __post_init__(self) -> None:
        if not self.file or not isinstance(self.position, int) or self.position < 0:
            raise ValueError("binlog 坐标必须包含文件名和非负位置")


@dataclass(frozen=True)
class IncrementalPlan:
    start: BinlogPosition
    end: BinlogPosition
    files: tuple[str, ...]


@dataclass(frozen=True)
class BackupPoint:
    id: int
    kind: str
    database: str
    parent_id: int | None
    start: BinlogPosition | None
    end: BinlogPosition | None
    status: str
    archive: str | None
    files: tuple[str, ...] = ()
    chain_id: str | None = None


def parse_snapshot_position(output: str | None) -> BinlogPosition | None:
    """只接受导出快照中的 SOURCE/MASTER 坐标，不读取事后的服务端位置。"""
    text = output or ""
    file_match = re.search(r"(?:SOURCE|MASTER)_LOG_FILE\s*=\s*'([^']+)'", text, re.IGNORECASE)
    pos_match = re.search(r"(?:SOURCE|MASTER)_LOG_POS\s*=\s*(\d+)", text, re.IGNORECASE)
    if not file_match or not pos_match:
        return None
    return BinlogPosition(file_match.group(1), int(pos_match.group(1)))


def plan_incremental_backup(
    start: BinlogPosition,
    end: BinlogPosition,
    *,
    available_files: Sequence[str],
    allow_rotation: bool,
) -> IncrementalPlan:
    """固定本次 [start, end) 区间；先证明日志存在且顺序正确。"""
    files = tuple(available_files)
    if start.file not in files or end.file not in files:
        raise IncrementalChainBrokenError("binlog 链已断裂或日志已被清理")
    first, last = files.index(start.file), files.index(end.file)
    if last < first or (first == last and end.position < start.position):
        raise IncrementalChainBrokenError("binlog 坐标倒退，必须重新执行全量备份")
    if first != last and not allow_rotation:
        raise IncrementalChainBrokenError(f"检测到 binlog 轮转({start.file} → {end.file})")
    return IncrementalPlan(start, end, files[first:last + 1])


def build_binlog_read_args(db_type: str, plan: IncrementalPlan) -> list[str]:
    """返回与厂商匹配的读取参数；连接和凭据由宿主装配。"""
    family = db_type.lower()
    if family not in {"mysql", "mariadb"}:
        raise ValueError(f"不支持的数据库类型用于 binlog 增量: {db_type}")
    return [
        *binlog_reader_flags(db_type),
        f"--start-position={plan.start.position}",
        f"--stop-position={plan.end.position}",
        *plan.files,
    ]


def binlog_reader_flags(db_type: str) -> list[str]:
    """MySQL 与 MariaDB 的远程读取参数不同。"""
    family = db_type.lower()
    if family not in {"mysql", "mariadb"}:
        raise ValueError(f"不支持的数据库类型用于 binlog 增量: {db_type}")
    return ["--read-from-remote-server" if family == "mysql" else "--read-from-remote-master",
            *(["--skip-gtids"] if family == "mysql" else [])]


def positions_contiguous(configs: Sequence[dict], *, allow_rotation: bool = False) -> bool:
    """兼容旧记录的 binlog_file 字段，校验相邻节点和区间完整性。"""
    for previous, current in pairwise(configs):
        prev_file = previous.get("end_file") or previous.get("binlog_file")
        start_file = current.get("start_file") or current.get("binlog_file")
        end_file = current.get("end_file") or current.get("binlog_file")
        try:
            prev_pos = int(previous["end_pos"])
            start_pos = int(current["start_pos"])
            end_pos = int(current["end_pos"])
        except (KeyError, TypeError, ValueError):
            return False
        if not prev_file or not start_file or not end_file or prev_file != start_file or prev_pos != start_pos:
            return False
        if end_file != start_file:
            if not allow_rotation:
                return False
        elif end_pos < start_pos:
            return False
    return True


def decide_backup_mode(
    requested_incremental: bool,
    *,
    previous_end: BinlogPosition | None,
    current: BinlogPosition | None,
    depth: int = 0,
    chain_started_at: datetime | None = None,
    now: datetime | None = None,
    max_increment_count: int = 24,
    max_chain_age_hours: int = 24,
    auto_full_when_chain_broken: bool = True,
    has_previous: bool = True,
) -> tuple[bool, str]:
    """决定全量或增量；宿主仍负责查询历史记录及执行任务。"""
    if not requested_incremental:
        return False, "未启用增量备份，执行全量"
    if not has_previous:
        return False, "无成功全量根节点，本次自动转全量"
    if depth >= max_increment_count:
        return False, "已达到连续增量次数上限，本次转全量"
    if chain_started_at:
        current_time = now or datetime.now(timezone.utc)
        started = chain_started_at.replace(tzinfo=timezone.utc) if chain_started_at.tzinfo is None else chain_started_at
        current_time = current_time.replace(tzinfo=timezone.utc) if current_time.tzinfo is None else current_time
        if current_time - started >= timedelta(hours=max_chain_age_hours):
            return False, "增量链已达到最长时间，本次转全量"
    reason = None
    if previous_end is None:
        reason = "上一条记录缺少完整 end 坐标"
    elif current is None:
        reason = "无法读取当前 binlog 坐标"
    elif previous_end.file != current.file:
        reason = f"检测到 binlog 轮转({previous_end.file} → {current.file})"
    elif current.position < previous_end.position:
        reason = "检测到 binlog 坐标倒退"
    if reason:
        if not auto_full_when_chain_broken:
            raise IncrementalChainBrokenError(f"{reason}，策略禁止自动转全量")
        return False, f"{reason}，本次转全量"
    return True, "执行增量备份"


def plan_restore(
    chain: Sequence[BackupPoint], *, selected_id: int, target_database: str,
    allow_rotation: bool = False,
) -> tuple[BackupPoint, ...]:
    """校验完整恢复链，调用者须先校验归档实物，再处理目标数据库。"""
    if not chain or chain[0].kind != "full":
        raise ValueError("增量恢复点缺少完整全量备份")
    if chain[0].parent_id is not None or len({point.id for point in chain}) != len(chain):
        raise ValueError("增量恢复链根节点或记录 ID 不合法")
    if chain[-1].id != selected_id:
        raise ValueError("增量恢复链未到达所选恢复点")
    source_database = chain[0].database
    root_chain_id = chain[0].chain_id or None
    for index, point in enumerate(chain):
        # 同库的多条全量链可拥有相同坐标；显式链 ID 必须一致，不能只凭父 ID 和坐标拼接。
        if (point.chain_id or None) != root_chain_id:
            raise ValueError("增量恢复链 ID 不一致")
        if point.status != "success" or not point.archive:
            raise ValueError("增量恢复链包含未成功或缺少归档的节点")
        if point.database != source_database:
            raise ValueError("增量恢复链包含其他数据库")
        if index == 0:
            continue
        previous = chain[index - 1]
        if point.kind != "incremental" or point.parent_id != previous.id:
            raise ValueError("增量恢复链父子关系不连续")
        if not previous.end or not point.start or not point.end or previous.end != point.start:
            raise ValueError("增量恢复链 binlog 坐标不连续")
        if point.end.file != point.start.file and not allow_rotation:
            raise ValueError("增量恢复链包含不支持的 binlog 轮转")
        if point.end.file != point.start.file and (not point.files or point.files[0] != point.start.file
                                                    or point.files[-1] != point.end.file):
            raise ValueError("增量恢复链缺少跨文件 binlog 清单")
        if point.end.file == point.start.file and point.end.position < point.start.position:
            raise ValueError("增量恢复链 binlog 坐标倒退")
    validate_restore_target(source_database, target_database, incremental=len(chain) > 1)
    return tuple(chain)


def validate_restore_target(source_database: str, target_database: str, *, incremental: bool) -> None:
    """ROW 事件内嵌源库名；增量链改名重放会误写源库。"""
    if incremental and target_database != source_database:
        raise ValueError("增量备份仅支持还原到同名数据库，请选择其他实例上的同名空库")
