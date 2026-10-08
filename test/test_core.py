"""独立增量备份与还原规划；不连接数据库或外部服务。"""
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from nem_db_incremental_core import (
    BackupPoint,
    BinlogPosition,
    IncrementalChainBrokenError,
    build_binlog_read_args,
    decide_backup_mode,
    parse_snapshot_position,
    plan_incremental_backup,
    plan_restore,
    positions_contiguous,
)


def point(id, *, kind="full", parent_id=None, start=None, end=None, database="business"):
    return BackupPoint(id=id, kind=kind, database=database, parent_id=parent_id,
                       start=start, end=end, status="success", archive=f"{id}.tar.gz")


def test_backup_plan_uses_previous_end_and_fixed_current_position():
    plan = plan_incremental_backup(BinlogPosition("bin.1", 100), BinlogPosition("bin.1", 180),
                                   available_files=["bin.1"], allow_rotation=False)
    assert plan.files == ("bin.1",)
    assert plan.start.position == 100
    assert plan.end.position == 180
    assert "--skip-gtids" in build_binlog_read_args("mysql", plan)
    assert "--read-from-remote-master" in build_binlog_read_args("mariadb", plan)


def test_backup_plan_rejects_missing_log_reverse_position_and_disallowed_rotation():
    with pytest.raises(IncrementalChainBrokenError, match="清理"):
        plan_incremental_backup(BinlogPosition("bin.1", 100), BinlogPosition("bin.2", 200),
                                available_files=["bin.2"], allow_rotation=True)
    with pytest.raises(IncrementalChainBrokenError, match="倒退"):
        plan_incremental_backup(BinlogPosition("bin.1", 200), BinlogPosition("bin.1", 100),
                                available_files=["bin.1"], allow_rotation=False)
    with pytest.raises(IncrementalChainBrokenError, match="轮转"):
        plan_incremental_backup(BinlogPosition("bin.1", 100), BinlogPosition("bin.2", 200),
                                available_files=["bin.1", "bin.2"], allow_rotation=False)


def test_backup_mode_handles_chain_threshold_and_missing_coordinates():
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    assert decide_backup_mode(True, previous_end=BinlogPosition("bin.1", 100),
                              current=BinlogPosition("bin.1", 200), depth=1, now=now)[0]
    assert not decide_backup_mode(True, previous_end=BinlogPosition("bin.1", 100),
                                  current=BinlogPosition("bin.1", 200), depth=24, now=now)[0]
    with pytest.raises(IncrementalChainBrokenError):
        decide_backup_mode(True, previous_end=None, current=BinlogPosition("bin.1", 200),
                           depth=1, auto_full_when_chain_broken=False, now=now)


def test_restore_plan_requires_full_root_continuous_successful_chain():
    full = point(1, end=BinlogPosition("bin.1", 100))
    inc = point(2, kind="incremental", parent_id=1, start=BinlogPosition("bin.1", 100),
                end=BinlogPosition("bin.1", 180))
    assert [node.id for node in plan_restore([full, inc], selected_id=2, target_database="business")] == [1, 2]
    with pytest.raises(ValueError, match="坐标"):
        plan_restore([full, point(2, kind="incremental", parent_id=1,
                                  start=BinlogPosition("bin.1", 101), end=BinlogPosition("bin.1", 180))],
                     selected_id=2, target_database="business")
    with pytest.raises(ValueError, match="父子"):
        plan_restore([full, point(2, kind="incremental", parent_id=9,
                                  start=BinlogPosition("bin.1", 100), end=BinlogPosition("bin.1", 180))],
                     selected_id=2, target_database="business")


def test_restore_plan_allows_full_rename_but_rejects_incremental_rename():
    full = point(1, end=BinlogPosition("bin.1", 100))
    assert plan_restore([full], selected_id=1, target_database="other")
    inc = point(2, kind="incremental", parent_id=1, start=BinlogPosition("bin.1", 100),
                end=BinlogPosition("bin.1", 180))
    with pytest.raises(ValueError, match="同名"):
        plan_restore([full, inc], selected_id=2, target_database="other")


def test_restore_plan_checks_status_database_and_cross_file_manifest():
    full = point(1, end=BinlogPosition("bin.1", 100))
    inc = BackupPoint(2, "incremental", "business", 1, BinlogPosition("bin.1", 100),
                      BinlogPosition("bin.2", 200), "success", "2.gz", ("bin.1", "bin.2"))
    assert plan_restore([full, inc], selected_id=2, target_database="business", allow_rotation=True)
    with pytest.raises(ValueError, match="清单"):
        plan_restore([full, BackupPoint(2, "incremental", "business", 1,
                                       inc.start, inc.end, "success", "2.gz")],
                     selected_id=2, target_database="business", allow_rotation=True)
    with pytest.raises(ValueError, match="其他数据库"):
        plan_restore([full, point(2, kind="incremental", parent_id=1, database="other",
                                  start=full.end, end=BinlogPosition("bin.1", 200))],
                     selected_id=2, target_database="business")
    with pytest.raises(ValueError, match="未成功"):
        plan_restore([BackupPoint(1, "full", "business", None, None, full.end, "failure", "1.gz")],
                     selected_id=1, target_database="business")
    with pytest.raises(ValueError, match="根节点"):
        plan_restore([BackupPoint(1, "full", "business", 9, None, full.end, "success", "1.gz")],
                     selected_id=1, target_database="business")


def test_legacy_coordinate_chain_rejects_missing_or_reversed_positions():
    assert positions_contiguous([{"binlog_file": "bin.1", "end_pos": 100},
                                 {"start_file": "bin.1", "start_pos": 100,
                                  "end_file": "bin.1", "end_pos": 200}])
    assert not positions_contiguous([{"end_file": "bin.1"},
                                     {"start_file": "bin.1", "start_pos": 100}])
    assert not positions_contiguous([{"end_file": "bin.1", "end_pos": 100},
                                     {"start_file": "bin.1", "start_pos": 100,
                                      "end_file": "bin.1", "end_pos": 90}])


def test_parse_snapshot_position_supports_mysql_and_mariadb():
    assert parse_snapshot_position("-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='bin.1', SOURCE_LOG_POS=180;") == BinlogPosition("bin.1", 180)
    assert parse_snapshot_position("-- CHANGE MASTER TO MASTER_LOG_FILE='bin.2', MASTER_LOG_POS=4;") == BinlogPosition("bin.2", 4)
    assert parse_snapshot_position("no coordinate") is None


def test_long_restore_chain_finds_middle_gap_and_preserves_selected_point():
    chain = [point(1, end=BinlogPosition("bin.1", 100))]
    for node_id in range(2, 51):
        chain.append(point(node_id, kind="incremental", parent_id=node_id - 1,
                           start=chain[-1].end, end=BinlogPosition("bin.1", 100 + 50 * (node_id - 1))))
    assert len(plan_restore(chain, selected_id=50, target_database="business")) == 50
    assert [item.id for item in plan_restore(chain[:28], selected_id=28, target_database="business")] == list(range(1, 29))

    broken = list(chain)
    broken[27] = replace(broken[27], start=BinlogPosition("bin.1", broken[27].start.position + 1))
    with pytest.raises(ValueError, match="坐标"):
        plan_restore(broken, selected_id=50, target_database="business")
    assert chain[27].start == chain[26].end


def test_multiple_chains_same_database_cannot_be_crossed_even_with_matching_parent_and_position():
    root_a = BackupPoint(1, "full", "business", None, None, BinlogPosition("bin.1", 100),
                         "success", "a-full.gz", chain_id="chain-a")
    delta_a = BackupPoint(2, "incremental", "business", 1, root_a.end, BinlogPosition("bin.1", 150),
                          "success", "a-inc.gz", chain_id="chain-a")
    root_b = BackupPoint(3, "full", "business", None, None, BinlogPosition("bin.1", 100),
                         "success", "b-full.gz", chain_id="chain-b")
    delta_b = BackupPoint(4, "incremental", "business", 3, root_b.end, BinlogPosition("bin.1", 150),
                          "success", "b-inc.gz", chain_id="chain-b")
    assert [item.id for item in plan_restore([root_a, delta_a], selected_id=2, target_database="business")] == [1, 2]
    assert [item.id for item in plan_restore([root_b, delta_b], selected_id=4, target_database="business")] == [3, 4]
    with pytest.raises(ValueError, match="链 ID"):
        plan_restore([root_a, replace(delta_b, parent_id=1)], selected_id=4, target_database="business")
