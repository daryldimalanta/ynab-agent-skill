# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Synthetic regression coverage for the staged repair pass."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from budget_tools import state
from budget_tools.api import ApiError
from validation import test_post_implementation_safety as review
from validation.test_post_implementation_safety import OfflineCase
from tests.test_receipts import proposal


class StorageRepairs(OfflineCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = state.StateStore(self.root / "state")
        self.store.ensure()

    def test_symlinked_locks_and_audit_rejected(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        self.store.locks_dir.rmdir()
        self.store.locks_dir.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(state.StateError):
            with self.store.exclusive_lock("synthetic"):
                self.fail("Must not acquire a redirected lock")
        self.assertEqual(list(outside.iterdir()), [])
        self.store.locks_dir.unlink()
        self.store.ensure()
        target = outside / "audit"
        target.write_text("unchanged")
        self.store.audit_path.symlink_to(target)
        with self.assertRaises(state.StateError):
            self.store.append_audit({"synthetic": True})
        self.assertEqual(target.read_text(), "unchanged")

    def test_hardlinked_or_symlinked_config_is_never_overwritten(self):
        outside = self.root / "original"
        outside.write_text("{}")
        outside.chmod(0o600)
        os.link(outside, self.store.config_path)
        with self.assertRaises(state.StateError):
            self.store.save_config({"synthetic": True})
        self.assertEqual(outside.read_text(), "{}")
        self.store.config_path.unlink()
        self.store.config_path.symlink_to(outside)
        with self.assertRaises(state.StateError):
            self.store.save_config({"synthetic": True})
        self.assertEqual(outside.read_text(), "{}")

    def test_proposal_parent_symlink_is_rejected(self):
        real = self.root / "actual"
        real.mkdir(mode=0o700)
        proposal = real / "proposal.json"
        proposal.write_text("{}")
        proposal.chmod(0o600)
        link = self.root / "alias"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(state.StateError):
            state.read_private_json(link / proposal.name)

    def test_changed_parent_during_atomic_write_blocks_publication(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        saved = self.store.drafts_dir / (str(uuid.uuid4()) + ".json")
        real_fsync = os.fsync
        swapped = False

        def swap_after_flush(descriptor):
            nonlocal swapped
            real_fsync(descriptor)
            if not swapped:
                swapped = True
                self.store.drafts_dir.rename(self.store.root / "original-drafts")
                self.store.drafts_dir.symlink_to(outside, target_is_directory=True)

        with patch("budget_tools.state.os.fsync", side_effect=swap_after_flush):
            with self.assertRaises(state.StateError):
                self.store._atomic_json(saved, {"synthetic": True})
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list((self.store.root / "original-drafts").iterdir()), [])

    def test_network_mount_metadata_rejected_before_open(self):
        metadata = "synthetic on / (apfs, local)\nsynthetic on /private/tmp (smbfs)\n"
        response = subprocess.CompletedProcess([], 0, stdout=metadata, stderr="")
        with patch("budget_tools.state.subprocess.run", return_value=response):
            with patch("budget_tools.state.os.open") as opened:
                with self.assertRaises(state.StateError):
                    with state._directory(self.store.root):
                        self.fail("Must not open a nonlocal destination")
                opened.assert_not_called()

    def test_private_modes_and_durable_json_round_trip(self):
        self.store.save_config({"synthetic": True})
        self.assertEqual(self.store.load_config(), {"synthetic": True})
        self.assertEqual(self.store.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)


class WorkflowRepairs(OfflineCase):
    run_cli = review.WorkflowAcceptance.run_cli
    make_receipt_draft = review.WorkflowAcceptance.make_receipt_draft
    apply = review.WorkflowAcceptance.apply

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = state.StateStore(self.root / "state")
        self.client = review.ReviewClient()
        self.store.save_config({"terms_acknowledged_version": state.TERMS_VERSION,
                                "plan_id": self.client.plan_id})
        value = proposal()
        value["allocations"][1]["category"] = "Example Group A: Example Category B"
        self.proposal_path = self.root / "synthetic-proposal.json"
        self.proposal_path.write_text(json.dumps(value))
        self.proposal_path.chmod(0o600)

    def test_approval_timeout_after_commit_reconciles_without_replay(self):
        draft_id = self.make_receipt_draft(existing=True)
        original = self.client.update_transaction

        def lost_response(plan, transaction, payload):
            original(plan, transaction, payload)
            raise ApiError("Synthetic response lost after commit")

        self.client.update_transaction = lost_response
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.store.load_draft(draft_id)["status"], "approval_uncertain")
        code, result, error = self.run_cli("drafts", "reconcile", draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(len(self.client.writes), 1)
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(len(self.client.writes), 1)

    def test_approval_timeout_before_commit_resumes_only_approval(self):
        draft_id = self.make_receipt_draft()
        original = self.client.update_transaction

        def timeout(plan, transaction, payload):
            if payload == {"approved": True}:
                raise ApiError("Synthetic timeout before commit")
            return original(plan, transaction, payload)

        self.client.update_transaction = timeout
        self.assertEqual(self.apply(draft_id)[0], 1)
        code, result, error = self.run_cli("drafts", "reconcile", draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["status"], "approval_pending")
        self.client.update_transaction = original
        code, result, error = self.run_cli("drafts", "resume", draft_id, "--confirm", draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(sum("subtransactions" in write for write in self.client.writes), 1)

    def test_never_applied_draft_cannot_reconcile_to_completion(self):
        draft_id = self.make_receipt_draft(existing=True)
        with patch.object(self.client, "get_transaction", side_effect=AssertionError("No API needed")):
            self.assertEqual(self.run_cli("drafts", "reconcile", draft_id)[0], 1)
        self.assertEqual(self.store.load_draft(draft_id)["status"], "pending")
        self.assertEqual(self.client.writes, [])

    def test_generic_unrequested_parent_changes_fail_verification(self):
        for field, value in (("amount", -99990), ("account_id", "other-account"),
                             ("payee_id", "other-payee"), ("deleted", True),
                             ("cleared", "reconciled")):
            with self.subTest(field=field):
                self.client = review.ReviewClient()
                code, draft, error = self.run_cli("transactions", "draft-update",
                    self.client.transaction["id"], "--memo", "Synthetic edit")
                self.assertEqual((code, error), (0, ""))
                original = self.client.update_transaction

                def changed(plan, transaction, payload):
                    result = original(plan, transaction, payload)
                    self.client.transaction[field] = value
                    return result

                self.client.update_transaction = changed
                code, result, error = self.apply(draft["id"])
                self.assertEqual((code, error), (0, ""))
                self.assertFalse(result["verification"]["verified"])
                self.assertIn(field, result["verification"]["mismatches"])

    def test_receipt_intervening_parent_changes_stop_approval(self):
        for field, value in (("deleted", True), ("flag_color", "red"),
                             ("transfer_account_id", "other-account"),
                             ("matched_transaction_id", "different-match")):
            with self.subTest(field=field):
                self.client = review.ReviewClient()
                draft_id = self.make_receipt_draft()
                original = self.client.get_transaction

                def changed(plan, transaction):
                    result = original(plan, transaction)
                    if self.client.split_reads == 2:
                        self.client.transaction[field] = value
                        result[field] = value
                    return result

                self.client.get_transaction = changed
                self.assertEqual(self.apply(draft_id)[0], 1)
                self.assertFalse(self.client.transaction["approved"])
                self.assertEqual(len(self.client.writes), 1)

    def test_wrong_response_id_retains_original_recovery_target(self):
        original_id = self.client.transaction["id"]
        code, draft, error = self.run_cli("transactions", "draft-update", original_id,
                                          "--memo", "Synthetic edit")
        self.assertEqual((code, error), (0, ""))
        original = self.client.update_transaction

        def wrong_id(plan, transaction, payload):
            result = original(plan, transaction, payload)
            result["data"]["transaction"]["id"] = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
            return result

        self.client.update_transaction = wrong_id
        self.assertEqual(self.apply(draft["id"])[0], 1)
        self.assertEqual(self.store.load_draft(draft["id"])["transaction_id"], original_id)
        code, result, error = self.run_cli("drafts", "reconcile", draft["id"])
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(len(self.client.writes), 1)

    def test_explicit_amount_update_is_allowed_and_verified(self):
        code, draft, error = self.run_cli("transactions", "draft-update", self.client.transaction["id"],
                                          "--outflow", "43.00")
        self.assertEqual((code, error), (0, ""))
        code, result, error = self.apply(draft["id"])
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(self.client.transaction["amount"], -43000)

    def test_reviewed_payload_cannot_change_under_same_id(self):
        draft_id = self.make_receipt_draft()
        draft = self.store.load_draft(draft_id)
        draft["payload"]["memo"] = "Not reviewed"
        with self.assertRaisesRegex(state.StateError, "content changed"):
            self.store.save_draft(draft)
        self.assertEqual(self.store.load_draft(draft_id)["payload"]["memo"], "Receipt reviewed")
        self.assertEqual(self.client.writes, [])

    def test_raw_draft_change_is_detected_before_api_access(self):
        draft_id = self.make_receipt_draft()
        draft = self.store.load_draft(draft_id)
        draft["proposal"]["approve_after_verification"] = False
        # Deliberately bypass the public save guard to simulate local corruption.
        self.store._atomic_json(self.store.draft_path(draft_id), draft)
        with patch.object(self.client, "get_transaction", side_effect=AssertionError("No API access")):
            code, _, error = self.apply(draft_id)
        self.assertEqual(code, 1)
        self.assertIn("content changed", error)
        self.assertEqual(self.client.writes, [])

    def test_state_write_failure_before_request_sends_nothing(self):
        draft_id = self.make_receipt_draft()
        with patch.object(self.store, "save_draft", side_effect=state.StateError("Synthetic disk failure")):
            self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.store.load_draft(draft_id)["status"], "pending")

    def test_state_failure_after_commit_blocks_replay_and_can_reconcile_update(self):
        draft_id = self.make_receipt_draft()
        original = self.store.save_draft

        def fail_acknowledgment(draft):
            if draft.get("stage") == "write_acknowledged":
                raise state.StateError("Synthetic disk failure after server commit")
            return original(draft)

        with patch.object(self.store, "save_draft", side_effect=fail_acknowledgment):
            self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.store.load_draft(draft_id)["status"], "applying")
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(len(self.client.writes), 1)
        code, result, error = self.run_cli("drafts", "reconcile", draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["status"], "approval_pending")
        self.assertEqual(self.run_cli("drafts", "resume", draft_id, "--confirm", draft_id)[0], 0)
        self.assertEqual(sum("subtransactions" in item for item in self.client.writes), 1)

    def test_unusable_category_ids_and_groups_block_receipt_drafts(self):
        for scope, field in (("category", "hidden"), ("category", "deleted"),
                             ("category", "internal"), ("group", "hidden"),
                             ("group", "deleted"), ("group", "internal")):
            with self.subTest(scope=scope, field=field):
                self.client = review.ReviewClient()
                groups = self.client.get_categories(self.client.plan_id)
                target = groups[0]["categories"][0] if scope == "category" else groups[0]
                target[field] = True
                self.client.get_categories = lambda plan: groups
                value = proposal()
                value["allocations"][0]["category"] = groups[0]["categories"][0]["id"]
                self.proposal_path.write_text(json.dumps(value))
                code, _, _ = self.run_cli("transactions", "draft-receipt-split", self.client.transaction["id"],
                                          "--proposal", str(self.proposal_path))
                self.assertEqual(code, 1)
                self.assertEqual(self.client.writes, [])

    def test_two_processes_with_different_drafts_serialize_one_transaction(self):
        drafts = []
        for memo in ("Synthetic first", "Synthetic second"):
            code, draft, error = self.run_cli("transactions", "draft-update", self.client.transaction["id"],
                                              "--memo", memo)
            self.assertEqual((code, error), (0, ""))
            drafts.append(draft["id"])
        server = self.store.root / "synthetic-server.json"
        server.write_text(json.dumps({"transaction": self.client.transaction, "writes": 0}))
        server.chmod(0o600)
        worker = r'''
import fcntl, json, os, sys, time, traceback
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch
from budget_tools.cli import main
import budget_tools.cli as cli
from budget_tools.state import StateStore
from tests.test_budget_tools import FakeClient
def forbidden(*args, **kwargs):
    raise AssertionError("Offline worker forbids network/credentials")
original_command = cli.cmd_drafts
def traced_command(*args):
    try:
        return original_command(*args)
    except Exception:
        traceback.print_exc()
        raise
cli.cmd_drafts = traced_command
class SharedClient(FakeClient):
    def get_transaction(self, plan_id, transaction_id):
        self._check_plan(plan_id)
        with server.open() as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            record = json.load(handle)
        self.transaction = record["transaction"]
        self._check_transaction(transaction_id)
        return dict(self.transaction)
    def update_transaction(self, plan_id, transaction_id, payload):
        # Widen the race: without the transaction lock both callers would pass preflight.
        time.sleep(0.15)
        with server.open("r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            record = json.load(handle)
            self.transaction = record["transaction"]
            result = super().update_transaction(plan_id, transaction_id, payload)
            record["transaction"] = self.transaction
            record["writes"] += 1
            handle.seek(0)
            json.dump(record, handle)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        return result
with patch("socket.create_connection", forbidden), patch("socket.socket.connect", forbidden), \
     patch("socket.getaddrinfo", forbidden), patch("budget_tools.utils.read_access_token", forbidden), \
     patch("budget_tools.state.subprocess.run", return_value=CompletedProcess(
         [], 0, stdout="synthetic on /private/tmp (apfs, local)\n", stderr="")), \
     patch("budget_tools.utils._client", return_value=SharedClient()):
    store = StateStore(Path(sys.argv[1]))
    server = store.root / "synthetic-server.json"
    draft_id, index = sys.argv[2:4]
    (store.root / ("ready-" + index)).touch(mode=0o600)
    deadline = time.monotonic() + 10
    while not (store.root / "start").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Synthetic start barrier timed out")
        time.sleep(0.01)
    sys.exit(main(["drafts", "apply", draft_id, "--confirm", draft_id], store=store))
'''
        processes = []
        try:
            for index, draft_id in enumerate(drafts):
                processes.append(subprocess.Popen(
                    [sys.executable, "-c", worker, str(self.store.root), draft_id, str(index)],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 10
            while not all((self.store.root / f"ready-{index}").exists() for index in range(2)):
                self.assertLess(time.monotonic(), deadline)
                self.assertTrue(all(process.poll() is None for process in processes))
                time.sleep(0.01)
            (self.store.root / "start").touch(mode=0o600)
            outputs = [process.communicate(timeout=10) for process in processes]
            self.assertEqual(sorted(process.returncode for process in processes), [0, 1], outputs)
            self.assertEqual(json.loads(server.read_text())["writes"], 1)
            self.assertEqual(sorted(self.store.load_draft(item)["status"] for item in drafts),
                             ["applied", "pending"])
            self.assertTrue(any("changed after this draft" in error for _, error in outputs), outputs)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
