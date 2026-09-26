"""业务用例编排、权限检查与审计。"""
from datetime import timedelta
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .preservation import PreservationRules, is_expired, parse_time, utcnow
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, preservation_rules: PreservationRules = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.preservation_rules = preservation_rules or PreservationRules()

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
        record["preservation"] = self.preservation_rules.summarize(record, self.repository.list_preservations(record_id), utcnow())
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

    def _ensure_preservation_role(self, actor: Actor, action: str) -> None:
        if not self.preservation_rules.role_can(actor.role, action):
            raise PermissionDenied("角色无权执行该保全操作")

    def propose_preservation(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_preservation_role(actor, "propose")
        record = self.repository.get(record_id)
        rules = self.preservation_rules
        rules.ensure_case_accepting(record)
        prepared = rules.validate_propose(data or {})
        total_due = float(record["payload"].get("total_due", 0.0))
        rules.ensure_amount_within_due(total_due, prepared["amount"])
        return self.repository.insert_preservation(record_id, prepared, total_due, actor.user_id)

    def review_preservation(self, actor: Actor, preservation_id: int, data: Dict[str, Any], now=None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_preservation_role(actor, "review")
        prepared = self.preservation_rules.validate_review(data or {})
        return self.repository.review_preservation(preservation_id, prepared["approve"], prepared["note"], actor.user_id, now or utcnow())

    def renew_preservation(self, actor: Actor, preservation_id: int, data: Dict[str, Any], now=None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_preservation_role(actor, "renew")
        prepared = self.preservation_rules.validate_renew(data or {})
        now = now or utcnow()
        preservation = self.repository.get_preservation(preservation_id)
        self.preservation_rules.ensure_renewable(preservation, now)
        new_expiry = (parse_time(preservation["expires_at"]) + timedelta(days=prepared["extend_days"])).isoformat()
        return self.repository.transition_preservation(
            preservation_id,
            ("active",),
            {"expires_at": new_expiry, "renewed_count": int(preservation["renewed_count"]) + 1},
            actor.user_id,
            "preservation_renew",
            {"summary": "保全续保", "preservation_id": preservation_id, "extend_days": prepared["extend_days"], "expires_at": new_expiry, "reason": prepared["reason"]},
        )

    def release_preservation(self, actor: Actor, preservation_id: int, data: Dict[str, Any], now=None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_preservation_role(actor, "release")
        prepared = self.preservation_rules.validate_release(data or {})
        now = now or utcnow()
        preservation = self.repository.get_preservation(preservation_id)
        self.preservation_rules.ensure_releasable(preservation)
        return self.repository.transition_preservation(
            preservation_id,
            ("active",),
            {"status": "released", "closed_by": actor.user_id, "closed_at": now.isoformat(), "close_note": prepared["reason"]},
            actor.user_id,
            "preservation_release",
            {"summary": "解除税收保全", "preservation_id": preservation_id, "reason": prepared["reason"], "was_expired": is_expired(preservation, now)},
        )

    def seize_preservation(self, actor: Actor, preservation_id: int, data: Dict[str, Any], now=None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_preservation_role(actor, "seize")
        prepared = self.preservation_rules.validate_seize(data or {})
        now = now or utcnow()
        preservation = self.repository.get_preservation(preservation_id)
        self.preservation_rules.ensure_seizable(preservation, now)
        return self.repository.transition_preservation(
            preservation_id,
            ("active",),
            {"status": "seized", "closed_by": actor.user_id, "closed_at": now.isoformat(), "close_note": prepared["note"]},
            actor.user_id,
            "preservation_seize",
            {"summary": "保全转为扣划", "preservation_id": preservation_id, "amount": preservation["amount"], "note": prepared["note"]},
        )

    def list_preservations(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        items = self.repository.list_preservations(record_id)
        return {"items": items, "summary": self.preservation_rules.summarize(record, items, utcnow())}

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
