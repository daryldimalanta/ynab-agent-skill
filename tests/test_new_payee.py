# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Confirmed missing payees through the real CLI/client and an offline API contract."""
import copy
import io
import json
import tempfile
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from budget_tools.api import ApiError, YnabClient
from budget_tools.rate_limit import RateLimiter
from budget_tools.cli import build_parser
from budget_tools.state import StateError, StateStore, TERMS_VERSION
from tests import test_receipt_contract_workflow as contract
from tests.test_rate_limit import Clock
from tests.test_receipt_contract_workflow import (
    BASE, PLAN, PAYEE, TX, ContractServer, raw_proposal,
)
from validation.test_post_implementation_safety import OfflineCase

NAME = "Fictional Café & Market"
NEW_ID = "30000000-0000-0000-0000-000000000099"


class PayeeServer(ContractServer):
    def __init__(self):
        super().__init__()
        self.payees = [{"id": PAYEE, "name": "Example Shop", "deleted": False}]
        self.payee_creates = 0
        self.ids_only = False
        self.lose_response = False
        self.fail_readback = False
        self.alter_created = lambda row: None
        self.after_ack = lambda row: None

    def transport(self, method, url, headers, body, timeout):
        path = urlsplit(url).path
        if method == "GET" and "/transactions/" in path and self.fail_readback:
            self.fail_readback = False
            raise ApiError("Synthetic failed readback")
        response = super().transport(method, url, headers, body, timeout)
        if method == "GET" and path == BASE + "/payees":
            return self.response({"payees": self.payees})
        if method == "POST" and path == BASE + "/transactions":
            payload = json.loads(body)["transaction"]
            if payload.get("payee_id") is None:
                assert isinstance(payload.get("payee_name"), str) and payload["payee_name"]
                assert "import_id" not in payload, "New payees must not enable import rename rules"
                matches = [p for p in self.payees if p["name"].casefold() == payload["payee_name"].casefold()]
                if matches:
                    payee = matches[0]
                else:
                    payee = {"id": NEW_ID, "name": payload["payee_name"], "deleted": False}
                    self.payees.append(payee)
                    self.payee_creates += 1
            else:
                payee = next(p for p in self.payees if p["id"] == payload["payee_id"])
            row = self.records["new-synthetic-entry"]
            row.update(payee_id=payee["id"], payee_name=payee["name"])
            self.alter_created(row)
            if self.lose_response:
                raise ApiError("Synthetic lost creation response")
            data = {"transaction_ids": [row["id"]]} if self.ids_only else {"transaction": copy.deepcopy(row)}
            response = self.response(data, 201)
            self.after_ack(row)
        if method == "PUT" and "/transactions/" in path:
            payload = json.loads(body)["transaction"]
            if payload.get("payee_id", object()) is None and payload.get("payee_name"):
                matches = [p for p in self.payees if p["name"].casefold() == payload["payee_name"].casefold()]
                payee = matches[0] if matches else {"id": NEW_ID, "name": payload["payee_name"], "deleted": False}
                if not matches:
                    self.payees.append(payee)
                    self.payee_creates += 1
                row = self.records[path.rsplit("/", 1)[1]]
                row.update(payee_id=payee["id"], payee_name=payee["name"])
                response = self.response({"transaction": copy.deepcopy(row)})
        return response


class NewPayeeTests(OfflineCase):
    run_cli = contract.ReceiptContractWorkflow.run_cli
    ok = contract.ReceiptContractWorkflow.ok
    save_proposal = contract.ReceiptContractWorkflow.save_proposal

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore(self.root / "state")
        self.store.save_config({"terms_acknowledged_version": TERMS_VERSION, "plan_id": PLAN})
        self.server = PayeeServer()
        self.clock = Clock()
        self.limiter = RateLimiter(self.store, "SYNTHETIC", clock=self.clock,
                                   sleep=self.clock.sleep, notify=lambda _: None)
        self.transport = self.server.transport
        self.client = YnabClient("SYNTHETIC-AUDIT-ONLY", transport=lambda *args: self.transport(*args),
                                 rate_limiter=self.limiter)
        self.value = raw_proposal()
        self.value["transaction_id"] = None
        self.value["receipt"]["match_evidence"] = "Explicit synthetic new entry request and bounded duplicate check"
        self.path = self.root / "proposal.json"

    def draft(self, name=NAME, flag="--new-payee", direction="--outflow"):
        self.save_proposal()
        return self.run_cli("transactions", "draft-receipt-create", "--account", "Example Account B", flag, name,
                            "--date", "2001-02-02", direction, "28.80", "--proposal", str(self.path))

    def draft_id(self):
        code, draft, error = self.draft()
        self.assertEqual((code, error), (0, ""))
        return draft["id"]

    def apply(self, draft_id):
        return self.run_cli("drafts", "apply", draft_id, "--confirm", draft_id)

    def reject_once(self, predicate):
        rejected = []
        def transport(*args):
            if not rejected and predicate(args):
                rejected.append(True)
                return 429, b'{}', {}
            return self.server.transport(*args)
        self.transport = transport

    def test_missing_existing_payee_asks_for_consent_without_drafting_or_writing(self):
        code, _, error = self.draft(flag="--payee")
        self.assertEqual(code, 1)
        self.assertIn("Ask the user", error)
        self.assertEqual(self.store.list_drafts(), [])
        self.assertEqual(self.server.writes(), [])

    def test_preview_and_show_expose_exact_new_payee_without_writes(self):
        draft_id = self.draft_id()
        shown = self.ok("drafts", "show", draft_id)
        self.assertTrue(shown["summary"]["creates_new_payee"])
        self.assertEqual(shown["summary"]["payee"], NAME)
        self.assertEqual(shown["changes"]["new_payee"], {"name": NAME, "create_if_missing": True})
        stored = self.store.load_draft(draft_id)
        self.assertIsNone(stored["payload"]["payee_id"])
        self.assertEqual(stored["payload"]["payee_name"], NAME)
        self.assertEqual(self.server.writes(), [])
        self.assertEqual(self.server.payee_creates, 0)

    def test_wrong_confirmation_does_not_create_payee_or_transaction(self):
        draft_id = self.draft_id()
        self.assertEqual(self.run_cli("drafts", "apply", draft_id, "--confirm", "wrong")[0], 1)
        self.assertEqual(self.server.writes(), [])

    def test_existing_payee_route_remains_id_based(self):
        code, draft, error = self.draft(name="Example Shop", flag="--payee")
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("creates_new_payee", draft["summary"])
        payload = self.store.load_draft(draft["id"])["payload"]
        self.assertEqual(payload["payee_id"], PAYEE)
        self.assertNotIn("payee_name", payload)
        self.assertEqual(self.apply(draft["id"])[0], 0)
        self.assertEqual(self.server.payee_creates, 0)

    def test_existing_transaction_update_creates_and_verifies_missing_payee(self):
        code, draft, error = self.run_cli(
            "transactions", "draft-update", TX, "--new-payee", NAME,
            "--memo", "Fictional purchase note (Example Marker)",
        )
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(draft["summary"]["creates_new_payee"])
        self.assertEqual(draft["changes"]["new_payee"], {"name": NAME, "create_if_missing": True})
        result = self.apply(draft["id"])[1]
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(result["transaction"]["payee_name"], NAME)
        self.assertEqual(result["transaction"]["memo"], "Fictional purchase note (Example Marker)")
        self.assertEqual(self.server.payee_creates, 1)
        self.assertEqual([call[0] for call in self.server.writes()], ["PUT"])

    def test_success_creates_once_then_verifies_and_approves(self):
        draft_id = self.draft_id()
        code, result, error = self.apply(draft_id)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(result["transaction"]["payee_name"], NAME)
        self.assertEqual(result["transaction"]["payee_id"], NEW_ID)
        self.assertEqual(self.server.payee_creates, 1)
        self.assertEqual([x[0] for x in self.server.writes()], ["POST", "PUT"])
        self.assertEqual(self.store.load_draft(draft_id)["resolved_payee_id"], NEW_ID)
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.server.payee_creates, 1)

    def test_new_payee_refund(self):
        self.value["expected"]["amount_milliunits"] = 28800
        code, draft, error = self.draft(direction="--inflow")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(self.apply(draft["id"])[1]["transaction"]["amount_milliunits"], 28800)

    def test_approval_false_stops_after_verification(self):
        self.value["approve_after_verification"] = False
        code, result, error = self.apply(self.draft_id())
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "verified_categorization")
        self.assertEqual([x[0] for x in self.server.writes()], ["POST"])

    def test_invalid_names_never_write(self):
        for name in ("", " ", " Leading", "Trailing ", "Line\nbreak", "Tab\tname", "x" * 501,
                     "Transfer: Example Account C", "tRaNsFeR: Savings"):
            with self.subTest(name=repr(name[:30])):
                self.assertEqual(self.draft(name=name)[0], 1)
        self.assertEqual(self.store.list_drafts(), [])
        self.assertEqual(self.server.writes(), [])

    def test_existing_ambiguous_deleted_and_transfer_names_are_not_new(self):
        for records in (
            [{"id": PAYEE, "name": NAME}],
            [{"id": PAYEE, "name": NAME.lower()}],
            [{"id": PAYEE, "name": " " + NAME + " "}],
            [{"id": PAYEE, "name": NAME, "deleted": True}],
            [{"id": PAYEE, "name": NAME, "transfer_account_id": "other"}],
            [{"id": PAYEE, "name": NAME}, {"id": NEW_ID, "name": NAME}],
        ):
            with self.subTest(records=records):
                self.server.payees = records
                code, _, error = self.draft()
                self.assertEqual(code, 1)
                self.assertIn("already exists", error)
        self.assertEqual(self.server.writes(), [])

    def test_ambiguous_existing_lookup_never_offers_automatic_creation(self):
        self.server.payees = [{"id": PAYEE, "name": NAME}, {"id": NEW_ID, "name": NAME}]
        code, _, error = self.draft(flag="--payee")
        self.assertEqual(code, 1)
        self.assertIn("Multiple payee", error)
        self.assertNotIn("--new-payee", error)
        self.assertEqual(self.server.writes(), [])

    def test_new_name_and_intent_are_sealed(self):
        draft_id = self.draft_id()
        path = self.store.draft_path(draft_id)
        original = path.read_text()
        for field in ("payload", "changes", "summary"):
            tampered = json.loads(original)
            tampered[field]["payee_name"] = "Changed name"
            path.write_text(json.dumps(tampered))
            self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.server.writes(), [])

    def test_new_payee_appearing_before_apply_requires_fresh_review(self):
        draft_id = self.draft_id()
        self.server.payees.append({"id": NEW_ID, "name": NAME})
        code, _, error = self.apply(draft_id)
        self.assertEqual(code, 1)
        self.assertIn("fresh draft", error)
        self.assertEqual(self.server.writes(), [])

    def test_new_payee_appearing_during_429_wait_blocks_retry(self):
        draft_id = self.draft_id()
        self.reject_once(lambda args: args[0] == "POST")
        self.clock.on_sleep = lambda: self.server.payees.append({"id": NEW_ID, "name": NAME})
        code, _, error = self.apply(draft_id)
        self.assertEqual(code, 1)
        self.assertIn("already exists", error)
        self.assertEqual(self.server.writes(), [])
        self.assertEqual(self.store.load_draft(draft_id)["status"], "pending")

    def test_429_creation_and_readback_create_once(self):
        draft_id = self.draft_id()
        rejected = set()
        def transport(*args):
            kind = "post" if args[0] == "POST" else "read" if args[0] == "GET" and "/transactions/" in args[1] else None
            if kind and kind not in rejected:
                rejected.add(kind)
                return 429, b'{}', {}
            return self.server.transport(*args)
        self.transport = transport
        self.assertEqual(self.apply(draft_id)[1]["stage"], "verified_approval")
        self.assertEqual(self.server.payee_creates, 1)
        self.assertEqual([x[0] for x in self.server.writes()], ["POST", "PUT"])

    def test_lost_response_never_replays_or_guesses_identity(self):
        draft_id = self.draft_id()
        self.server.lose_response = True
        self.assertEqual(self.apply(draft_id)[0], 1)
        stored = self.store.load_draft(draft_id)
        self.assertEqual(stored["status"], "uncertain")
        self.assertIsNone(stored["transaction_id"])
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.run_cli("drafts", "reconcile", draft_id)[0], 1)
        self.assertEqual(self.server.payee_creates, 1)
        self.assertEqual(len(self.server.writes()), 1)

    def test_full_ack_pins_identity_before_failed_readback(self):
        draft_id = self.draft_id()
        self.server.fail_readback = True
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.store.load_draft(draft_id)["resolved_payee_id"], NEW_ID)
        self.assertEqual(self.ok("drafts", "reconcile", draft_id)["status"], "approval_pending")
        self.assertEqual(self.ok("drafts", "resume", draft_id, "--confirm", draft_id)["stage"], "verified_approval")
        self.assertEqual(self.server.payee_creates, 1)

    def test_id_only_ack_recovers_and_pins_on_verified_readback(self):
        draft_id = self.draft_id()
        self.server.ids_only = True
        self.server.fail_readback = True
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertNotIn("resolved_payee_id", self.store.load_draft(draft_id))
        self.assertEqual(self.ok("drafts", "reconcile", draft_id)["status"], "approval_pending")
        self.assertEqual(self.store.load_draft(draft_id)["resolved_payee_id"], NEW_ID)
        self.assertEqual(self.ok("drafts", "resume", draft_id, "--confirm", draft_id)["stage"], "verified_approval")
        self.assertEqual(self.server.payee_creates, 1)

    def test_wrong_name_or_missing_id_blocks_approval(self):
        for changes in ({"payee_name": "Wrong merchant"}, {"payee_id": None},
                        {"payee_id": ""}, {"payee_id": " "}, {"payee_id": 42},
                        {"transfer_account_id": "other"}):
            with self.subTest(changes=changes):
                self.server.payees = []
                draft_id = self.draft_id()
                self.server.alter_created = lambda row: row.update(changes)
                before = len(self.server.writes())
                code, result, error = self.apply(draft_id)
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(result["stage"], "categorization_verification_failed")
                self.assertEqual(len(self.server.writes()) - before, 1)
                self.assertEqual(self.run_cli("drafts", "reconcile", draft_id)[0], 1)

    def test_same_name_different_id_after_ack_blocks_approval(self):
        draft_id = self.draft_id()
        self.server.after_ack = lambda row: row.update(payee_id=PAYEE)
        result = self.apply(draft_id)[1]
        self.assertEqual(result["stage"], "categorization_verification_failed")
        self.assertIn("payee_id", result["categorization_verification"]["mismatches"])
        self.assertEqual(len(self.server.writes()), 1)

    def test_name_changed_before_approval_blocks_approval(self):
        draft_id = self.draft_id()
        reads = []
        def transport(*args):
            if args[0] == "GET" and "/transactions/" in args[1]:
                reads.append(True)
                if len(reads) == 2:
                    self.server.records["new-synthetic-entry"]["payee_name"] = "Changed name"
            return self.server.transport(*args)
        self.transport = transport
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(len(self.server.writes()), 1)

    def test_approval_failure_resumes_without_creating_again(self):
        draft_id = self.draft_id()
        self.transport = lambda *args: (400, b'{}') if args[0] == "PUT" else self.server.transport(*args)
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.transport = self.server.transport
        self.assertEqual(self.ok("drafts", "reconcile", draft_id)["status"], "approval_pending")
        self.assertEqual(self.ok("drafts", "resume", draft_id, "--confirm", draft_id)["stage"], "verified_approval")
        self.assertEqual(self.server.payee_creates, 1)

    def test_rejected_create_never_automatically_retries(self):
        draft_id = self.draft_id()
        calls = []
        def transport(*args):
            if args[0] == "POST":
                calls.append(True)
                return 400, b'{}'
            return self.server.transport(*args)
        self.transport = transport
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.store.load_draft(draft_id)["status"], "failed")
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(len(calls), 1)

    def test_expired_draft_never_creates_payee(self):
        draft_id = self.draft_id()
        with patch("budget_tools.cli.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=25)
            self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.server.writes(), [])

    def test_invalid_receipt_blocks_new_payee_draft(self):
        self.value["receipt"]["unresolved"] = ["Unknown receipt date"]
        self.assertEqual(self.draft()[0], 1)
        self.assertEqual(self.store.list_drafts(), [])
        self.assertEqual(self.server.writes(), [])

    def test_payee_options_are_required_mutually_exclusive_and_receipt_only(self):
        args = ["transactions", "draft-receipt-create", "--account", "Example Account B",
                "--date", "2001-02-02", "--outflow", "28.80", "--proposal", str(self.path)]
        for values in (args, args + ["--payee", "Example Shop", "--new-payee", NAME],
                       ["transactions", "draft-create", "--account", "Example Account B", "--date", "2001-02-02",
                        "--outflow", "28.80", "--new-payee", NAME]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                build_parser().parse_args(values)
            self.assertEqual(error.exception.code, 2)

    def test_proactive_wait_rechecks_missing_name(self):
        draft_id = self.draft_id()
        first = []
        def transport(*args):
            result = self.server.transport(*args)
            if args[0] == "GET" and not first:
                first.append(True)
                return (*result, {"X-Rate-Limit": "200/200"})
            return result
        self.transport = transport
        self.clock.on_sleep = lambda: self.server.payees.append({"id": NEW_ID, "name": NAME})
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.server.writes(), [])

    def test_expiry_during_429_wait_prevents_creation(self):
        draft_id = self.draft_id()
        created = datetime.fromisoformat(self.store.load_draft(draft_id)["created_at"])
        start, clock = self.clock.now, self.clock
        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return created + timedelta(hours=23, minutes=30, seconds=clock.now - start)
        self.reject_once(lambda args: args[0] == "POST")
        with patch("budget_tools.cli.datetime", FakeDatetime):
            self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.server.writes(), [])

    def test_identity_change_during_approval_wait_blocks_approval(self):
        draft_id = self.draft_id()
        self.reject_once(lambda args: args[0] == "PUT")
        self.clock.on_sleep = lambda: self.server.records["new-synthetic-entry"].update(payee_id=PAYEE)
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual([x[0] for x in self.server.writes()], ["POST"])

    def test_identity_changed_after_failed_approval_blocks_reconcile(self):
        draft_id = self.draft_id()
        self.transport = lambda *args: (400, b'{}') if args[0] == "PUT" else self.server.transport(*args)
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.transport = self.server.transport
        self.server.records["new-synthetic-entry"]["payee_id"] = PAYEE
        self.assertEqual(self.run_cli("drafts", "reconcile", draft_id)[0], 1)
        self.assertEqual(self.run_cli("drafts", "resume", draft_id, "--confirm", draft_id)[0], 1)
        self.assertEqual(len(self.server.writes()), 1)

    def test_new_payee_checkpoint_failure_prevents_send(self):
        draft_id = self.draft_id()
        with patch.object(self.store, "save_draft", side_effect=StateError("Synthetic persistence failure")):
            self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.server.writes(), [])

    def test_interrupted_creation_never_replays(self):
        draft_id = self.draft_id()
        def transport(*args):
            result = self.server.transport(*args)
            if args[0] == "POST":
                raise KeyboardInterrupt()
            return result
        self.transport = transport
        self.assertEqual(self.apply(draft_id)[0], 130)
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.server.payee_creates, 1)
        self.assertEqual(len(self.server.writes()), 1)

    def test_boundary_name_preserves_unicode_punctuation_and_500_characters(self):
        name = "Café & Market's " + "x" * 483 + "é"
        self.assertEqual(len(name), 500)
        code, draft, error = self.draft(name=name)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(self.apply(draft["id"])[1]["transaction"]["payee_name"], name)
