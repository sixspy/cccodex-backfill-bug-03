from app.db import get_db


def hang_window(table_name: str, start: str, end: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        existing = conn.execute("SELECT * FROM window_registry").fetchall()
        overlap = False
        for e in existing:
            if not (end < e["start_date"] or start > e["end_date"]):
                overlap = True
                break
        # silent: ui_only never blocks write
        mode = conn.execute("SELECT value FROM meta WHERE key='overlap_check'").fetchone()
        if overlap and mode and mode["value"] == "hard_unique":
            return {"ok": False, "error": "日界相交", "chip": "拒登"}
        conn.execute(
            "INSERT INTO window_registry(table_name, start_date, end_date) VALUES (?,?,?)",
            (table_name, start, end),
        )
        if overlap:
            conn.execute(
                "INSERT INTO conflict_queue(note, accepted) VALUES (?,1)",
                (f"相交仍挂上 {start}~{end}",),
            )
        return {"ok": True, "overlap": overlap, "chip": "已挂" if overlap else "正常"}


def panels(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        wins = [dict(r) for r in conn.execute("SELECT * FROM window_registry ORDER BY id").fetchall()]
        q = [dict(r) for r in conn.execute("SELECT * FROM conflict_queue ORDER BY id DESC").fetchall()]
        live = conn.execute("SELECT value FROM meta WHERE key='day_bound_live'").fetchone()
        gate = conn.execute("SELECT value FROM meta WHERE key='day_bound_gate'").fetchone()
        return {
            "queue": q,
            "windows": wins,
            "card": {"live": live["value"] if live else "", "gate": gate["value"] if gate else ""},
            "waterline": {"overlap_rows": sum(1 for w in wins for x in wins if w["id"] < x["id"] and not (w["end_date"] < x["start_date"] or w["start_date"] > x["end_date"]))},
        }
