# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

from __future__ import annotations

import io
import json
import argparse
import os
import subprocess
import time
import tempfile
import threading
import urllib.request
import urllib.response
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from decimal import localcontext
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from budget_tools.api import ApiError, YnabClient, _default_transport
from budget_tools.errors import CliError
from budget_tools.cli import cmd_drafts, main, _fingerprint
from budget_tools.utils import _currency_string, _milliunits
from budget_tools.state import StateError, StateStore, TERMS_VERSION, read_private_json
from tests.test_receipts import proposal as receipt_proposal


class FakeClient:
    def __init__(self) -> None:
        self.plan_id = "99999999-9999-9999-9999-999999999999"
        self.transaction = {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "date": "2001-01-30",
            "amount": -43250,
            "account_id": "11111111-1111-1111-1111-111111111111",
            "account_name": "Example Account A",
            "payee_id": "22222222-2222-2222-2222-222222222222",
            "payee_name": "Example Merchant A",
            "category_id": "33333333-3333-3333-3333-333333333333",
            "category_name": "Example Category A",
            "memo": None,
            "cleared": "uncleared",
            "approved": False,
            "flag_color": None,
            "subtransactions": [],
            "deleted": False,
        }

    def _check_plan(self, plan_id):
        if plan_id != self.plan_id:
            raise ApiError("Synthetic plan not found", status=404)

    def _check_transaction(self, transaction_id):
        if transaction_id != self.transaction["id"]:
            raise ApiError("Synthetic transaction not found", status=404)

    def get_user(self):
        return {"id": "user-id"}

    def get_plans(self):
        return [{"id": self.plan_id, "name": "Example Category B"}]

    def get_accounts(self, plan_id):
        self._check_plan(plan_id)
        return [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "Example Account A",
                "closed": False,
                "deleted": False,
            }
        ]

    def get_plan_settings(self, plan_id):
        self._check_plan(plan_id)
        return {"currency_format": {"decimal_digits": 2}}

    def get_categories(self, plan_id):
        self._check_plan(plan_id)
        return [
            {
                "name": "Example Group A",
                "categories": [
                    {
                        "id": "33333333-3333-3333-3333-333333333333",
                        "name": "Example Category A",
                        "hidden": False,
                        "deleted": False,
                    },
                    {
                        "id": "44444444-4444-4444-4444-444444444444",
                        "name": "Example Category B",
                        "hidden": False,
                        "deleted": False,
                    },
                ],
            }
        ]

    def get_payees(self, plan_id):
        self._check_plan(plan_id)
        return [
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "name": "Example Merchant A",
                "deleted": False,
            }
        ]

    def get_transactions(self, plan_id, **kwargs):
        self._check_plan(plan_id)
        return [dict(self.transaction)]

    def get_transactions_by_account(self, plan_id, account_id, **kwargs):
        self._check_plan(plan_id)
        if account_id != self.transaction["account_id"]:
            return []
        return [dict(self.transaction)]

    def get_transaction(self, plan_id, transaction_id):
        self._check_plan(plan_id)
        self._check_transaction(transaction_id)
        return dict(self.transaction)

    def create_transaction(self, plan_id, transaction):
        self._check_plan(plan_id)
        for field in ("account_id", "date", "amount"):
            if field not in transaction:
                raise ApiError(f"Synthetic request omitted {field}", status=400)
        self.transaction.update(transaction)
        return {"data": {"transaction": dict(self.transaction)}}

    def update_transaction(self, plan_id, transaction_id, transaction):
        self._check_plan(plan_id)
        self._check_transaction(transaction_id)
        if "subtransactions" in transaction:
            total = sum(item.get("amount", 0) for item in transaction["subtransactions"])
            if total != self.transaction["amount"]:
                raise ApiError("Synthetic split does not balance", status=400)
        self.transaction.update(transaction)
        return {"data": {"transaction": dict(self.transaction)}}


class MoneyTests(unittest.TestCase):
    def test_currency_conversion_is_exact(self):
        self.assertEqual(_milliunits("43.25", outflow=True), -43250)
        self.assertEqual(_milliunits("43.25", outflow=False), 43250)
        self.assertEqual(_currency_string(-43250), "-43.25")

    def test_fingerprint_changes(self):
        transaction = {"amount": -1000, "date": "2001-01-30"}
        original = _fingerprint(transaction)
        transaction["amount"] = -2000
        self.assertNotEqual(original, _fingerprint(transaction))

    def test_conversion_and_display_ignore_decimal_context_precision(self):
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(_milliunits("1234.567", outflow=True), -1234567)
            self.assertEqual(_milliunits("1234.567", outflow=False), 1234567)
            self.assertEqual(_currency_string(-1234567), "-1234.567")
            self.assertEqual(_currency_string(1234567), "1234.567")

    def test_excess_fraction_cannot_round_into_a_valid_amount(self):
        for value in ("0.010000000000000000000000000000001", "1e-1000000"):
            with self.subTest(value=value), self.assertRaises(CliError):
                _milliunits(value, outflow=True)

    def test_amount_range_is_checked_before_conversion(self):
        for value in ("9223372036854775.808", "1e1000000"):
            with self.subTest(value=value), self.assertRaises(CliError):
                _milliunits(value, outflow=False)
        self.assertEqual(_milliunits("9223372036854775.807", outflow=False), 9223372036854775807)
        self.assertEqual(_milliunits("0.0000", outflow=True), 0)
        self.assertEqual(_milliunits("1.2300", outflow=True), -1230)

    def test_display_preserves_milliunits_at_range_boundary(self):
        self.assertEqual(_currency_string(9223372036854775807), "9223372036854775.807")
        self.assertEqual(_currency_string(-9223372036854775807), "-9223372036854775.807")
        self.assertEqual(_currency_string(1000), "1")
        self.assertEqual(_currency_string(10), "0.01")
        self.assertEqual(_currency_string(0), "0")


class StateSecurityTests(unittest.TestCase):
    def test_private_json_rejects_permissive_file(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            path = Path(directory) / "proposal.json"
            path.write_text("{}", encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(StateError, "mode 600"):
                read_private_json(path)
            os.chmod(path, 0o600)
            self.assertEqual(read_private_json(path), {})

    def test_operation_lock_serializes_a_separate_process(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory)
            signal = root / "child-acquired"
            script = (
                "from pathlib import Path; "
                "from budget_tools.state import StateStore; "
                f"store=StateStore(Path({str(root)!r})); "
                "lock=store.exclusive_lock('shared'); lock.__enter__(); "
                f"Path({str(signal)!r}).write_text('yes'); "
                "lock.__exit__(None,None,None)"
            )
            store = StateStore(root)
            with store.exclusive_lock("shared"):
                process = subprocess.Popen(
                    ["/usr/bin/python3", "-c", script],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                time.sleep(0.15)
                self.assertFalse(signal.exists())
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(signal.read_text(encoding="utf-8"), "yes")


class ApiTests(unittest.TestCase):
    def test_fixed_host_and_bearer_header(self):
        captured = {}

        def transport(method, url, headers, body, timeout):
            captured.update(method=method, url=url, headers=headers, body=body)
            return 200, b'{"data":{"user":{"id":"abc"}}}'

        client = YnabClient("secret-value", transport=transport)
        self.assertEqual(client.get_user()["id"], "abc")
        self.assertEqual(captured["url"], "https://api.ynab.com/v1/user")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret-value")

    def test_rejects_alternate_hosts(self):
        client = YnabClient("secret-value", transport=lambda *args: (200, b"{}"))
        with self.assertRaises(ValueError):
            client.request("GET", "https://example.com/steal")

    def test_authenticated_request_rejects_redirect(self):
        requests = []

        class OfflineHTTPSHandler(urllib.request.BaseHandler):
            handler_order = 100

            def https_open(self, request):
                requests.append(request)
                headers = Message()
                headers["Location"] = "https://redirect-test.invalid/synthetic"
                response = urllib.response.addinfourl(
                    io.BytesIO(b""), headers, request.full_url, 302
                )
                response.msg = "Found"
                return response

        real_build_opener = urllib.request.build_opener

        def offline_opener(*handlers):
            return real_build_opener(
                urllib.request.ProxyHandler({}), OfflineHTTPSHandler(), *handlers
            )

        with patch("urllib.request.build_opener", side_effect=offline_opener):
            with self.assertRaises(ApiError):
                _default_transport(
                    "GET",
                    "https://api.ynab.com/v1/user",
                    {"Authorization": "Bearer SYNTHETIC"},
                    None,
                    1.0,
                )
        self.assertEqual(len(requests), 1)


class CliWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.store = StateStore(Path(self.temporary.name))
        self.store.save_config(
            {
                "terms_acknowledged_version": TERMS_VERSION,
                "plan_id": "99999999-9999-9999-9999-999999999999",
            }
        )
        self.fake = FakeClient()

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("budget_tools.utils._client", return_value=self.fake):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(arguments, store=self.store)
        parsed = json.loads(stdout.getvalue()) if stdout.getvalue() else None
        return code, parsed, stderr.getvalue()

    def test_create_draft_then_apply(self):
        code, draft, error = self.run_cli(
            [
                "transactions",
                "draft-create",
                "--account",
                "Example Account A",
                "--date",
                "2001-01-30",
                "--outflow",
                "43.25",
                "--payee",
                "Example Merchant A",
                "--category",
                "Example Group A: Example Category A",
            ]
        )
        self.assertEqual((code, error), (0, ""))
        draft_id = draft["id"]
        code, result, error = self.run_cli(
            ["drafts", "apply", draft_id, "--confirm", draft_id]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["verification"]["verified"])

    def test_update_draft_detects_stale_transaction(self):
        code, draft, _ = self.run_cli(
            [
                "transactions",
                "draft-update",
                self.fake.transaction["id"],
                "--memo",
                "Receipt checked",
            ]
        )
        self.assertEqual(code, 0)
        self.fake.transaction["amount"] = -9999
        draft_id = draft["id"]
        code, result, error = self.run_cli(
            ["drafts", "apply", draft_id, "--confirm", draft_id]
        )
        self.assertEqual(code, 1)
        self.assertIsNone(result)
        self.assertIn("changed after this draft", error)

    def test_exact_get_exposes_split_details(self):
        self.fake.transaction.update(
            category_id=None,
            subtransactions=[
                {
                    "id": "split-1", "amount": -30000,
                    "category_id": "category-1", "category_name": "Example Category A",
                    "memo": "Example Category C", "deleted": False,
                },
                {
                    "id": "split-2", "amount": -13250,
                    "category_id": "category-2", "category_name": "Example Category B",
                    "memo": "Soap", "deleted": False,
                },
            ],
        )
        code, result, error = self.run_cli(
            ["transactions", "get", self.fake.transaction["id"]]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["subtransactions"][0]["amount_milliunits"], -30000)
        self.assertEqual(result["subtransactions"][1]["memo"], "Soap")

    def test_match_counts_all_candidates_before_display_limit(self):
        first = dict(self.fake.transaction)
        second = dict(self.fake.transaction, id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        self.fake.get_transactions_by_account = lambda *args, **kwargs: [first, second]
        code, result, error = self.run_cli(
            [
                "transactions", "match", "--since", "2001-01-29",
                "--until", "2001-01-31", "--account", "Example Account A",
                "--outflow", "43.25", "--limit", "1",
            ]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["match_status"], "ambiguous")
        self.assertEqual(result["candidate_count"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["candidates"]), 1)

    def test_receipt_split_draft_verifies_then_approves(self):
        value = receipt_proposal()
        value["allocations"][1]["category"] = "Example Group A: Example Category B"
        proposal_path = Path(self.temporary.name) / "proposal.json"
        proposal_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(proposal_path, 0o600)
        code, draft, error = self.run_cli(
            [
                "transactions", "draft-receipt-split", self.fake.transaction["id"],
                "--proposal", str(proposal_path),
            ]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(len(draft["receipt_review"]["allocations"]), 2)
        draft_id = draft["id"]
        code, result, error = self.run_cli(
            ["drafts", "apply", draft_id, "--confirm", draft_id]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["status"], "applied")
        self.assertTrue(self.fake.transaction["approved"])
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(
            sum(item["amount"] for item in self.fake.transaction["subtransactions"]),
            self.fake.transaction["amount"],
        )

    def test_receipt_split_does_not_approve_when_saved_split_differs(self):
        class IgnoringSplitClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.approval_writes = 0

            def update_transaction(self, plan_id, transaction_id, transaction):
                changes = dict(transaction)
                if "subtransactions" in changes:
                    changes.pop("subtransactions")
                if changes.get("approved") is True:
                    self.approval_writes += 1
                self.transaction.update(changes)
                return {"data": {"transaction": dict(self.transaction)}}

        self.fake = IgnoringSplitClient()
        value = receipt_proposal()
        value["allocations"][1]["category"] = "Example Group A: Example Category B"
        proposal_path = Path(self.temporary.name) / "proposal.json"
        proposal_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(proposal_path, 0o600)
        code, draft, error = self.run_cli(
            ["transactions", "draft-receipt-split", self.fake.transaction["id"],
             "--proposal", str(proposal_path)]
        )
        self.assertEqual((code, error), (0, ""))
        code, result, error = self.run_cli(
            ["drafts", "apply", draft["id"], "--confirm", draft["id"]]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "categorization_verification_failed")
        self.assertFalse(self.fake.transaction["approved"])
        self.assertEqual(self.fake.approval_writes, 0)

    def test_receipt_split_does_not_approve_when_total_changes(self):
        class ChangedTotalClient(FakeClient):
            approval_writes = 0

            def update_transaction(self, plan_id, transaction_id, transaction):
                result = super().update_transaction(plan_id, transaction_id, transaction)
                if "subtransactions" in transaction:
                    self.transaction["amount"] = -99999
                    result = {"data": {"transaction": dict(self.transaction)}}
                if transaction.get("approved") is True:
                    self.approval_writes += 1
                return result

        self.fake = ChangedTotalClient()
        value = receipt_proposal()
        value["allocations"][1]["category"] = "Example Group A: Example Category B"
        proposal_path = Path(self.temporary.name) / "proposal.json"
        proposal_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(proposal_path, 0o600)
        code, draft, error = self.run_cli(
            ["transactions", "draft-receipt-split", self.fake.transaction["id"],
             "--proposal", str(proposal_path)]
        )
        self.assertEqual((code, error), (0, ""))
        code, result, error = self.run_cli(
            ["drafts", "apply", draft["id"], "--confirm", draft["id"]]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "categorization_verification_failed")
        self.assertFalse(self.fake.transaction["approved"])
        self.assertEqual(self.fake.approval_writes, 0)

    def test_approval_failure_preserves_verified_split(self):
        class ApprovalFailureClient(FakeClient):
            def update_transaction(self, plan_id, transaction_id, transaction):
                if transaction == {"approved": True}:
                    raise ApiError("Synthetic approval rejection", status=400)
                return super().update_transaction(plan_id, transaction_id, transaction)

        self.fake = ApprovalFailureClient()
        value = receipt_proposal()
        value["allocations"][1]["category"] = "Example Group A: Example Category B"
        proposal_path = Path(self.temporary.name) / "proposal.json"
        proposal_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(proposal_path, 0o600)
        code, draft, error = self.run_cli(
            ["transactions", "draft-receipt-split", self.fake.transaction["id"],
             "--proposal", str(proposal_path)]
        )
        self.assertEqual((code, error), (0, ""))
        code, _, error = self.run_cli(
            ["drafts", "apply", draft["id"], "--confirm", draft["id"]]
        )
        self.assertEqual(code, 1)
        self.assertIn("Synthetic approval rejection", error)
        stored = self.store.load_draft(draft["id"])
        self.assertEqual(stored["status"], "approval_failed")
        self.assertTrue(stored["categorization_verification"]["verified"])
        self.assertEqual(len(self.fake.transaction["subtransactions"]), 2)
        self.assertFalse(self.fake.transaction["approved"])

    def test_existing_split_is_assessed_and_can_draft_approval(self):
        value = receipt_proposal()
        value["allocations"][1]["category"] = "Example Group A: Example Category B"
        proposal_path = Path(self.temporary.name) / "proposal.json"
        proposal_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(proposal_path, 0o600)
        self.fake.transaction.update(
            category_id=None,
            category_name="Split",
            memo="Receipt reviewed",
            subtransactions=[
                {
                    "id": "split-1", "amount": -30000,
                    "category_id": "33333333-3333-3333-3333-333333333333",
                    "memo": "Example Category C", "deleted": False,
                },
                {
                    "id": "split-2", "amount": -13250,
                    "category_id": "44444444-4444-4444-4444-444444444444",
                    "memo": "Example Category E and tax", "deleted": False,
                },
            ],
            approved=False,
        )
        code, assessment, error = self.run_cli(
            ["transactions", "assess-receipt", self.fake.transaction["id"],
             "--proposal", str(proposal_path)]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(assessment["route"], "existing_split_matches")

        code, draft, error = self.run_cli(
            ["transactions", "draft-receipt-approval", self.fake.transaction["id"],
             "--proposal", str(proposal_path)]
        )
        self.assertEqual((code, error), (0, ""))
        code, result, error = self.run_cli(
            ["drafts", "apply", draft["id"], "--confirm", draft["id"]]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "verified_approval")
        self.assertTrue(self.fake.transaction["approved"])

    def test_mismatched_existing_split_routes_to_manual_correction(self):
        value = receipt_proposal()
        value["allocations"][1]["category"] = "Example Group A: Example Category B"
        proposal_path = Path(self.temporary.name) / "proposal.json"
        proposal_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(proposal_path, 0o600)
        self.fake.transaction.update(
            category_id=None,
            memo="Receipt reviewed",
            subtransactions=[
                {
                    "id": "split-1", "amount": -43250,
                    "category_id": "33333333-3333-3333-3333-333333333333",
                    "memo": "All groceries", "deleted": False,
                }
            ],
            approved=False,
        )
        code, assessment, error = self.run_cli(
            ["transactions", "assess-receipt", self.fake.transaction["id"],
             "--proposal", str(proposal_path)]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(assessment["route"], "manual_split_correction_required")
        self.assertIsNone(assessment["next_command"])

    def test_successful_write_persists_id_before_readback_and_reconciles(self):
        class ReadbackFailureClient(FakeClient):
            fail_readback = True

            def create_transaction(self, plan_id, transaction):
                self.transaction.update(transaction)
                return {"data": {"transaction": dict(self.transaction)}}

            def get_transaction(self, plan_id, transaction_id):
                if self.fail_readback:
                    raise ApiError("Synthetic readback failure")
                return dict(self.transaction)

        self.fake = ReadbackFailureClient()
        code, draft, error = self.run_cli(
            [
                "transactions", "draft-create", "--account", "Example Account A",
                "--date", "2001-01-30", "--outflow", "43.25",
            ]
        )
        self.assertEqual((code, error), (0, ""))
        draft_id = draft["id"]
        code, _, _ = self.run_cli(["drafts", "apply", draft_id, "--confirm", draft_id])
        self.assertEqual(code, 1)
        stored = self.store.load_draft(draft_id)
        self.assertEqual(stored["transaction_id"], self.fake.transaction["id"])
        self.assertEqual(stored["status"], "verification_pending")

        self.fake.fail_readback = False
        code, result, error = self.run_cli(["drafts", "reconcile", draft_id])
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["status"], "applied")

    def test_concurrent_apply_submits_once(self):
        class CountingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.writes = 0
                self.guard = threading.Lock()

            def create_transaction(self, plan_id, transaction):
                with self.guard:
                    self.writes += 1
                return super().create_transaction(plan_id, transaction)

        self.fake = CountingClient()
        code, draft, error = self.run_cli(
            [
                "transactions", "draft-create", "--account", "Example Account A",
                "--date", "2001-01-30", "--outflow", "43.25",
            ]
        )
        self.assertEqual((code, error), (0, ""))
        draft_id = draft["id"]
        args = argparse.Namespace(
            drafts_command="apply", draft_id=draft_id, confirm=draft_id
        )

        def apply_once():
            try:
                cmd_drafts(args, self.store)
                return "applied"
            except CliError:
                return "blocked"

        with patch("budget_tools.utils._client", return_value=self.fake):
            with patch("budget_tools.utils._json_output"):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: apply_once(), range(2)))
        self.assertEqual(sorted(results), ["applied", "blocked"])
        self.assertEqual(self.fake.writes, 1)


if __name__ == "__main__":
    unittest.main()
