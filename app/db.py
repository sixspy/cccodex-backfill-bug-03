import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx(db_path: str | None = None):
    """显式写事务：BEGIN IMMEDIATE 立刻拿库级写锁，串行化并发写。

    用于挂窗这类“先查库存再插行”的流程，消除 TOCTOU 双写。
    """
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 后来的写事务排队等锁，而非立刻抛 database is locked
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('草稿','已计划','执行中','已完成')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backfill_slices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  slice_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待重放','已落库','失败')),
  row_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(window_id, slice_date)
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  planned INTEGER NOT NULL,
  loaded INTEGER NOT NULL,
  missing INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS window_registry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '在册'
);

CREATE TABLE IF NOT EXISTS conflict_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  note TEXT NOT NULL,
  accepted INTEGER NOT NULL DEFAULT 1,
  rejected INTEGER NOT NULL DEFAULT 0
);

-- 日界相交硬约束（含擦边共日：a.start <= b.end AND b.start <= a.end 即拦）。
-- 应用层选边的最后一道防线：任何路径都无法把相交窗写进库存。
CREATE TRIGGER IF NOT EXISTS trg_window_no_overlap_insert
BEFORE INSERT ON window_registry
WHEN EXISTS (
  SELECT 1 FROM window_registry r
  WHERE NEW.start_date <= r.end_date AND r.start_date <= NEW.end_date
)
BEGIN
  SELECT RAISE(ABORT, '日界相交，拒登');
END;

CREATE TRIGGER IF NOT EXISTS trg_window_no_overlap_update
BEFORE UPDATE ON window_registry
WHEN EXISTS (
  SELECT 1 FROM window_registry r
  WHERE r.id != NEW.id
    AND NEW.start_date <= r.end_date AND r.start_date <= NEW.end_date
)
BEGIN
  SELECT RAISE(ABORT, '日界相交，拒登');
END;

-- 回灌切片同日落库互斥：同一切片日只许一个窗口落库，杜绝重放叠日同一落点。
CREATE TRIGGER IF NOT EXISTS trg_slice_no_overlap_loaded
BEFORE UPDATE OF status ON backfill_slices
WHEN NEW.status = '已落库'
  AND EXISTS (
    SELECT 1 FROM backfill_slices s
    WHERE s.window_id != NEW.window_id
      AND s.slice_date = NEW.slice_date
      AND s.status = '已落库'
  )
BEGIN
  SELECT RAISE(ABORT, '该日已有窗口落库，禁止叠日重放');
END;

"""


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        # 老库补列：conflict_queue.rejected（拒登账册标记）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(conflict_queue)")}
        if "rejected" not in cols:
            conn.execute(
                "ALTER TABLE conflict_queue ADD COLUMN rejected INTEGER NOT NULL DEFAULT 0"
            )
        # 老库迁移：ui_only（静默放行、勾选不拦写）不再合法，归一为硬约束
        conn.execute(
            "UPDATE meta SET value='hard_unique' "
            "WHERE key='overlap_check' AND value NOT IN ('hard_unique','conflict_ledger')"
        )
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        cur = conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_orders_d','2026-11-10','2026-11-12','草稿')"
        )
        wid = cur.lastrowid
        conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_payments_d','2026-11-11','2026-11-11','草稿')"
        )
        # placeholder so list is non-empty; slices created by plan
        _ = wid
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        # 相交拦截默认硬约束；可切换为 conflict_ledger（旁路账册）。
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('overlap_check','hard_unique')")
        conn.execute("INSERT INTO window_registry(table_name, start_date, end_date) VALUES ('dw_orders_d','2026-11-10','2026-11-12')")
