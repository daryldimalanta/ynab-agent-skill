# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Explicit new receipt split creation; synthetic API only."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from budget_tools.api import ApiError
from budget_tools.state import StateStore, TERMS_VERSION
from tests.test_receipts import proposal
from validation import test_post_implementation_safety as review


class CreateClient(review.ReviewClient):
    def __init__(self):
        super().__init__()
        self.creates = 0
        self.lose_response = False
        self.ids_only = False
        self.fail_create_readback = False

    def create_transaction(self, plan_id, payload):
        self._check_plan(plan_id)
        for field in ("account_id", "date", "amount", "payee_id", "subtransactions"):
            if field not in payload:
                raise ApiError("Synthetic required field missing", status=400)
        if sum(item["amount"] for item in payload["subtransactions"]) != payload["amount"]:
            raise ApiError("Synthetic split imbalance", status=400)
        if payload.get("category_id") is not None or payload.get("approved") is not False:
            raise ApiError("Synthetic split must be initially unapproved", status=400)
        self.creates += 1
        self.writes.append(copy.deepcopy(payload))
        self.transaction = copy.deepcopy(payload)
        self.transaction.update(id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", deleted=False,
                                payee_name="Example Merchant A", account_name="Example Account A")
        # Realistic generated line IDs and reverse order must not break verification.
        for index, item in enumerate(self.transaction["subtransactions"]):
            item.update(id=f"synthetic-generated-{index}", deleted=False, payee_id=None)
        self.transaction["subtransactions"].reverse()
        if self.lose_response:
            raise ApiError("Synthetic create response lost after commit")
        self.readback_failure_pending = self.fail_create_readback
        if self.ids_only:
            return {"data": {"transaction_ids": [self.transaction["id"]]}}
        return {"data": {"transaction": copy.deepcopy(self.transaction)}}


class ReceiptCreateTests(review.OfflineCase):
    run_cli = review.WorkflowAcceptance.run_cli
    apply = review.WorkflowAcceptance.apply

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore(self.root / "state")
        self.client = CreateClient()
        self.store.save_config({"terms_acknowledged_version": TERMS_VERSION,
                                "plan_id": self.client.plan_id})
        self.value = proposal()
        self.value["transaction_id"] = None
        self.value["receipt"]["match_evidence"] = "User explicitly requests a new synthetic entry"
        self.value["allocations"][1]["category"] = "Example Group A: Example Category B"
        self.proposal_path = self.root / "synthetic-proposal.json"

    def draft(self):
        self.proposal_path.write_text(json.dumps(self.value))
        self.proposal_path.chmod(0o600)
        return self.run_cli("transactions", "draft-receipt-create", "--account", "Example Account A",
                            "--payee", "Example Merchant A", "--date", "2001-01-30", "--outflow", "43.25",
                            "--proposal", str(self.proposal_path))

    def draft_id(self):
        code, draft, error = self.draft()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(draft["operation"], "receipt_create")
        self.assertTrue(draft["summary"]["creates_new_transaction"])
        self.assertIsNone(draft["before"])
        self.assertEqual(self.client.creates, 0)
        return draft["id"]

    def test_create_then_verify_then_approve_and_never_replay(self):
        draft_id = self.draft_id()
        code, result, error = self.apply(draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(self.client.creates, 1)
        self.assertFalse(self.client.writes[0]["approved"])
        self.assertEqual(self.client.writes[1], {"approved": True})
        self.assertEqual(self.store.load_draft(draft_id)["transaction_id"], self.client.transaction["id"])
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.client.creates, 1)

    def test_creation_without_approval_stops_after_verified_categorization(self):
        self.value["approve_after_verification"] = False
        code, result, error = self.apply(self.draft_id())
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "verified_categorization")
        self.assertFalse(self.client.transaction["approved"])
        self.assertEqual(len(self.client.writes), 1)

    def test_approval_failure_recovers_without_second_create(self):
        draft_id = self.draft_id()
        self.client.reject_approval = True
        self.assertEqual(self.apply(draft_id)[0], 1)
        code, result, error = self.run_cli("drafts", "reconcile", draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["status"], "approval_pending")
        self.client.reject_approval = False
        code, result, error = self.run_cli("drafts", "resume", draft_id, "--confirm", draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(self.client.creates, 1)

    def test_lost_create_response_without_id_blocks_retry_and_guessing(self):
        draft_id = self.draft_id()
        self.client.lose_response = True
        self.assertEqual(self.apply(draft_id)[0], 1)
        stored = self.store.load_draft(draft_id)
        self.assertEqual(stored["status"], "uncertain")
        self.assertIsNone(stored["transaction_id"])
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.run_cli("drafts", "reconcile", draft_id)[0], 1)
        self.assertEqual(self.client.creates, 1)

    def test_id_only_acknowledgment_survives_failed_readback(self):
        draft_id = self.draft_id()
        self.client.ids_only = True
        self.client.fail_create_readback = True
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.store.load_draft(draft_id)["transaction_id"], self.client.transaction["id"])
        code, result, error = self.run_cli("drafts", "reconcile", draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["status"], "approval_pending")
        self.assertEqual(self.run_cli("drafts", "resume", draft_id, "--confirm", draft_id)[0], 0)
        self.assertEqual(self.client.creates, 1)

    def test_existing_transaction_reference_is_not_a_creation_proposal(self):
        self.value["transaction_id"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        code, _, error = self.draft()
        self.assertEqual(code, 1)
        self.assertIn("transaction_id", error)
        self.assertEqual(self.client.creates, 0)

    def test_multiple_tenders_block_creation_before_any_write(self):
        self.value["receipt"]["tenders"] = [
            {"label": "Gift card", "amount": "10.00"}, {"label": "Example Account A", "amount": "33.25"}]
        self.assertEqual(self.draft()[0], 1)
        self.assertEqual(self.client.writes, [])

    def test_unknown_currency_precision_is_not_assumed(self):
        self.client.get_plan_settings = lambda plan: {"currency_format": {}}
        self.assertEqual(self.draft()[0], 1)
        self.assertEqual(self.client.writes, [])

    def test_mixed_purchase_return_allocations_are_blocked(self):
        self.value["allocations"][0]["amount"] = "50.00"
        self.value["allocations"][1]["amount"] = "-7.82"
        self.assertEqual(self.draft()[0], 1)
        self.assertEqual(self.client.writes, [])


if __name__ == "__main__":
    unittest.main()
