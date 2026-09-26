"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound, ValidationError


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
                    target_type TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    proposed_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_note TEXT,
                    expires_on TEXT NOT NULL,
                    renewed_count INTEGER NOT NULL DEFAULT 0,
                    released_by TEXT,
                    released_at TEXT,
                    release_reason TEXT,
                    converted_by TEXT,
                    converted_at TEXT,
                    convert_note TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_preservations_record ON preservations(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_preservations_live_target ON preservations(target_type, target_key) WHERE status IN ('pending','active');
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

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ---- 税收保全 ----

    def _preservation_audit(self, connection: sqlite3.Connection, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, int(row["version"]) if row else 0, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
        )

    def _sweep_expired_locked(self, connection: sqlite3.Connection, today: str) -> None:
        """把已过期的生效中保全置为expired并留痕，需在事务内调用。"""
        rows = connection.execute(
            "SELECT id, record_id, target_type, target_key, amount, version FROM preservations WHERE status='active' AND expires_on<?",
            (today,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE preservations SET status='expired', version=?, updated_at=? WHERE id=?",
                (int(row["version"]) + 1, _now(), row["id"]),
            )
            self._preservation_audit(connection, row["record_id"], "system", "preservation_expire", {
                "summary": "保全期限届满，措施失效",
                "order_id": row["id"],
                "target_type": row["target_type"],
                "target_key": row["target_key"],
                "amount": row["amount"],
            })

    @staticmethod
    def _locked_preservation(connection: sqlite3.Connection, order_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM preservations WHERE id=?", (order_id,)).fetchone()
        if row is None:
            connection.rollback()
            raise NotFound("保全记录不存在")
        return row

    @staticmethod
    def _require_preservation_status(connection: sqlite3.Connection, row: sqlite3.Row, allowed, action: str) -> None:
        if row["status"] in allowed:
            return
        connection.rollback()
        if row["status"] == "expired":
            raise Conflict("保全已到期，不能%s" % {"renew": "续保", "convert": "转为扣划"}.get(action, action))
        raise Conflict("当前状态不允许执行%s" % action)

    def create_preservation(self, record_id: int, fields: Dict[str, Any], total_due: float, today: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired_locked(connection, today)
            clash = connection.execute(
                "SELECT id, record_id FROM preservations WHERE target_type=? AND target_key=? AND status IN ('pending','active')",
                (fields["target_type"], fields["target_key"]),
            ).fetchone()
            if clash is not None:
                connection.rollback()
                raise Conflict("该财产标的已被案件#%s保全占用" % clash["record_id"])
            row = connection.execute(
                "SELECT COALESCE(SUM(amount),0) AS occupied FROM preservations WHERE record_id=? AND status IN ('pending','active')",
                (record_id,),
            ).fetchone()
            occupied = round(float(row["occupied"]) + float(fields["amount"]), 2)
            if occupied > round(float(total_due), 2):
                connection.rollback()
                raise ValidationError("保全金额合计%s超过案件欠缴总额%s" % (occupied, round(float(total_due), 2)))
            try:
                cursor = connection.execute(
                    "INSERT INTO preservations(record_id,target_type,target_key,amount,status,reason,proposed_by,expires_on,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (record_id, fields["target_type"], fields["target_key"], fields["amount"], "pending", fields["reason"], actor_id, fields["expires_on"], 1, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("该财产标的已被其他案件保全占用") from exc
            order_id = int(cursor.lastrowid)
            self._preservation_audit(connection, record_id, actor_id, "preservation_propose", {
                "summary": "提出税收保全申请",
                "order_id": order_id,
                "input": fields,
            })
            result = connection.execute("SELECT * FROM preservations WHERE id=?", (order_id,)).fetchone()
            connection.commit()
        return dict(result)

    def get_preservation(self, order_id: int, today: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired_locked(connection, today)
            row = connection.execute("SELECT * FROM preservations WHERE id=?", (order_id,)).fetchone()
            connection.commit()
        if row is None:
            raise NotFound("保全记录不存在")
        return dict(row)

    def list_preservations(self, record_id: int, today: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired_locked(connection, today)
            rows = connection.execute("SELECT * FROM preservations WHERE record_id=? ORDER BY id DESC", (record_id,)).fetchall()
            connection.commit()
        return [dict(row) for row in rows]

    def review_preservation(self, order_id: int, today: str, outcome: str, note: str, total_due: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired_locked(connection, today)
            row = self._locked_preservation(connection, order_id)
            self._require_preservation_status(connection, row, ("pending",), "review")
            if outcome == "approved":
                occupied_row = connection.execute(
                    "SELECT COALESCE(SUM(amount),0) AS occupied FROM preservations WHERE record_id=? AND status IN ('pending','active') AND id<>?",
                    (row["record_id"], order_id),
                ).fetchone()
                occupied = round(float(occupied_row["occupied"]) + float(row["amount"]), 2)
                if occupied > round(float(total_due), 2):
                    connection.rollback()
                    raise ValidationError("保全金额合计%s超过案件欠缴总额%s" % (occupied, round(float(total_due), 2)))
            status = "active" if outcome == "approved" else "rejected"
            connection.execute(
                "UPDATE preservations SET status=?, reviewed_by=?, reviewed_at=?, review_note=?, version=?, updated_at=? WHERE id=?",
                (status, actor_id, now, note, int(row["version"]) + 1, now, order_id),
            )
            self._preservation_audit(connection, row["record_id"], actor_id, "preservation_review", {
                "summary": "保全复核%s" % ("通过，措施生效" if outcome == "approved" else "驳回"),
                "order_id": order_id,
                "outcome": outcome,
                "note": note,
                "target_type": row["target_type"],
                "target_key": row["target_key"],
                "amount": row["amount"],
            })
            result = connection.execute("SELECT * FROM preservations WHERE id=?", (order_id,)).fetchone()
            connection.commit()
        return dict(result)

    def renew_preservation(self, order_id: int, today: str, new_expiry: str, reason: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired_locked(connection, today)
            row = self._locked_preservation(connection, order_id)
            self._require_preservation_status(connection, row, ("active",), "renew")
            connection.execute(
                "UPDATE preservations SET expires_on=?, renewed_count=?, version=?, updated_at=? WHERE id=?",
                (new_expiry, int(row["renewed_count"]) + 1, int(row["version"]) + 1, now, order_id),
            )
            self._preservation_audit(connection, row["record_id"], actor_id, "preservation_renew", {
                "summary": "保全续保",
                "order_id": order_id,
                "from_expires_on": row["expires_on"],
                "to_expires_on": new_expiry,
                "reason": reason,
            })
            result = connection.execute("SELECT * FROM preservations WHERE id=?", (order_id,)).fetchone()
            connection.commit()
        return dict(result)

    def release_preservation(self, order_id: int, today: str, reason: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired_locked(connection, today)
            row = self._locked_preservation(connection, order_id)
            self._require_preservation_status(connection, row, ("active", "expired"), "release")
            connection.execute(
                "UPDATE preservations SET status='released', released_by=?, released_at=?, release_reason=?, version=?, updated_at=? WHERE id=?",
                (actor_id, now, reason, int(row["version"]) + 1, now, order_id),
            )
            self._preservation_audit(connection, row["record_id"], actor_id, "preservation_release", {
                "summary": "解除税收保全",
                "order_id": order_id,
                "from_status": row["status"],
                "reason": reason,
                "target_type": row["target_type"],
                "target_key": row["target_key"],
                "amount": row["amount"],
            })
            result = connection.execute("SELECT * FROM preservations WHERE id=?", (order_id,)).fetchone()
            connection.commit()
        return dict(result)

    def convert_preservation(self, order_id: int, today: str, note: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired_locked(connection, today)
            row = self._locked_preservation(connection, order_id)
            self._require_preservation_status(connection, row, ("active",), "convert")
            connection.execute(
                "UPDATE preservations SET status='converted', converted_by=?, converted_at=?, convert_note=?, version=?, updated_at=? WHERE id=?",
                (actor_id, now, note, int(row["version"]) + 1, now, order_id),
            )
            self._preservation_audit(connection, row["record_id"], actor_id, "preservation_convert", {
                "summary": "保全转为扣划",
                "order_id": order_id,
                "note": note,
                "target_type": row["target_type"],
                "target_key": row["target_key"],
                "amount": row["amount"],
            })
            result = connection.execute("SELECT * FROM preservations WHERE id=?", (order_id,)).fetchone()
            connection.commit()
        return dict(result)

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
