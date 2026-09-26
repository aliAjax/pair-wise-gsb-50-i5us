import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'taxpayer': 'Star Ltd', 'tax_period': '2025-Q4', 'declared_tax': 500000.0, 'assessed_tax': 760000.0, 'penalty_rate': 0.2, 'evidence_count': 4, 'days_late': 90, 'appeal_deadline_day': 60}
TOTAL_DUE = 323700.0
INSPECTOR = Actor("investigator", "inspector")
REVIEWER = Actor("leader", "reviewer")
FLOW = [('investigate', 'inspector', {'plan': '核对账簿'}), ('propose', 'inspector', {'proposal': '补税并处罚'}), ('review', 'reviewer', {'outcome': 'accepted', 'review_note': '证据充分'}), ('close', 'reviewer', {'final_decision': '维持处理'})]


class PreservationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.today = [date(2026, 9, 26)]
        self.service = build_service(str(Path(self.temp.name) / "test.db"), clock=lambda: self.today[0])
        self.record = self.service.create(INSPECTOR, "TAX-26001", CREATE_DATA)

    def tearDown(self):
        self.temp.cleanup()

    def _propose(self, record_id=None, **overrides):
        data = {'target_type': 'bank_account', 'target_key': '6222020200112233', 'amount': 100000.0, 'duration_days': 30, 'reason': '发现转移财产迹象'}
        data.update(overrides)
        return self.service.propose_preservation(INSPECTOR, record_id or self.record["id"], data)

    def _approve(self, order_id):
        return self.service.act_preservation(REVIEWER, order_id, "review", {"outcome": "approved", "note": "同意保全"})

    def _timeline_actions(self):
        return [event["action"] for event in self.service.timeline(INSPECTOR, self.record["id"])]

    def test_propose_then_review_takes_effect(self):
        order = self._propose()
        self.assertEqual(order["status"], "pending")
        detail = self.service.get_record(INSPECTOR, self.record["id"])
        self.assertEqual(detail["preservation"]["occupied_amount"], 0)
        self.assertEqual(detail["preservation"]["pending_amount"], 100000.0)
        self.assertEqual(detail["preservation"]["todos"][0]["type"], "review")
        reviewed = self._approve(order["id"])
        self.assertEqual(reviewed["status"], "active")
        self.assertEqual(reviewed["reviewed_by"], "leader")
        detail = self.service.get_record(INSPECTOR, self.record["id"])
        self.assertEqual(detail["preservation"]["occupied_amount"], 100000.0)
        self.assertEqual(detail["preservation"]["available_amount"], TOTAL_DUE - 100000.0)
        actions = self._timeline_actions()
        self.assertIn("preservation_propose", actions)
        self.assertIn("preservation_review", actions)

    def test_same_target_cannot_be_occupied_twice(self):
        self._propose()
        other = self.service.create(INSPECTOR, "TAX-26002", dict(CREATE_DATA, taxpayer="Moon Ltd"))
        with self.assertRaises(Conflict):
            self._propose(record_id=other["id"])
        with self.assertRaises(Conflict):
            self._propose()

    def test_target_freed_after_release_and_record_kept(self):
        order = self._approve(self._propose()["id"])
        released = self.service.act_preservation(INSPECTOR, order["id"], "release", {"reason": "风险消除"})
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["release_reason"], "风险消除")
        again = self._propose()
        self.assertEqual(again["status"], "pending")
        detail = self.service.get_record(INSPECTOR, self.record["id"])
        statuses = [o["status"] for o in detail["preservation"]["orders"]]
        self.assertIn("released", statuses)
        self.assertIn("preservation_release", self._timeline_actions())

    def test_amount_capped_by_total_due(self):
        with self.assertRaises(ValidationError):
            self._propose(amount=TOTAL_DUE + 1)
        self._propose(amount=300000.0)
        with self.assertRaises(ValidationError):
            self._propose(target_key="6222020200998877", amount=50000.0)
        ok = self._propose(target_key="6222020200998877", amount=23700.0)
        self.assertEqual(ok["status"], "pending")

    def test_expired_order_cannot_renew_or_convert(self):
        order = self._approve(self._propose(duration_days=2)["id"])
        self.today[0] = self.today[0] + timedelta(days=3)
        detail = self.service.get_record(INSPECTOR, self.record["id"])
        current = [o for o in detail["preservation"]["orders"] if o["id"] == order["id"]][0]
        self.assertEqual(current["status"], "expired")
        self.assertEqual(detail["preservation"]["occupied_amount"], 0)
        self.assertEqual(detail["preservation"]["todos"][0]["type"], "release")
        with self.assertRaises(Conflict):
            self.service.act_preservation(INSPECTOR, order["id"], "renew", {"duration_days": 10, "reason": "尚未办结"})
        with self.assertRaises(Conflict):
            self.service.act_preservation(REVIEWER, order["id"], "convert", {"note": "扣划"})
        released = self.service.act_preservation(REVIEWER, order["id"], "release", {"reason": "到期解除"})
        self.assertEqual(released["status"], "released")
        actions = self._timeline_actions()
        self.assertIn("preservation_expire", actions)
        self.assertIn("preservation_release", actions)

    def test_renew_extends_expiry_and_traces(self):
        order = self._approve(self._propose(duration_days=5)["id"])
        with self.assertRaises(ValidationError):
            self.service.act_preservation(INSPECTOR, order["id"], "renew", {"duration_days": 3, "reason": "太短"})
        renewed = self.service.act_preservation(INSPECTOR, order["id"], "renew", {"duration_days": 10, "reason": "案件未办结"})
        self.assertEqual(renewed["renewed_count"], 1)
        self.assertEqual(renewed["expires_on"], (self.today[0] + timedelta(days=10)).isoformat())
        self.assertIn("preservation_renew", self._timeline_actions())

    def test_convert_to_deduction(self):
        order = self._approve(self._propose()["id"])
        converted = self.service.act_preservation(REVIEWER, order["id"], "convert", {"note": "依法扣划"})
        self.assertEqual(converted["status"], "converted")
        detail = self.service.get_record(INSPECTOR, self.record["id"])
        self.assertEqual(detail["preservation"]["occupied_amount"], 0)
        self.assertEqual(detail["preservation"]["converted_total"], 100000.0)
        again = self._propose()
        self.assertEqual(again["status"], "pending")
        self.assertIn("preservation_convert", self._timeline_actions())

    def test_review_reject_frees_target(self):
        order = self._propose()
        rejected = self.service.act_preservation(REVIEWER, order["id"], "review", {"outcome": "rejected", "note": "证据不足"})
        self.assertEqual(rejected["status"], "rejected")
        again = self._propose()
        self.assertEqual(again["status"], "pending")

    def test_permissions(self):
        order = self._propose()
        with self.assertRaises(PermissionDenied):
            self.service.act_preservation(INSPECTOR, order["id"], "review", {"outcome": "approved", "note": "越权"})
        with self.assertRaises(PermissionDenied):
            self.service.propose_preservation(Actor("rep", "taxpayer_rep"), self.record["id"], {})
        with self.assertRaises(PermissionDenied):
            self.service.act_preservation(Actor("rep", "taxpayer_rep"), order["id"], "release", {"reason": "越权"})

    def test_closed_case_cannot_propose(self):
        record = self.record
        for action, role, data in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        with self.assertRaises(Conflict):
            self._propose()

    def test_target_key_validation(self):
        with self.assertRaises(ValidationError):
            self._propose(target_key="abc")
        vehicle = self._propose(target_type="vehicle", target_key="lsvaa2180a2109714")
        self.assertEqual(vehicle["target_key"], "LSVAA2180A2109714")
        estate = self._propose(target_type="real_estate", target_key="京(2026)朝阳区不动产权第0123456号")
        self.assertEqual(estate["status"], "pending")


if __name__ == "__main__":
    unittest.main()
