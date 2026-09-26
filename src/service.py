"""业务用例编排、权限检查与审计。"""
from datetime import date
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        record["preservation"] = self.preservation_summary(record)
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 税收保全 ----

    def propose_preservation(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_preservation_action(actor.role, "propose"):
            raise PermissionDenied("角色无权提出税收保全")
        record = self.repository.get(record_id)
        if record["state"] == "closed":
            raise Conflict("案件已结案，不能采取保全措施")
        fields = self.rules.validate_preservation_proposal(data or {})
        total_due = float(record["payload"].get("total_due", 0) or 0)
        return self.repository.create_preservation(record_id, fields, total_due, self.rules.today().isoformat(), actor.user_id)

    def get_preservation(self, actor: Actor, order_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_preservation(order_id, self.rules.today().isoformat())

    def list_preservations(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.list_preservations(record_id, self.rules.today().isoformat())

    def act_preservation(self, actor: Actor, order_id: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if action not in ("review", "renew", "release", "convert"):
            raise ValidationError("未知的保全操作：%s" % action)
        if not self.rules.role_can_preservation_action(actor.role, action):
            raise PermissionDenied("角色无权执行该保全操作")
        today = self.rules.today().isoformat()
        data = data or {}
        if action == "review":
            outcome, note = self.rules.validate_preservation_review(data)
            order = self.repository.get_preservation(order_id, today)
            record = self.repository.get(order["record_id"])
            total_due = float(record["payload"].get("total_due", 0) or 0)
            return self.repository.review_preservation(order_id, today, outcome, note, total_due, actor.user_id)
        if action == "renew":
            order = self.repository.get_preservation(order_id, today)
            new_expiry, reason = self.rules.validate_preservation_renewal(order, data)
            return self.repository.renew_preservation(order_id, today, new_expiry, reason, actor.user_id)
        if action == "release":
            reason = self.rules.validate_preservation_release(data)
            return self.repository.release_preservation(order_id, today, reason, actor.user_id)
        note = self.rules.validate_preservation_convert(data)
        return self.repository.convert_preservation(order_id, today, note, actor.user_id)

    def preservation_summary(self, record: Dict[str, Any]) -> Dict[str, Any]:
        today = self.rules.today()
        orders = self.repository.list_preservations(record["id"], today.isoformat())
        occupied = round(sum(float(o["amount"]) for o in orders if o["status"] == "active"), 2)
        pending = round(sum(float(o["amount"]) for o in orders if o["status"] == "pending"), 2)
        converted = round(sum(float(o["amount"]) for o in orders if o["status"] == "converted"), 2)
        total_due = round(float(record["payload"].get("total_due", 0) or 0), 2)
        todos: List[Dict[str, Any]] = []
        for order in orders:
            label = "%s(%s)" % (self.rules.target_type_label(order["target_type"]), order["target_key"])
            if order["status"] == "pending":
                todos.append({"type": "review", "order_id": order["id"], "message": "保全申请待复核：%s" % label})
            elif order["status"] == "expired":
                todos.append({"type": "release", "order_id": order["id"], "message": "保全已到期，待办理解除：%s" % label})
            elif order["status"] == "active" and (date.fromisoformat(order["expires_on"]) - today).days <= 7:
                todos.append({"type": "renew", "order_id": order["id"], "message": "保全将于%s到期，需续保或解除：%s" % (order["expires_on"], label)})
        return {
            "total_due": total_due,
            "occupied_amount": occupied,
            "pending_amount": pending,
            "converted_total": converted,
            "available_amount": round(total_due - occupied - pending, 2),
            "orders": orders,
            "todos": todos,
        }
