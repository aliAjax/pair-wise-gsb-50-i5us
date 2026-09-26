"""税务稽查案件与复议流程领域规则与状态转换。"""
import re
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "opened"
CREATE_ROLES = {'inspector'}
ACTION_ROLES = {'investigate': {'inspector'}, 'propose': {'inspector'}, 'review': {'reviewer'}, 'appeal': {'taxpayer_rep'}, 'close': {'reviewer'}}
TRANSITIONS = {'investigate': {'opened': 'investigating'}, 'propose': {'investigating': 'proposed'}, 'review': {'proposed': 'reviewed'}, 'appeal': {'reviewed': 'appealed'}, 'close': {'reviewed': 'closed', 'appealed': 'closed'}}

# 税收保全：标的类型、操作权限与状态
PRESERVATION_TARGET_TYPES = ["bank_account", "real_estate", "vehicle"]
PRESERVATION_TARGET_LABELS = {"bank_account": "银行账户", "real_estate": "不动产", "vehicle": "车辆"}
PRESERVATION_ACTIONS = ["review", "renew", "release", "convert"]
PRESERVATION_ACTION_ROLES = {'propose': {'inspector'}, 'review': {'reviewer'}, 'renew': {'inspector'}, 'release': {'inspector', 'reviewer'}, 'convert': {'reviewer'}}
PRESERVATION_REVIEW_OUTCOMES = ["approved", "rejected"]
PRESERVATION_LIVE_STATUSES = ("pending", "active")
TARGET_KEY_PATTERNS = {
    "bank_account": (re.compile(r"^\d{8,32}$"), "银行账户应为8-32位数字"),
    "real_estate": (re.compile(r"^[0-9A-Za-z一-鿿（）()\-]{4,64}$"), "不动产权证号格式不正确"),
    "vehicle": (re.compile(r"^[A-HJ-NPR-Z0-9]{17}$"), "车辆车架号应为17位字母数字(不含I/O/Q)"),
}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def __init__(self, clock: Callable[[], date] = None) -> None:
        self._clock = clock or date.today

    def today(self) -> date:
        return self._clock()

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "taxpayer")
        text(p, "tax_period")
        number(p, "declared_tax", 0)
        number(p, "assessed_tax", 0)
        number(p, "penalty_rate", 0, 1)
        integer(p, "evidence_count", 0)
        integer(p, "days_late", 0)
        integer(p, "appeal_deadline_day", 1)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        difference = max(0.0, float(p["assessed_tax"]) - float(p["declared_tax"]))
        interest = difference * 0.0005 * int(p["days_late"])
        penalty = difference * float(p["penalty_rate"])
        p["tax_difference"] = round(difference, 2)
        p["interest"] = round(interest, 2)
        p["penalty"] = round(penalty, 2)
        p["total_due"] = round(difference + interest + penalty, 2)
        p["refund_due"] = round(max(0.0, float(p["declared_tax"]) - float(p["assessed_tax"])), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed"} and item["payload"].get("taxpayer") == payload.get("taxpayer") and item["payload"].get("tax_period") == payload.get("tax_period"):
                raise Conflict("同一纳税人同一税期已有未结稽查案件")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "investigate":
            changes["investigation_plan"] = text(data, "plan")
            summary = "进入稽查调查"
        elif action == "propose":
            if int(p["evidence_count"]) <= 0:
                raise ValidationError("没有证据不能提出处理建议")
            changes["proposal"] = text(data, "proposal")
            changes["proposed_amount"] = float(p["total_due"])
            summary = "已提出补税和处罚建议"
        elif action == "review":
            outcome = choice(data, "outcome", ["accepted", "reduced", "remanded"])
            changes["review_outcome"] = outcome
            changes["review_note"] = text(data, "review_note")
            if outcome == "reduced":
                changes["total_due"] = round(float(p["total_due"]) * float(data.get("reduction_pct", 0.5)), 2)
            summary = "复核完成"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["appeal_deadline_day"]):
                raise ValidationError("复议申请超过期限")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "复议申请已受理"
        elif action == "close":
            changes["final_decision"] = text(data, "final_decision")
            summary = "案件已结案"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 税收保全 ----

    def role_can_preservation_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in PRESERVATION_ACTION_ROLES.get(action, set())

    def target_type_label(self, target_type: str) -> str:
        return PRESERVATION_TARGET_LABELS.get(target_type, target_type)

    def resolve_expiry(self, data: Dict[str, Any], base: date = None) -> str:
        base = base or self.today()
        raw = data.get("expires_on")
        if raw is not None and str(raw).strip():
            try:
                expiry = date.fromisoformat(str(raw).strip())
            except ValueError as exc:
                raise ValidationError("expires_on必须是YYYY-MM-DD日期") from exc
        elif data.get("duration_days") is not None:
            expiry = base + timedelta(days=integer(data, "duration_days", 1, 3650))
        else:
            expiry = base + timedelta(days=30)
        if expiry <= base:
            raise ValidationError("保全到期日必须晚于今天")
        return expiry.isoformat()

    def validate_preservation_proposal(self, data: Dict[str, Any]) -> Dict[str, Any]:
        target_type = choice(data, "target_type", PRESERVATION_TARGET_TYPES)
        target_key = text(data, "target_key")
        if target_type == "vehicle":
            target_key = target_key.upper()
        pattern, message = TARGET_KEY_PATTERNS[target_type]
        if not pattern.match(target_key):
            raise ValidationError(message)
        amount = number(data, "amount", 0)
        if amount <= 0:
            raise ValidationError("amount必须大于0")
        return {
            "target_type": target_type,
            "target_key": target_key,
            "amount": round(amount, 2),
            "reason": text(data, "reason"),
            "expires_on": self.resolve_expiry(data),
        }

    def validate_preservation_review(self, data: Dict[str, Any]) -> Tuple[str, str]:
        return choice(data, "outcome", PRESERVATION_REVIEW_OUTCOMES), text(data, "note")

    def validate_preservation_renewal(self, order: Dict[str, Any], data: Dict[str, Any]) -> Tuple[str, str]:
        reason = text(data, "reason")
        new_expiry = self.resolve_expiry(data)
        if new_expiry <= str(order["expires_on"]):
            raise ValidationError("续保后的到期日必须晚于当前到期日")
        return new_expiry, reason

    def validate_preservation_release(self, data: Dict[str, Any]) -> str:
        return text(data, "reason")

    def validate_preservation_convert(self, data: Dict[str, Any]) -> str:
        return optional_text(data, "note")
