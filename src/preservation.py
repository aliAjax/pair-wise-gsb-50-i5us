"""税收保全：财产标的、复核生效、唯一占用与到期规则。"""
from datetime import datetime, timezone
from typing import Any, Dict, List

from .domain import Conflict, ValidationError, boolean, choice, integer, number, optional_text, text


ASSET_TYPES = ["bank_account", "real_estate", "vehicle"]
ASSET_LABELS = {"bank_account": "银行账户", "real_estate": "不动产权证", "vehicle": "车辆车架号"}

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_REJECTED = "rejected"
STATUS_RELEASED = "released"
STATUS_SEIZED = "seized"
OCCUPYING_STATUSES = (STATUS_PENDING, STATUS_ACTIVE)

MAX_DURATION_DAYS = 180
MAX_RENEW_DAYS = 180

ACTION_ROLES = {
    "propose": {"inspector"},
    "review": {"reviewer"},
    "renew": {"inspector"},
    "release": {"reviewer"},
    "seize": {"reviewer"},
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def is_expired(preservation: Dict[str, Any], now: datetime) -> bool:
    return preservation["status"] == STATUS_ACTIVE and parse_time(preservation["expires_at"]) <= now


class PreservationRules:
    ACTION_ROLES = ACTION_ROLES

    def role_can(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_propose(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "asset_type": choice(data, "asset_type", ASSET_TYPES),
            "asset_key": text(data, "asset_key"),
            "amount": round(number(data, "amount", 0.01), 2),
            "duration_days": integer(data, "duration_days", 1, MAX_DURATION_DAYS),
            "reason": text(data, "reason"),
        }

    def validate_review(self, data: Dict[str, Any]) -> Dict[str, Any]:
        approve = boolean(data, "approve")
        note = optional_text(data, "note")
        if not approve and not note:
            raise ValidationError("驳回必须填写复核意见")
        return {"approve": approve, "note": note}

    def validate_renew(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"extend_days": integer(data, "extend_days", 1, MAX_RENEW_DAYS), "reason": optional_text(data, "reason")}

    def validate_release(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"reason": text(data, "reason")}

    def validate_seize(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"note": optional_text(data, "note")}

    def ensure_case_accepting(self, record: Dict[str, Any]) -> None:
        if record["state"] == "closed":
            raise Conflict("案件已结案，不能新增税收保全")

    def ensure_amount_within_due(self, total_due: float, amount: float) -> None:
        if round(amount, 2) > round(float(total_due), 2):
            raise ValidationError("保全金额%s超过案件欠缴总额%s" % (round(amount, 2), round(float(total_due), 2)))

    def ensure_renewable(self, preservation: Dict[str, Any], now: datetime) -> None:
        if preservation["status"] != STATUS_ACTIVE:
            raise Conflict("当前状态不允许续保")
        if is_expired(preservation, now):
            raise Conflict("保全已到期，不能续保")

    def ensure_seizable(self, preservation: Dict[str, Any], now: datetime) -> None:
        if preservation["status"] != STATUS_ACTIVE:
            raise Conflict("当前状态不允许转为扣划")
        if is_expired(preservation, now):
            raise Conflict("保全已到期，不能转为扣划")

    def ensure_releasable(self, preservation: Dict[str, Any]) -> None:
        if preservation["status"] != STATUS_ACTIVE:
            raise Conflict("当前状态不允许解除")

    def summarize(self, record: Dict[str, Any], preservations: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
        total_due = round(float(record["payload"].get("total_due", 0.0)), 2)
        occupied = 0.0
        pending_amount = 0.0
        expired_amount = 0.0
        todos: List[Dict[str, Any]] = []
        for item in preservations:
            if item["status"] == STATUS_PENDING:
                pending_amount += item["amount"]
                todos.append({"type": "review", "label": "保全待复核", "preservation_id": item["id"], "asset_key": item["asset_key"], "amount": item["amount"]})
            elif item["status"] == STATUS_ACTIVE:
                if is_expired(item, now):
                    expired_amount += item["amount"]
                    todos.append({"type": "release", "label": "保全已到期待解除", "preservation_id": item["id"], "asset_key": item["asset_key"], "amount": item["amount"]})
                else:
                    occupied += item["amount"]
        reserved = occupied + pending_amount + expired_amount
        return {
            "total_due": total_due,
            "occupied_amount": round(occupied, 2),
            "pending_amount": round(pending_amount, 2),
            "expired_amount": round(expired_amount, 2),
            "available_amount": round(max(0.0, total_due - reserved), 2),
            "todos": todos,
        }
