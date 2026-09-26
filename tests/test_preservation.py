import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.preservation import utcnow


CREATE_DATA = {'taxpayer': 'Star Ltd', 'tax_period': '2025-Q4', 'declared_tax': 500000.0, 'assessed_tax': 760000.0, 'penalty_rate': 0.2, 'evidence_count': 4, 'days_late': 90, 'appeal_deadline_day': 60}
TOTAL_DUE = 323700.0
FLOW = [('investigate', 'inspector', {'plan': '核对账簿'}, 'investigating'), ('propose', 'inspector', {'proposal': '补税并处罚'}, 'proposed'), ('review', 'reviewer', {'outcome': 'accepted', 'review_note': '证据充分'}, 'reviewed'), ('close', 'reviewer', {'final_decision': '维持处理'}, 'closed')]
INSPECTOR = Actor('inspector-1', 'inspector')
REVIEWER = Actor('reviewer-1', 'reviewer')


def propose_data(**overrides):
    data = {'asset_type': 'bank_account', 'asset_key': '6222-0001', 'amount': 100000.0, 'duration_days': 30, 'reason': '发现转移财产迹象'}
    data.update(overrides)
    return data


class PreservationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / 'test.db'))
        self.record = self.service.create(INSPECTOR, 'TAX-PR-001', CREATE_DATA)

    def tearDown(self):
        self.temp.cleanup()

    def _active(self, **overrides):
        pres = self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data(**overrides))
        return self.service.review_preservation(REVIEWER, pres['id'], {'approve': True, 'note': '同意保全'})

    def _new_case(self, reference, taxpayer):
        return self.service.create(INSPECTOR, reference, dict(CREATE_DATA, taxpayer=taxpayer))

    def test_propose_review_release_flow(self):
        pres = self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data())
        self.assertEqual(pres['status'], 'pending')
        detail = self.service.get_record(INSPECTOR, self.record['id'])
        self.assertEqual(detail['preservation']['occupied_amount'], 0.0)
        self.assertEqual(detail['preservation']['pending_amount'], 100000.0)
        self.assertEqual(detail['preservation']['todos'][0]['type'], 'review')

        pres = self.service.review_preservation(REVIEWER, pres['id'], {'approve': True, 'note': '同意保全'})
        self.assertEqual(pres['status'], 'active')
        self.assertIsNotNone(pres['expires_at'])
        detail = self.service.get_record(INSPECTOR, self.record['id'])
        self.assertEqual(detail['preservation']['occupied_amount'], 100000.0)
        self.assertEqual(detail['preservation']['available_amount'], TOTAL_DUE - 100000.0)

        pres = self.service.release_preservation(REVIEWER, pres['id'], {'reason': '欠税已缴清'})
        self.assertEqual(pres['status'], 'released')
        detail = self.service.get_record(INSPECTOR, self.record['id'])
        self.assertEqual(detail['preservation']['occupied_amount'], 0.0)
        actions = [event['action'] for event in self.service.timeline(INSPECTOR, self.record['id'])]
        self.assertEqual(actions, ['created', 'preservation_propose', 'preservation_approve', 'preservation_release'])

    def test_renew_and_seize_leave_trail(self):
        pres = self._active()
        pres = self.service.renew_preservation(INSPECTOR, pres['id'], {'extend_days': 15, 'reason': '案件未办结'})
        self.assertEqual(pres['renewed_count'], 1)
        pres = self.service.seize_preservation(REVIEWER, pres['id'], {'note': '划拨入库抵缴欠税'})
        self.assertEqual(pres['status'], 'seized')
        actions = [event['action'] for event in self.service.timeline(INSPECTOR, self.record['id'])]
        self.assertEqual(actions, ['created', 'preservation_propose', 'preservation_approve', 'preservation_renew', 'preservation_seize'])

    def test_expired_blocks_renew_and_seize_but_release_is_kept(self):
        now = utcnow()
        pres = self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data())
        pres = self.service.review_preservation(REVIEWER, pres['id'], {'approve': True}, now=now)
        later = now + timedelta(days=31)
        with self.assertRaises(Conflict):
            self.service.renew_preservation(INSPECTOR, pres['id'], {'extend_days': 10}, now=later)
        with self.assertRaises(Conflict):
            self.service.seize_preservation(REVIEWER, pres['id'], {}, now=later)
        pres = self.service.release_preservation(REVIEWER, pres['id'], {'reason': '到期解除'}, now=later)
        self.assertEqual(pres['status'], 'released')
        timeline = self.service.timeline(INSPECTOR, self.record['id'])
        self.assertEqual(timeline[-1]['action'], 'preservation_release')
        self.assertTrue(timeline[-1]['details']['was_expired'])

    def test_expired_preservation_shows_release_todo(self):
        past = utcnow() - timedelta(days=40)
        pres = self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data())
        self.service.review_preservation(REVIEWER, pres['id'], {'approve': True}, now=past)
        detail = self.service.get_record(INSPECTOR, self.record['id'])
        self.assertEqual(detail['preservation']['occupied_amount'], 0.0)
        self.assertEqual(detail['preservation']['expired_amount'], 100000.0)
        self.assertEqual(detail['preservation']['todos'][0]['type'], 'release')

    def test_same_asset_cannot_be_occupied_by_two_cases(self):
        other = self._new_case('TAX-PR-002', 'Other Ltd')
        self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data())
        with self.assertRaises(Conflict):
            self.service.propose_preservation(INSPECTOR, other['id'], propose_data())
        with self.assertRaises(Conflict):
            self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data())

    def test_release_frees_asset_for_other_case(self):
        pres = self._active()
        self.service.release_preservation(REVIEWER, pres['id'], {'reason': '解除保全'})
        other = self._new_case('TAX-PR-003', 'Third Ltd')
        again = self.service.propose_preservation(INSPECTOR, other['id'], propose_data())
        self.assertEqual(again['status'], 'pending')

    def test_reject_frees_asset(self):
        pres = self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data())
        pres = self.service.review_preservation(REVIEWER, pres['id'], {'approve': False, 'note': '证据不足'})
        self.assertEqual(pres['status'], 'rejected')
        other = self._new_case('TAX-PR-004', 'Fourth Ltd')
        again = self.service.propose_preservation(INSPECTOR, other['id'], propose_data())
        self.assertEqual(again['status'], 'pending')

    def test_amount_cannot_exceed_total_due(self):
        with self.assertRaises(ValidationError):
            self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data(amount=TOTAL_DUE + 0.01))
        self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data(amount=300000.0))
        with self.assertRaises(Conflict):
            self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data(asset_key='6222-0002', amount=30000.0))

    def test_review_rechecks_capacity_after_case_reduction(self):
        pres = self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data(amount=200000.0))
        record = self.service.act(INSPECTOR, self.record['id'], self.record['version'], 'investigate', {'plan': '核对账簿'})
        record = self.service.act(INSPECTOR, record['id'], record['version'], 'propose', {'proposal': '补税并处罚'})
        self.service.act(REVIEWER, record['id'], record['version'], 'review', {'outcome': 'reduced', 'review_note': '部分采纳', 'reduction_pct': 0.5})
        with self.assertRaises(Conflict):
            self.service.review_preservation(REVIEWER, pres['id'], {'approve': True})

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.propose_preservation(Actor('tp', 'taxpayer_rep'), self.record['id'], propose_data())
        pres = self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data())
        with self.assertRaises(PermissionDenied):
            self.service.review_preservation(INSPECTOR, pres['id'], {'approve': True})
        with self.assertRaises(PermissionDenied):
            self.service.release_preservation(INSPECTOR, pres['id'], {'reason': '越权'})

    def test_closed_case_cannot_propose(self):
        record = self.record
        for action, role, data, expected_state in FLOW:
            record = self.service.act(Actor('operator', role), record['id'], record['version'], action, data)
            self.assertEqual(record['state'], expected_state)
        with self.assertRaises(Conflict):
            self.service.propose_preservation(INSPECTOR, record['id'], propose_data())

    def test_invalid_propose_input(self):
        with self.assertRaises(ValidationError):
            self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data(asset_type='gold'))
        with self.assertRaises(ValidationError):
            self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data(duration_days=0))
        with self.assertRaises(ValidationError):
            self.service.propose_preservation(INSPECTOR, self.record['id'], propose_data(asset_key='  '))
