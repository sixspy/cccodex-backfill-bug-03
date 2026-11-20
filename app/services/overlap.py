import sqlite3
from datetime import date, timedelta

from app.db import get_db, init_db, write_tx
from app.services.errors import ServiceError

VALID_MODES = ("hard_unique", "conflict_ledger")
HANG_REJECT_MSG = "日界相交，拒登"


def _ensure_db(db_path: str) -> None:
    init_db(db_path)


def _isodate(value: str, field: str) -> str:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ServiceError(f"{field}须为 YYYY-MM-DD 日期")
    return value


def get_mode(db_path: str | None = None) -> str:
    _ensure_db(db_path)
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='overlap_check'"
        ).fetchone()
        mode = row["value"] if row else "hard_unique"
        # ui_only 等历史值一律按硬约束处理，杜绝“只提示不拦写”
        return mode if mode in VALID_MODES else "hard_unique"


def set_mode(mode: str, db_path: str | None = None) -> str:
    if mode not in VALID_MODES:
        raise ServiceError("落库策略只能是 hard_unique 或 conflict_ledger")
    _ensure_db(db_path)
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('overlap_check', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (mode,),
        )
    return mode


def _record_rejection(note: str, db_path: str | None) -> None:
    """旁路冲突账册：独立短事务先落一笔拒登。"""
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO conflict_queue(note, accepted, rejected) VALUES (?, 0, 1)",
            (note,),
        )


def _day_bounds(conn) -> tuple[str, str]:
    """同源派生双界：都取自 window_registry 最大结束日。

    live = 库存现行日界（最大结束日）；gate = 可挂门槛（次日）。
    库存为空时双界皆空，不设门槛。
    """
    row = conn.execute(
        "SELECT MAX(end_date) AS max_end FROM window_registry"
    ).fetchone()
    live = row["max_end"] if row and row["max_end"] else ""
    if not live:
        return "", ""
    gate = (date.fromisoformat(live) + timedelta(days=1)).isoformat()
    return live, gate


def _precheck(db_path: str | None, start: str, end: str) -> tuple[dict | None, str, str]:
    """只读预检：返回 (命中的库存行 or None, live, gate)。"""
    with get_db(db_path) as conn:
        existing = conn.execute("SELECT * FROM window_registry").fetchall()
        live, gate = _day_bounds(conn)
        hit = next(
            (
                e
                for e in existing
                if not (end < e["start_date"] or start > e["end_date"])
            ),
            None,
        )
        return (dict(hit) if hit else None), live, gate


def hang_window(
    table_name: str,
    start: str,
    end: str,
    db_path: str | None = None,
    mode: str | None = None,
) -> dict:
    _ensure_db(db_path)
    table_name = str(table_name).strip()
    if not table_name:
        raise ServiceError("目标表不能为空")
    start = _isodate(str(start).strip(), "开始日")
    end = _isodate(str(end).strip(), "结束日")
    if end < start:
        raise ServiceError("结束日不能早于开始日")

    chosen = mode or get_mode(db_path)
    if chosen not in VALID_MODES:
        raise ServiceError("落库策略只能是 hard_unique 或 conflict_ledger")

    # 第一步：只读预检。账册策略在拿写锁之前用独立短事务把拒登落账。
    hit, live, gate = _precheck(db_path, start, end)
    pre_reject = hit is not None or (bool(gate) and start <= live)
    if pre_reject and chosen == "conflict_ledger":
        _record_rejection(f"相交拒登 {table_name} {start}~{end}", db_path)

    # 第二步：BEGIN IMMEDIATE 写事务内做权威判定，串行化并发挂窗
    try:
        with write_tx(db_path) as conn:
            existing = conn.execute(
                "SELECT * FROM window_registry"
            ).fetchall()
            locked_hit = next(
                (
                    e
                    for e in existing
                    if not (end < e["start_date"] or start > e["end_date"])
                ),
                None,
            )
            live, gate = _day_bounds(conn)
            locked_reject = locked_hit is not None or (
                bool(gate) and start <= live
            )

            if locked_reject:
                # 不插任何窗行，事务提交（无窗行变更）即整笔撤回开窗
                if chosen == "conflict_ledger" and not pre_reject:
                    # 并发抢锁下预检漏判：账册补记
                    conn.execute(
                        "INSERT INTO conflict_queue(note, accepted, rejected) "
                        "VALUES (?, 0, 1)",
                        (f"相交拒登 {table_name} {start}~{end}",),
                    )
                return {
                    "ok": False,
                    "error": HANG_REJECT_MSG,
                    "chip": "拒登",
                    "rejected": True,
                    "mode": chosen,
                }

            cur = conn.execute(
                "INSERT INTO window_registry(table_name, start_date, end_date) "
                "VALUES (?,?,?)",
                (table_name, start, end),
            )
            live, gate = _day_bounds(conn)
            return {
                "ok": True,
                "overlap": False,
                "chip": "已挂",
                "id": cur.lastrowid,
                "mode": chosen,
                "day_bound_live": live,
                "day_bound_gate": gate,
            }
    except sqlite3.IntegrityError as exc:
        # 触发器兜底（并发下双方应用层判断同时通过时，后写者被硬约束拦下）
        if "日界相交" in str(exc):
            if chosen == "conflict_ledger":
                _record_rejection(
                    f"相交拒登 {table_name} {start}~{end}", db_path
                )
            return {
                "ok": False,
                "error": HANG_REJECT_MSG,
                "chip": "拒登",
                "rejected": True,
                "mode": chosen,
            }
        raise ServiceError(f"挂窗失败：{exc}")


def panels(db_path: str | None = None) -> dict:
    _ensure_db(db_path)
    with get_db(db_path) as conn:
        wins = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM window_registry ORDER BY id"
            ).fetchall()
        ]
        q = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM conflict_queue ORDER BY id DESC"
            ).fetchall()
        ]
        live, gate = _day_bounds(conn)
        mode = get_mode(db_path)

        def _overlaps(a: dict, b: dict) -> bool:
            return not (
                a["end_date"] < b["start_date"]
                or a["start_date"] > b["end_date"]
            )

        registry_overlaps = sum(
            1
            for i, w in enumerate(wins)
            for x in wins[i + 1 :]
            if _overlaps(w, x)
        )
        # 切片重放叠日：同一日两个窗口都已落库
        slice_overlaps = conn.execute(
            "SELECT COUNT(*) AS c FROM ("
            "SELECT slice_date FROM backfill_slices WHERE status='已落库' "
            "GROUP BY slice_date HAVING COUNT(DISTINCT window_id) > 1)"
        ).fetchone()["c"]

        return {
            "queue": q,
            "windows": wins,
            "mode": mode,
            "card": {
                "live": live,
                "gate": gate,
                "aligned": bool(live) and bool(gate),
            },
            "waterline": {
                "overlap_rows": registry_overlaps,
                "slice_overlap_rows": slice_overlaps,
            },
        }
