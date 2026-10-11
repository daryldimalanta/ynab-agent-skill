# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Direct library contracts reuse only fictional CLI draft fixtures."""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import unittest

from budget_tools import transaction_workflow as workflow
from budget_tools.api import ApiError
from budget_tools.errors import CliError
from budget_tools.state import StateError
from tests import test_budget_tools as fixtures


class DirectWorkflowTests(unittest.TestCase):
    setUp = fixtures.CliWorkflowTests.setUp
    tearDown = fixtures.CliWorkflowTests.tearDown
    run_cli = fixtures.CliWorkflowTests.run_cli

    def draft(self):
        code, draft, error = self.run_cli(
            [
                "transactions",
                "draft-update",
                self.fake.transaction["id"],
                "--memo",
                "Fictional direct workflow",
            ]
        )
        self.assertEqual((code, error), (0, ""))
        return draft["id"]

    def execute(self, draft_id, **options):
        return workflow.execute(
            self.store, draft_id, client_factory=lambda: self.fake, confirm=draft_id, **options
        )

    def test_apply_verifies_and_cannot_replay(self):
        draft_id = self.draft()
        result = self.execute(draft_id)
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(result["status"], "applied")
        with self.assertRaisesRegex(CliError, "only pending"):
            self.execute(draft_id)
        self.assertEqual(self.fake.transaction["memo"], "Fictional direct workflow")

    def test_bad_confirmation_and_terms_block_client_creation(self):
        draft_id = self.draft()
        factory = Mock(side_effect=AssertionError("No API access before authorization"))
        with self.assertRaisesRegex(CliError, "confirm"):
            workflow.execute(self.store, draft_id, client_factory=factory, confirm="wrong")
        self.store.save_config({})
        with self.assertRaises(StateError):
            workflow.execute(self.store, draft_id, client_factory=factory, confirm=draft_id)
        factory.assert_not_called()

    def test_expiry_and_stale_transaction_prevent_write(self):
        draft_id = self.draft()
        with patch("budget_tools.transaction_workflow.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=25)
            with self.assertRaisesRegex(CliError, "24 hours"):
                self.execute(draft_id)
        self.fake.transaction["memo"] = "Changed by another client"
        with self.assertRaisesRegex(CliError, "changed after"):
            self.execute(draft_id)
        self.assertEqual(self.store.load_draft(draft_id)["status"], "pending")

    def test_tampered_review_seal_blocks_client_creation(self):
        draft_id = self.draft()
        path = self.store.root / "drafts" / (draft_id + ".json")
        draft = json.loads(path.read_text())
        draft["payload"]["memo"] = "Tampered"
        path.write_text(json.dumps(draft))
        factory = Mock()
        with self.assertRaises(StateError):
            workflow.execute(self.store, draft_id, client_factory=factory, confirm=draft_id)
        factory.assert_not_called()

    def test_draft_then_transaction_locks_remain_held_during_write(self):
        draft_id = self.draft()
        held = []
        original_lock = self.store.exclusive_lock
        original_update = self.fake.update_transaction

        @contextmanager
        def tracked(scope):
            with original_lock(scope):
                held.append(scope)
                try:
                    yield
                finally:
                    held.pop()

        def update(*args):
            self.assertEqual(
                held,
                [
                    "draft:" + draft_id,
                    "transaction:" + self.fake.plan_id + ":" + self.fake.transaction["id"],
                ],
            )
            self.assertEqual(self.store.load_draft(draft_id)["status"], "applying")
            return original_update(*args)

        with (
            patch.object(self.store, "exclusive_lock", tracked),
            patch.object(self.fake, "update_transaction", update),
        ):
            self.execute(draft_id)
        self.assertEqual(held, [])

    def test_uncertain_write_never_replays_and_reconcile_never_writes(self):
        draft_id = self.draft()
        with patch.object(
            self.fake, "update_transaction", side_effect=ApiError("Fictional lost response")
        ) as write:
            with self.assertRaises(ApiError):
                self.execute(draft_id)
            with self.assertRaisesRegex(CliError, "only pending"):
                self.execute(draft_id)
            with self.assertRaisesRegex(CliError, "does not prove"):
                self.execute(draft_id, command="reconcile")
            self.assertEqual(write.call_count, 1)
        self.assertEqual(self.store.load_draft(draft_id)["status"], "uncertain")

    def test_resume_cannot_apply_generic_write(self):
        draft_id = self.draft()
        with self.assertRaisesRegex(CliError, "Only receipt"):
            self.execute(draft_id, command="resume")
