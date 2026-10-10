# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Offline acceptance checks added during the post-implementation review.

These preserve safety regressions from a historical review; its findings were
subsequently repaired. Any new failure is a regression to investigate, not an
expected failure. Do not weaken the assertions. All records are synthetic.
Run: python3 -m unittest discover -s validation -p test_post_implementation_safety.py -v
"""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.response
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from budget_tools.api import ApiError, YnabClient
from budget_tools.cli import _canonical_subtransactions, main
from budget_tools.receipts import ReceiptProposalError, validate_receipt_proposal
from budget_tools.state import StateError, StateStore, TERMS_VERSION, assert_local_storage
from tests.test_budget_tools import FakeClient
from tests.test_receipts import TRANSACTION, category, proposal


class ReviewClient(FakeClient):
    """ID-checked synthetic client with independent response snapshots."""

    def __init__(self):
        super().__init__()
        self.writes = []
        self.reject_approval = False
        self.fail_approval_readback = False
        self.readback_failure_pending = False
        self.change_total_after_write = False
        self.change_payee_before_approval = False
        self.split_reads = None

    def get_transaction(self, plan_id, transaction_id):
        self._check_plan(plan_id)
        self._check_transaction(transaction_id)
        if self.readback_failure_pending:
            self.readback_failure_pending = False
            raise ApiError("Synthetic readback interruption")
        if self.split_reads is not None:
            self.split_reads += 1
            if self.split_reads == 2 and self.change_payee_before_approval:
                self.transaction["payee_id"] = "55555555-5555-5555-5555-555555555555"
                self.transaction["payee_name"] = "Different synthetic merchant"
        return copy.deepcopy(super().get_transaction(plan_id, transaction_id))

    def update_transaction(self, plan_id, transaction_id, transaction):
        self._check_plan(plan_id)
        self._check_transaction(transaction_id)
        self.writes.append(copy.deepcopy(transaction))
        if transaction == {"approved": True} and self.reject_approval:
            raise ApiError("Synthetic approval rejection", status=400)
        result = copy.deepcopy(super().update_transaction(plan_id, transaction_id, transaction))
        if "subtransactions" in transaction:
            self.split_reads = 0
        if self.change_total_after_write:
            self.transaction["amount"] = -99990
        if transaction == {"approved": True} and self.fail_approval_readback:
            self.readback_failure_pending = True
        return result


class OfflineCase(unittest.TestCase):
    def setUp(self):
        # Restrict filesystem discovery to the synthetic local fixture. Never
        # enumerate or resolve unrelated mounted shares during these tests.
        mount = patch("budget_tools.state.subprocess.run", return_value=subprocess.CompletedProcess(
            ["/sbin/mount"], 0, stdout="synthetic-device on /private/tmp (apfs, local)\n", stderr=""))
        mount.start()
        self.addCleanup(mount.stop)
        # Unexpected network or credential access is a test failure, not a fallback.
        for target in ("socket.create_connection", "socket.socket.connect",
                       "socket.getaddrinfo", "budget_tools.utils.read_access_token"):
            blocker = patch(target, side_effect=AssertionError("Offline review forbids network/credentials"))
            blocker.start()
            self.addCleanup(blocker.stop)


class WorkflowAcceptance(OfflineCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore(self.root / "state")
        self.client = ReviewClient()
        self.store.save_config({"terms_acknowledged_version": TERMS_VERSION,
                                "plan_id": self.client.plan_id})
        self.proposal_path = self.root / "synthetic-proposal.json"
        value = proposal()
        value["allocations"][1]["category"] = "Example Group A: Example Category B"
        self.proposal_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(self.proposal_path, 0o600)

    def run_cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("budget_tools.utils._client", return_value=self.client):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(list(arguments), store=self.store)
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def make_receipt_draft(self, existing=False):
        if existing:
            self.client.transaction.update(
                category_id=None, memo="Receipt reviewed", subtransactions=[
                    {"id": "synthetic-line-1", "amount": -30000,
                     "category_id": "33333333-3333-3333-3333-333333333333", "memo": "Example Category C"},
                    {"id": "synthetic-line-2", "amount": -13250,
                     "category_id": "44444444-4444-4444-4444-444444444444",
                     "memo": "Example Category E and tax"},
                ])
        command = "draft-receipt-approval" if existing else "draft-receipt-split"
        code, result, error = self.run_cli("transactions", command, self.client.transaction["id"],
                                          "--proposal", str(self.proposal_path))
        self.assertEqual((code, error), (0, ""))
        return result["id"]

    def apply(self, draft_id):
        return self.run_cli("drafts", "apply", draft_id, "--confirm", draft_id)

    def test_control_split_verifies_before_separate_approval(self):
        draft_id = self.make_receipt_draft()
        code, result, error = self.apply(draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "verified_approval")
        self.assertFalse(self.client.writes[0]["approved"])
        self.assertEqual(self.client.writes[1], {"approved": True})
        self.assertEqual(len(self.client.writes), 2)

    def test_generic_update_detects_changed_original_total_on_readback(self):
        code, draft, error = self.run_cli("transactions", "draft-update", self.client.transaction["id"],
                                          "--memo", "Synthetic receipt checked")
        self.assertEqual((code, error), (0, ""))
        self.client.change_total_after_write = True
        code, result, error = self.apply(draft["id"])
        self.assertEqual((code, error), (0, ""))
        self.assertFalse(result["verification"]["verified"],
                         "A changed original amount must not be reported as verified")

    def test_existing_split_rejected_approval_reconciles_as_pending_not_complete(self):
        draft_id = self.make_receipt_draft(existing=True)
        self.client.reject_approval = True
        code, _, _ = self.apply(draft_id)
        self.assertEqual(code, 1)
        self.assertFalse(self.client.transaction["approved"])
        code, result, error = self.run_cli("drafts", "reconcile", draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["status"], "approval_pending",
                         "An unapproved transaction must not be reported as completed")

    def test_existing_split_successful_approval_recovers_after_readback_failure(self):
        draft_id = self.make_receipt_draft(existing=True)
        self.client.fail_approval_readback = True
        code, _, _ = self.apply(draft_id)
        self.assertEqual(code, 1)
        self.assertTrue(self.client.transaction["approved"])
        code, result, error = self.run_cli("drafts", "reconcile", draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(len(self.client.writes), 1, "Reconciliation must not retry approval")
        self.assertEqual(result["stage"], "verified_approval")

    def test_existing_split_approval_can_resume_after_reconciliation(self):
        draft_id = self.make_receipt_draft(existing=True)
        self.client.reject_approval = True
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.run_cli("drafts", "reconcile", draft_id)[0], 0)
        self.client.reject_approval = False
        code, result, error = self.run_cli("drafts", "resume", draft_id, "--confirm", draft_id)
        self.assertEqual((code, error), (0, ""), "Existing split approvals need the same recovery path")
        self.assertEqual(result["stage"], "verified_approval")

    def test_changed_payee_between_verification_and_approval_blocks_approval(self):
        draft_id = self.make_receipt_draft()
        self.client.change_payee_before_approval = True
        self.apply(draft_id)
        self.assertNotEqual(self.client.transaction["payee_name"], "Example Merchant A")
        self.assertFalse(self.client.transaction["approved"],
                         "Intervening merchant changes must invalidate the reviewed receipt match")
        self.assertEqual(len(self.client.writes), 1)

    def test_expired_receipt_resume_does_not_write(self):
        draft_id = self.make_receipt_draft()
        self.client.reject_approval = True
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.run_cli("drafts", "reconcile", draft_id)[0], 0)
        self.client.reject_approval = False
        count = len(self.client.writes)
        with patch("budget_tools.cli.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=25)
            code, _, error = self.run_cli("drafts", "resume", draft_id, "--confirm", draft_id)
        self.assertEqual(len(self.client.writes), count, "Expired drafts must not send a resumed write")
        self.assertEqual(code, 1)
        self.assertIn("older than 24 hours", error)

    def test_control_two_processes_apply_same_create_only_once(self):
        code, draft, error = self.run_cli("transactions", "draft-create", "--account", "Example Account A",
                                          "--date", "2001-01-30", "--outflow", "43.25")
        self.assertEqual((code, error), (0, ""))
        worker = r'''
import os, sys, time
from subprocess import CompletedProcess
from pathlib import Path
from unittest.mock import patch
from budget_tools.cli import main
from budget_tools.state import StateStore
from tests.test_budget_tools import FakeClient
draft_id, worker_id = sys.argv[2:4]
class CountingClient(FakeClient):
    def create_transaction(self, plan_id, transaction):
        time.sleep(0.15)
        fd = os.open(store.root / "synthetic-writes", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, b"create\n")
        finally:
            os.close(fd)
        return super().create_transaction(plan_id, transaction)
def forbidden(*args, **kwargs):
    raise AssertionError("Offline worker forbids network/credentials")
with patch("socket.create_connection", forbidden), patch("socket.socket.connect", forbidden), \
     patch("socket.getaddrinfo", forbidden), patch("budget_tools.utils.read_access_token", forbidden), \
     patch("budget_tools.state.subprocess.run", return_value=CompletedProcess(
         ["/sbin/mount"], 0, stdout="synthetic-device on /private/tmp (apfs, local)\n", stderr="")), \
     patch("budget_tools.utils._client", return_value=CountingClient()):
    store = StateStore(Path(sys.argv[1]))
    (store.root / ("ready-" + worker_id)).touch(mode=0o600)
    deadline = time.monotonic() + 10
    while not (store.root / "start").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Synthetic start barrier timed out")
        time.sleep(0.01)
    sys.exit(main(["drafts", "apply", draft_id, "--confirm", draft_id], store=store))
'''
        processes = []
        try:
            for index in range(2):
                processes.append(subprocess.Popen(
                    [sys.executable, "-c", worker, str(self.store.root), draft["id"], str(index)],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 10
            while not all((self.store.root / f"ready-{index}").exists() for index in range(2)):
                self.assertLess(time.monotonic(), deadline, "Both workers must reach the start barrier")
                self.assertTrue(all(process.poll() is None for process in processes))
                time.sleep(0.01)
            (self.store.root / "start").touch(mode=0o600)
            results = [process.communicate(timeout=10) for process in processes]
            self.assertEqual(sorted(process.returncode for process in processes), [0, 1], results)
            self.assertEqual((self.store.root / "synthetic-writes").read_text(), "create\n")
            self.assertEqual(self.store.load_draft(draft["id"])["status"], "applied")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


class ReceiptAcceptance(OfflineCase):
    def test_control_pure_refund_allocations_use_positive_milliunits(self):
        value = proposal()
        refund = dict(TRANSACTION, amount=43250)
        value["expected"]["amount_milliunits"] = 43250
        result = validate_receipt_proposal(value, transaction=refund,
                                           plan_id=value["plan_id"], decimal_digits=2,
                                           resolve_category=category)
        self.assertEqual([line["amount_milliunits"] for line in result["allocations"]],
                         [30000, 13250])

    def test_control_split_comparison_ignores_generated_ids_but_preserves_duplicates(self):
        line = {"amount": -1000, "category_id": "synthetic-category", "memo": "Synthetic"}
        other = dict(line, memo="Other")
        expected = _canonical_subtransactions([line, line, other])
        actual = _canonical_subtransactions([
            dict(other, id="generated-3"), dict(line, id="generated-2"),
            dict(line, id="generated-1"), dict(line, deleted=True)])
        self.assertEqual(expected, actual)
        self.assertNotEqual(expected, _canonical_subtransactions([line, other]))

    def test_multiple_tenders_without_account_binding_are_blocked(self):
        value = proposal()
        value["receipt"]["tenders"] = [
            {"label": "Gift card", "amount": "10.00"},
            {"label": "Example Account A", "amount": "33.25"},
        ]
        # The selected Example Account A transaction is 43.25, not the receipt's Example Account A tender.
        with self.assertRaises(ReceiptProposalError,
                               msg="Reconciled receipt total alone does not validate the selected tender"):
            validate_receipt_proposal(value, transaction=TRANSACTION,
                                      plan_id=value["plan_id"], decimal_digits=2,
                                      resolve_category=category)

    def test_control_unresolved_evidence_blocks_proposal(self):
        value = proposal()
        value["receipt"]["unresolved"] = ["Unknown taxable item"]
        with self.assertRaises(ReceiptProposalError):
            validate_receipt_proposal(value, transaction=TRANSACTION,
                                      plan_id=value["plan_id"], decimal_digits=2,
                                      resolve_category=category)


class StorageAcceptance(OfflineCase):
    def test_unknown_filesystem_is_rejected_without_accessing_network_storage(self):
        # Only the mount-command response is simulated. No mount or remote path is accessed.
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            response = subprocess.CompletedProcess(["/sbin/mount"], 0,
                stdout="synthetic-device on / (unrecognizedfs, nodev)\n", stderr="")
            with patch("budget_tools.state.subprocess.run", return_value=response):
                with self.assertRaises(StateError, msg="Unknown filesystem destinations must fail closed"):
                    assert_local_storage(Path(directory))

    def test_symlinked_draft_directory_is_rejected_before_write(self):
        # Both directories are synthetic local temp paths; no protected data is touched.
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory) / "state"
            outside = Path(directory) / "outside-state"
            root.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            store = StateStore(root)
            (root / "drafts").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(StateError, msg="Symlinked state directories must be rejected before writing"):
                store.save_draft({"id": str(uuid.uuid4()), "synthetic": True})
            self.assertEqual(list(outside.iterdir()), [])


class TransportAcceptance(OfflineCase):
    def test_control_cross_host_and_downgrade_redirects_never_follow(self):
        real_build_opener = urllib.request.build_opener
        for status in (301, 302, 303, 307, 308):
            for destination in ("https://synthetic.invalid/path", "http://synthetic.invalid/path"):
                with self.subTest(status=status, destination=destination):
                    requests = []

                    class OfflineHTTPS(urllib.request.BaseHandler):
                        handler_order = 100

                        def https_open(self, request):
                            requests.append(request.full_url)
                            headers = Message()
                            headers["Location"] = destination
                            response = urllib.response.addinfourl(
                                io.BytesIO(b""), headers, request.full_url, status)
                            response.msg = "Synthetic redirect"
                            return response

                    def opener(*handlers):
                        return real_build_opener(urllib.request.ProxyHandler({}), OfflineHTTPS(), *handlers)

                    with patch("urllib.request.build_opener", side_effect=opener):
                        with self.assertRaises(ApiError):
                            YnabClient("SYNTHETIC-NOT-A-CREDENTIAL").get_user()
                    self.assertEqual(requests, ["https://api.ynab.com/v1/user"])


if __name__ == "__main__":
    unittest.main()
