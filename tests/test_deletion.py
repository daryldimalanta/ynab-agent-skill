# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

import copy
import json
import unittest
from unittest.mock import patch

from budget_tools import deletion
from budget_tools import transaction_workflow as workflow
from budget_tools.api import ApiError
from budget_tools.errors import CliError
from tests import test_budget_tools as fixtures


class DeletionTests(unittest.TestCase):
    setUp = fixtures.CliWorkflowTests.setUp
    tearDown = fixtures.CliWorkflowTests.tearDown
    run_cli = fixtures.CliWorkflowTests.run_cli

    def test_fingerprint_deterministic_and_sensitive(self):
        tx = dict(self.fake.transaction)
        fp1 = deletion.fingerprint(tx)
        fp2 = deletion.fingerprint(dict(tx))
        self.assertEqual(fp1, fp2)
        tx_changed = dict(tx, amount=-50000)
        self.assertNotEqual(fp1, deletion.fingerprint(tx_changed))

    def test_protected_states_identification(self):
        tx = dict(self.fake.transaction, approved=False, cleared="uncleared")
        self.assertEqual(deletion.protected_states(tx), [])

        tx["approved"] = True
        self.assertIn("approved", deletion.protected_states(tx))

        tx["approved"] = False
        tx["cleared"] = "cleared"
        self.assertIn("cleared", deletion.protected_states(tx))

        tx["cleared"] = "reconciled"
        self.assertIn("reconciled", deletion.protected_states(tx))

        tx["cleared"] = "uncleared"
        tx["import_id"] = "BANK-123"
        self.assertIn("imported/posted", deletion.protected_states(tx))

        tx["import_id"] = None
        tx["matched_transaction_id"] = "OTHER-456"
        self.assertIn("matched", deletion.protected_states(tx))

    def test_require_eligible_checks(self):
        tx = dict(self.fake.transaction, approved=False, cleared="uncleared")
        # Matches exact id and account
        with self.assertRaisesRegex(CliError, "does not match"):
            deletion.require_eligible(tx, "wrong-id", tx["account_id"])
        with self.assertRaisesRegex(CliError, "does not match"):
            deletion.require_eligible(tx, tx["id"], "wrong-account")

        # Deleted transaction rejected
        with self.assertRaisesRegex(CliError, "non-deleted"):
            deletion.require_eligible(dict(tx, deleted=True), tx["id"], tx["account_id"])

        # Unknown cleared / approval state
        with self.assertRaisesRegex(CliError, "clearing or approval state"):
            deletion.require_eligible(dict(tx, cleared="invalid"), tx["id"], tx["account_id"])
        with self.assertRaisesRegex(CliError, "clearing or approval state"):
            deletion.require_eligible(dict(tx, approved=None), tx["id"], tx["account_id"])

        # Protected state rejected without allow_protected
        protected_tx = dict(tx, approved=True)
        with self.assertRaisesRegex(CliError, "protected; explicit protected-deletion review"):
            deletion.require_eligible(protected_tx, tx["id"], tx["account_id"], allow_protected=False)
        deletion.require_eligible(protected_tx, tx["id"], tx["account_id"], allow_protected=True)

        # Invalid date or amount
        with self.assertRaisesRegex(CliError, "valid date or amount"):
            deletion.require_eligible(dict(tx, date=123), tx["id"], tx["account_id"])
        with self.assertRaisesRegex(CliError, "valid date or amount"):
            deletion.require_eligible(dict(tx, amount="invalid"), tx["id"], tx["account_id"])

        # Transfers blocked on parent
        with self.assertRaisesRegex(CliError, "Transfer transactions"):
            deletion.require_eligible(dict(tx, transfer_account_id="other-acc"), tx["id"], tx["account_id"])
        with self.assertRaisesRegex(CliError, "Transfer transactions"):
            deletion.require_eligible(dict(tx, transfer_transaction_id="other-tx"), tx["id"], tx["account_id"])

        # Split checks
        with self.assertRaisesRegex(CliError, "malformed split data"):
            deletion.require_eligible(dict(tx, subtransactions="not-a-list"), tx["id"], tx["account_id"])

        # Transfers blocked on split rows
        transfer_split = [
            {"id": "s1", "amount": tx["amount"], "transfer_account_id": "other-acc", "deleted": False}
        ]
        with self.assertRaisesRegex(CliError, "Transfer transactions"):
            deletion.require_eligible(dict(tx, subtransactions=transfer_split), tx["id"], tx["account_id"])

        # Unbalanced split rows
        unbalanced = [
            {"id": "s1", "amount": tx["amount"] + 100, "deleted": False}
        ]
        with self.assertRaisesRegex(CliError, "unbalanced or malformed split amounts"):
            deletion.require_eligible(dict(tx, subtransactions=unbalanced), tx["id"], tx["account_id"])

        # Balanced split rows pass
        balanced = [
            {"id": "s1", "amount": tx["amount"], "deleted": False}
        ]
        deletion.require_eligible(dict(tx, subtransactions=balanced), tx["id"], tx["account_id"])

    def test_create_draft_validates_reason(self):
        tx = self.fake.transaction
        account_id = tx["account_id"]
        with self.assertRaisesRegex(CliError, "nonempty reason"):
            deletion.create_draft(self.fake, self.store, tx["id"], account_id, "")
        with self.assertRaisesRegex(CliError, "nonempty reason"):
            deletion.create_draft(self.fake, self.store, tx["id"], account_id, "   ")
        with self.assertRaisesRegex(CliError, "at most 500 characters"):
            deletion.create_draft(self.fake, self.store, tx["id"], account_id, "x" * 501)

    def test_create_draft_and_apply_unprotected_success(self):
        tx_id = self.fake.transaction["id"]
        account_name = self.fake.transaction["account_name"]
        code, draft, err = self.run_cli([
            "transactions", "draft-delete", tx_id,
            "--account", account_name,
            "--reason", "Provisional duplicate entry",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(draft["operation"], "delete")
        self.assertIn("deletion_review", draft)
        self.assertTrue(draft["deletion_review"]["irreversible"])
        self.assertIn(f"ynab-agent-skill drafts apply {draft['id']} --confirm {draft['id']}", draft["apply_command"])

        # Apply deletion
        code, applied, err = self.run_cli([
            "drafts", "apply", draft["id"],
            "--confirm", draft["id"],
        ])
        self.assertEqual(code, 0)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["stage"], "verified_deletion")
        self.assertTrue(applied["verification"]["verified"])
        self.assertEqual(applied["verification"]["observed"], "deleted")
        self.assertTrue(self.fake.transaction["deleted"])

        # Cannot replay
        code, replay, err = self.run_cli([
            "drafts", "apply", draft["id"],
            "--confirm", draft["id"],
        ])
        self.assertEqual(code, 1)
        self.assertIn("only pending drafts", err)

        # Deletion history recorded
        code, history_info, err = self.run_cli(["drafts", "deletion-history"])
        self.assertEqual(code, 0)
        history_text = self.store.deletion_history_path.read_text(encoding="utf-8")
        self.assertIn("Provisional duplicate entry", history_text)
        self.assertIn(draft["id"], history_text)

    def test_apply_protected_requires_confirm_protected(self):
        self.fake.transaction["cleared"] = "cleared"
        tx_id = self.fake.transaction["id"]
        account_name = self.fake.transaction["account_name"]

        # Without allow-protected, drafting fails
        code, draft, err = self.run_cli([
            "transactions", "draft-delete", tx_id,
            "--account", account_name,
            "--reason", "Removing cleared error",
        ])
        self.assertEqual(code, 1)
        self.assertIn("protected-deletion review is required", err)

        # With allow-protected, draft succeeds
        code, draft, err = self.run_cli([
            "transactions", "draft-delete", tx_id,
            "--account", account_name,
            "--reason", "Removing cleared error",
            "--allow-protected",
        ])
        self.assertEqual(code, 0)
        self.assertIn(f"--confirm-protected {tx_id}", draft["apply_command"])

        # Applying without --confirm-protected fails
        code, applied, err = self.run_cli([
            "drafts", "apply", draft["id"],
            "--confirm", draft["id"],
        ])
        self.assertEqual(code, 1)
        self.assertIn("--confirm-protected matching the exact transaction id", err)

        # Applying with wrong --confirm-protected fails
        code, applied, err = self.run_cli([
            "drafts", "apply", draft["id"],
            "--confirm", draft["id"],
            "--confirm-protected", "wrong-tx-id",
        ])
        self.assertEqual(code, 1)
        self.assertIn("--confirm-protected matching the exact transaction id", err)

        # Applying with correct --confirm-protected succeeds
        code, applied, err = self.run_cli([
            "drafts", "apply", draft["id"],
            "--confirm", draft["id"],
            "--confirm-protected", tx_id,
        ])
        self.assertEqual(code, 0)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["stage"], "verified_deletion")
        self.assertTrue(applied["verification"]["verified"])

    def test_confirm_protected_disallowed_for_non_delete(self):
        code, draft, err = self.run_cli([
            "transactions", "draft-update", self.fake.transaction["id"],
            "--memo", "Testing update",
        ])
        self.assertEqual(code, 0)
        code, applied, err = self.run_cli([
            "drafts", "apply", draft["id"],
            "--confirm", draft["id"],
            "--confirm-protected", self.fake.transaction["id"],
        ])
        self.assertEqual(code, 1)
        self.assertIn("--confirm-protected is only for protected transaction deletion", err)

    def test_revalidate_detects_transaction_change_before_deletion(self):
        tx_id = self.fake.transaction["id"]
        account_name = self.fake.transaction["account_name"]
        code, draft, err = self.run_cli([
            "transactions", "draft-delete", tx_id,
            "--account", account_name,
            "--reason", "Testing revalidation change",
        ])
        self.assertEqual(code, 0)

        # Modify transaction before apply
        self.fake.transaction["amount"] = -100000

        code, applied, err = self.run_cli([
            "drafts", "apply", draft["id"],
            "--confirm", draft["id"],
        ])
        self.assertEqual(code, 1)
        self.assertIn("Deletion target changed after review", err)

    def test_readback_404_when_account_exists(self):
        tx_id = self.fake.transaction["id"]
        account_name = self.fake.transaction["account_name"]
        code, draft, err = self.run_cli([
            "transactions", "draft-delete", tx_id,
            "--account", account_name,
            "--reason", "Testing 404 readback",
        ])
        self.assertEqual(code, 0)

        # Patch get_transaction during readback to simulate 404 resource_not_found
        orig_get_tx = self.fake.get_transaction
        call_count = [0]
        def mock_get_tx(plan_id, target_id):
            call_count[0] += 1
            if call_count[0] >= 2:  # 1: revalidate before send, 2: readback
                raise ApiError("Not found", status=404, error_name="resource_not_found")
            return orig_get_tx(plan_id, target_id)

        with patch.object(self.fake, "get_transaction", side_effect=mock_get_tx):
            code, applied, err = self.run_cli([
                "drafts", "apply", draft["id"],
                "--confirm", draft["id"],
            ])
        self.assertEqual(code, 0)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["verification"]["observed"], "resource_not_found")
        self.assertTrue(applied["verification"]["verified"])

    def test_uncertain_deletion_and_reconciliation(self):
        tx_id = self.fake.transaction["id"]
        account_name = self.fake.transaction["account_name"]
        code, draft, err = self.run_cli([
            "transactions", "draft-delete", tx_id,
            "--account", account_name,
            "--reason", "Testing network failure on delete",
        ])
        self.assertEqual(code, 0)

        # Fail DELETE request with 500 error
        def fail_delete(plan_id, target_id):
            raise ApiError("Gateway Timeout", status=504, error_name="gateway_timeout")

        with patch.object(self.fake, "delete_transaction", side_effect=fail_delete):
            code, applied, err = self.run_cli([
                "drafts", "apply", draft["id"],
                "--confirm", draft["id"],
            ])
        self.assertEqual(code, 1)
        saved_draft = self.store.load_draft(draft["id"])
        self.assertEqual(saved_draft["status"], "uncertain")
        self.assertEqual(saved_draft["stage"], "write_uncertain")

        # Reconciling while transaction still exists fails closed and preserves uncertainty
        code, reconciled, err = self.run_cli(["drafts", "reconcile", draft["id"]])
        self.assertEqual(code, 1)
        self.assertIn("The transaction is still present; deletion is not verified; do not replay", err)

        # Once transaction is deleted, reconcile succeeds
        self.fake.transaction["deleted"] = True
        code, reconciled, err = self.run_cli(["drafts", "reconcile", draft["id"]])
        self.assertEqual(code, 0)
        self.assertEqual(reconciled["status"], "applied")
        self.assertEqual(reconciled["stage"], "verified_deletion")

    def test_create_restoration_draft_and_apply(self):
        tx_id = self.fake.transaction["id"]
        account_name = self.fake.transaction["account_name"]
        code, draft, err = self.run_cli([
            "transactions", "draft-delete", tx_id,
            "--account", account_name,
            "--reason", "To be restored",
        ])
        self.assertEqual(code, 0)
        code, applied, err = self.run_cli([
            "drafts", "apply", draft["id"],
            "--confirm", draft["id"],
        ])
        self.assertEqual(code, 0)

        # Create restoration draft
        code, restore_draft, err = self.run_cli([
            "transactions", "draft-restore-deletion", draft["id"],
        ])
        self.assertEqual(code, 0)
        self.assertEqual(restore_draft["operation"], "create")
        self.assertEqual(self.store.load_draft(restore_draft["id"])["workflow"], "deletion_restore")
        self.assertEqual(restore_draft["restoration_review"]["new_transaction_payload"]["cleared"], "uncleared")
        self.assertEqual(restore_draft["restoration_review"]["new_transaction_payload"]["approved"], False)

        # Cannot create second restoration draft from same deletion draft
        code, second_restore, err = self.run_cli([
            "transactions", "draft-restore-deletion", draft["id"],
        ])
        self.assertEqual(code, 1)
        self.assertIn("A restoration draft already exists", err)

        # Apply restoration draft
        new_tx_id = "new-restored-tx-id"
        def mock_create(plan_id, payload):
            created = dict(self.fake.transaction, **payload)
            created["id"] = new_tx_id
            created["deleted"] = False
            self.fake.transaction = created
            return {"data": {"transaction": created}}

        with patch.object(self.fake, "create_transaction", side_effect=mock_create):
            code, restore_applied, err = self.run_cli([
                "drafts", "apply", restore_draft["id"],
                "--confirm", restore_draft["id"],
            ])
        self.assertEqual(code, 0)
        self.assertEqual(restore_applied["status"], "applied")
        self.assertEqual(restore_applied["stage"], "verified")
        self.assertTrue(restore_applied["verification"]["verified"])
        self.assertEqual(restore_applied["transaction"]["id"], new_tx_id)

    def test_restore_deletion_with_splits(self):
        # Set up a split transaction
        self.fake.transaction.update(
            category_id=None,
            amount=-50000,
            subtransactions=[
                {"id": "s1", "amount": -30000, "category_id": "c1", "memo": "Row 1", "deleted": False},
                {"id": "s2", "amount": -20000, "category_id": "c2", "memo": "Row 2", "deleted": False},
            ],
        )
        tx_id = self.fake.transaction["id"]
        account_name = self.fake.transaction["account_name"]

        code, draft, err = self.run_cli([
            "transactions", "draft-delete", tx_id,
            "--account", account_name,
            "--reason", "Deleting split transaction",
        ])
        self.assertEqual(code, 0)
        code, applied, err = self.run_cli([
            "drafts", "apply", draft["id"],
            "--confirm", draft["id"],
        ])
        self.assertEqual(code, 0)

        # Draft restoration
        code, restore_draft, err = self.run_cli([
            "transactions", "draft-restore-deletion", draft["id"],
        ])
        self.assertEqual(code, 0)
        payload = restore_draft["restoration_review"]["new_transaction_payload"]
        self.assertIsNone(payload["category_id"])
        self.assertEqual(len(payload["subtransactions"]), 2)
        self.assertEqual(payload["subtransactions"][0]["amount"], -30000)
        self.assertEqual(payload["subtransactions"][1]["amount"], -20000)

        # Apply restored split
        new_tx_id = "restored-split-id"
        def mock_create(plan_id, payload):
            created = dict(self.fake.transaction, **payload)
            created["id"] = new_tx_id
            created["deleted"] = False
            created["category_name"] = "Split"
            created["category_id"] = "split-cat-id"
            self.fake.transaction = created
            return {"data": {"transaction": created}}

        with patch.object(self.fake, "create_transaction", side_effect=mock_create):
            code, restore_applied, err = self.run_cli([
                "drafts", "apply", restore_draft["id"],
                "--confirm", restore_draft["id"],
            ])
        self.assertEqual(code, 0)
        self.assertEqual(restore_applied["status"], "applied")
        self.assertTrue(restore_applied["verification"]["verified"])

    def test_restore_blocked_if_duplicate_exists(self):
        tx_id = self.fake.transaction["id"]
        account_name = self.fake.transaction["account_name"]
        code, draft, err = self.run_cli([
            "transactions", "draft-delete", tx_id,
            "--account", account_name,
            "--reason", "Duplicate test",
        ])
        self.assertEqual(code, 0)
        code, applied, err = self.run_cli([
            "drafts", "apply", draft["id"],
            "--confirm", draft["id"],
        ])
        self.assertEqual(code, 0)

        # Simulate a duplicate existing transaction on same date and amount
        duplicate_tx = dict(self.fake.transaction, id="dup-tx-id", deleted=False)
        self.fake.get_transactions_by_account = lambda plan_id, account_id, **kwargs: [duplicate_tx]

        code, restore_draft, err = self.run_cli([
            "transactions", "draft-restore-deletion", draft["id"],
        ])
        self.assertEqual(code, 1)
        self.assertIn("A same-date, same-amount transaction exists", err)


if __name__ == "__main__":
    unittest.main()
