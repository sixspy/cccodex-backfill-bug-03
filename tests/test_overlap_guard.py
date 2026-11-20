import sqlite3

import pytest

from app.db import connect, init_db
from app.services import overlap as ov
from app.services.errors import ServiceError
from app.services.windows import plan_slices, replay_slice


def _registry_count(db_path: str) -> int:
    with connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM window_registry").fetchone()[
            "c"
        ]


def test_overlapping_hang_rejected_hard_unique(tmp_path):
    db = str(tmp_path / "t.db")
    # 种子库存：dw_orders_d 2026-11-10 ~ 2026-11-12
    r1 = ov.hang_window("dw_a", "2026-11-20", "2026-11-22", db, mode="hard_unique")
    assert r1["ok"] is True

    r2 = ov.hang_window("dw_b", "2026-11-21", "2026-11-25", db, mode="hard_unique")
    assert r2["ok"] is False
    assert r2["chip"] == "拒登"
    assert r2["error"] == "日界相交，拒登"
    # 相交窗不得留在库存
    assert _registry_count(db) == 2  # 种子 1 条 + 成功 1 条

    panels = ov.panels(db)
    assert panels["waterline"]["overlap_rows"] == 0
    assert panels["queue"] == []


def test_edge_touching_day_is_intersection(tmp_path):
    """擦边共日：新窗开始日 == 库存结束日，按日界相交拦截。"""
    db = str(tmp_path / "t.db")
    r = ov.hang_window("dw_edge", "2026-11-12", "2026-11-13", db, mode="hard_unique")
    assert r["ok"] is False
    assert r["chip"] == "拒登"
    assert _registry_count(db) == 1


def test_adjacent_noneday_gap_allowed(tmp_path):
    """不共日的相邻窗（开始日 = 库存结束次日）应放行。"""
    db = str(tmp_path / "t.db")
    r = ov.hang_window("dw_next", "2026-11-13", "2026-11-14", db, mode="hard_unique")
    assert r["ok"] is True
    assert _registry_count(db) == 2


def test_conflict_ledger_records_then_rolls_back(tmp_path):
    db = str(tmp_path / "t.db")
    r = ov.hang_window("dw_b", "2026-11-11", "2026-11-13", db, mode="conflict_ledger")
    assert r["ok"] is False
    assert r["chip"] == "拒登"
    # 选边差异：账册留一笔拒登，但库存窗行整笔撤回
    assert _registry_count(db) == 1
    with connect(db) as conn:
        q = conn.execute("SELECT * FROM conflict_queue").fetchall()
        assert len(q) == 1
        assert dict(q[0])["rejected"] == 1
        assert dict(q[0])["accepted"] == 0


def test_mode_switch_persists_and_ui_only_normalized(tmp_path):
    db = str(tmp_path / "t.db")
    ov.set_mode("conflict_ledger", db)
    assert ov.get_mode(db) == "conflict_ledger"
    # 历史 ui_only 归一为硬约束，不允许“只提示不拦写”
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('overlap_check','ui_only') "
            "ON CONFLICT(key) DO UPDATE SET value='ui_only'"
        )
    assert ov.get_mode(db) == "hard_unique"
    with pytest.raises(ServiceError):
        ov.set_mode("ui_only", db)


def test_day_bounds_same_source_and_gate_blocks(tmp_path):
    db = str(tmp_path / "t.db")
    panels = ov.panels(db)
    # 种子结束日 11-12 → 双界同源派生
    assert panels["card"]["live"] == "2026-11-12"
    assert panels["card"]["gate"] == "2026-11-13"
    assert panels["card"]["aligned"] is True

    # 门槛内（start <= live）即拦
    blocked = ov.hang_window("dw_x", "2026-11-12", "2026-11-12", db)
    assert blocked["ok"] is False

    ok = ov.hang_window("dw_x", "2026-11-13", "2026-11-14", db)
    assert ok["ok"] is True
    # 挂后双界仍同源
    assert ok["day_bound_live"] == "2026-11-14"
    assert ok["day_bound_gate"] == "2026-11-15"


def test_concurrent_hang_no_double_write(tmp_path):
    """左右调度同时点挂窗：写锁串行化，库存不得出现双窗同日。"""
    import threading

    db = str(tmp_path / "t.db")
    results = []
    errors = []

    def fire():
        try:
            results.append(
                ov.hang_window(
                    "dw_race", "2026-11-13", "2026-11-15", db, mode="hard_unique"
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=fire)
    t2 = threading.Thread(target=fire)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    assert len(results) == 2
    assert sum(1 for r in results if r["ok"]) == 1
    assert sum(1 for r in results if not r["ok"] and r["chip"] == "拒登") == 1
    # 本地库可核：只有一条新窗行，无叠日
    assert _registry_count(db) == 2
    panels = ov.panels(db)
    assert panels["waterline"]["overlap_rows"] == 0


def test_trigger_blocks_raw_insert(tmp_path):
    """绕过应用层直接写库，触发器照样拦。"""
    db = str(tmp_path / "t.db")
    init_db(db)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(db) as conn:
            conn.execute(
                "INSERT INTO window_registry(table_name,start_date,end_date) "
                "VALUES ('dw_raw','2026-11-11','2026-11-12')"
            )


def test_replay_overlapping_day_blocked(tmp_path):
    """两个窗口含同一日，重放叠日进同一落点必须被拒，水线可验。"""
    db = str(tmp_path / "t.db")
    init_db(db)
    with connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO backfill_windows(table_name,start_date,end_date,status) "
            "VALUES('dw_a','2026-11-20','2026-11-21','草稿')"
        )
        wid_a = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO backfill_windows(table_name,start_date,end_date,status) "
            "VALUES('dw_b','2026-11-21','2026-11-22','草稿')"
        )
        wid_b = cur.lastrowid

    plan_a = plan_slices(wid_a, db)
    plan_b = plan_slices(wid_b, db)
    # 窗口 a 先落 11-21
    slice_a_21 = next(s for s in plan_a["slices"] if s["slice_date"] == "2026-11-21")
    replay_slice(slice_a_21["id"], db)

    # 窗口 b 的 11-21 再重放 → 白话 409
    slice_b_21 = next(s for s in plan_b["slices"] if s["slice_date"] == "2026-11-21")
    with pytest.raises(ServiceError) as exc:
        replay_slice(slice_b_21["id"], db)
    assert exc.value.status_code == 409
    assert "禁止叠日重放" in exc.value.message

    panels = ov.panels(db)
    assert panels["waterline"]["slice_overlap_rows"] == 0

    # 不叠日的切片仍可正常落库，切片重跑不会再叠
    slice_b_22 = next(s for s in plan_b["slices"] if s["slice_date"] == "2026-11-22")
    out = replay_slice(slice_b_22["id"], db)
    assert out["status"] == "已落库"


@pytest.mark.asyncio
async def test_api_hang_reject_chip_and_mode(authed):
    # 种子库存 11-10~11-12，默认硬约束
    r = await authed.post(
        "/api/overlap/hang",
        json={
            "table_name": "dw_api",
            "start_date": "2026-11-11",
            "end_date": "2026-11-12",
        },
    )
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False
    assert body["chip"] == "拒登"

    panels = (await authed.get("/api/overlap/panels")).json()
    assert panels["card"]["aligned"] is True
    assert panels["waterline"]["overlap_rows"] == 0

    # 切到账册模式后再试：仍拒登，队列同亮拒登
    m = await authed.post("/api/overlap/mode", json={"mode": "conflict_ledger"})
    assert m.json()["mode"] == "conflict_ledger"
    r2 = await authed.post(
        "/api/overlap/hang",
        json={
            "table_name": "dw_api",
            "start_date": "2026-11-11",
            "end_date": "2026-11-12",
        },
    )
    assert r2.status_code == 409
    assert r2.json()["chip"] == "拒登"
    panels2 = (await authed.get("/api/overlap/panels")).json()
    assert panels2["queue"][0]["rejected"] == 1
    assert panels2["mode"] == "conflict_ledger"


@pytest.mark.asyncio
async def test_api_invalid_date_returns_plain_message(authed):
    r = await authed.post(
        "/api/overlap/hang",
        json={"table_name": "t", "start_date": "bad", "end_date": "2026-11-12"},
    )
    assert r.status_code == 400
    assert "日期" in r.json()["error"]
