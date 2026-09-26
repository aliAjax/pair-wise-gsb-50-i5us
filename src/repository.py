"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE TABLE IF NOT EXISTS preservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    asset_type TEXT NOT NULL,
                    asset_key TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    duration_days INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    proposed_by TEXT NOT NULL,
                    proposed_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_note TEXT,
                    effective_at TEXT,
                    expires_at TEXT,
                    renewed_count INTEGER NOT NULL DEFAULT 0,
                    closed_by TEXT,
                    closed_at TEXT,
                    close_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_preservations_record ON preservations(record_id, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_preservations_asset_busy ON preservations(asset_type, asset_key) WHERE status IN ('pending','active');
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def get_preservation(self, preservation_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM preservations WHERE id=?", (preservation_id,)).fetchone()
        if row is None:
            raise NotFound("保全记录不存在")
        return dict(row)

    def list_preservations(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM preservations WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def insert_preservation(self, record_id: int, data: Dict[str, Any], max_total: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT id, version FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            holder = connection.execute(
                "SELECT record_id FROM preservations WHERE asset_type=? AND asset_key=? AND status IN ('pending','active')",
                (data["asset_type"], data["asset_key"]),
            ).fetchone()
            if holder is not None:
                connection.rollback()
                raise Conflict("该财产已被案件%s保全占用" % holder["record_id"])
            used = connection.execute(
                "SELECT COALESCE(SUM(amount),0) AS total FROM preservations WHERE record_id=? AND status IN ('pending','active')",
                (record_id,),
            ).fetchone()["total"]
            if round(float(used) + data["amount"], 2) > round(float(max_total), 2):
                connection.rollback()
                raise Conflict("保全金额累计不能超过案件欠缴总额%s" % round(float(max_total), 2))
            try:
                cursor = connection.execute(
                    "INSERT INTO preservations(record_id,asset_type,asset_key,amount,status,duration_days,reason,proposed_by,proposed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (record_id, data["asset_type"], data["asset_key"], data["amount"], "pending", data["duration_days"], data["reason"], actor_id, now, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("该财产已被其他案件保全占用") from exc
            preservation_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    record_id,
                    "preservation_propose",
                    actor_id,
                    int(record_row["version"]),
                    json.dumps(
                        {"summary": "提出税收保全申请", "preservation_id": preservation_id, "asset_type": data["asset_type"], "asset_key": data["asset_key"], "amount": data["amount"], "duration_days": data["duration_days"], "reason": data["reason"]},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM preservations WHERE id=?", (preservation_id,)).fetchone()
            connection.commit()
        return dict(row)

    def review_preservation(self, preservation_id: int, approve: bool, note: str, actor_id: str, now: datetime) -> Dict[str, Any]:
        now_iso = now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM preservations WHERE id=?", (preservation_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("保全记录不存在")
            if row["status"] != "pending":
                connection.rollback()
                raise Conflict("当前状态不允许复核")
            record_row = connection.execute("SELECT payload, version FROM records WHERE id=?", (row["record_id"],)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if approve:
                total_due = float(json.loads(record_row["payload"]).get("total_due", 0.0))
                used = connection.execute(
                    "SELECT COALESCE(SUM(amount),0) AS total FROM preservations WHERE record_id=? AND status IN ('pending','active')",
                    (row["record_id"],),
                ).fetchone()["total"]
                if round(float(used), 2) > round(total_due, 2):
                    connection.rollback()
                    raise Conflict("保全金额累计超过案件欠缴总额%s" % round(total_due, 2))
                expires_at = (now + timedelta(days=int(row["duration_days"]))).isoformat()
                connection.execute(
                    "UPDATE preservations SET status='active',reviewed_by=?,reviewed_at=?,review_note=?,effective_at=?,expires_at=?,updated_at=? WHERE id=?",
                    (actor_id, now_iso, note, now_iso, expires_at, now_iso, preservation_id),
                )
                action = "preservation_approve"
                details = {"summary": "保全复核通过", "preservation_id": preservation_id, "amount": row["amount"], "expires_at": expires_at, "note": note}
            else:
                connection.execute(
                    "UPDATE preservations SET status='rejected',reviewed_by=?,reviewed_at=?,review_note=?,updated_at=? WHERE id=?",
                    (actor_id, now_iso, note, now_iso, preservation_id),
                )
                action = "preservation_reject"
                details = {"summary": "保全复核驳回", "preservation_id": preservation_id, "note": note}
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (row["record_id"], action, actor_id, int(record_row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), now_iso),
            )
            result = connection.execute("SELECT * FROM preservations WHERE id=?", (preservation_id,)).fetchone()
            connection.commit()
        return dict(result)

    def transition_preservation(self, preservation_id: int, allowed_statuses: tuple, updates: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM preservations WHERE id=?", (preservation_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("保全记录不存在")
            if row["status"] not in allowed_statuses:
                connection.rollback()
                raise Conflict("当前状态不允许该操作")
            record_row = connection.execute("SELECT version FROM records WHERE id=?", (row["record_id"],)).fetchone()
            assignments = ",".join("%s=?" % key for key in updates)
            connection.execute(
                "UPDATE preservations SET %s,updated_at=? WHERE id=?" % assignments,
                list(updates.values()) + [now, preservation_id],
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (row["record_id"], action, actor_id, int(record_row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM preservations WHERE id=?", (preservation_id,)).fetchone()
            connection.commit()
        return dict(result)

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
