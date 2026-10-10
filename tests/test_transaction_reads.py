# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Synthetic posted recurring entries through the real client and CLI."""
import copy
import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from budget_tools import cli
from budget_tools.api import ApiError, YnabClient
from budget_tools.state import StateStore, TERMS_VERSION
from tests.test_receipt_contract_workflow import (
    ACCOUNT, BASE, FOOD, OTHER_ACCOUNT, PLAN, SHARED, TX, ContractServer, raw_proposal,
)
from validation.test_post_implementation_safety import OfflineCase

# The suffix is identity, not a date filter: the record's date may be edited.
POSTED = TX + "_2001-01-25"


class PostedTransactionTests(OfflineCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = StateStore(self.root / "state")
        self.store.save_config({"terms_acknowledged_version": TERMS_VERSION, "plan_id": PLAN})
        self.server = ContractServer()
        self.record = dict(copy.deepcopy(self.server.records[TX]), id=POSTED)
        self.server.records = {POSTED: self.record}
        self.client = YnabClient("SYNTHETIC-AUDIT-ONLY", transport=self.server.transport)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("budget_tools.utils._client", return_value=self.client), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args), store=self.store)
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def ok(self, *args):
        code, value, error = self.run_cli(*args)
        self.assertEqual((code, error), (0, ""))
        return value

    def draft(self):
        value = raw_proposal()
        value["transaction_id"] = POSTED
        path = self.root / "proposal.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return self.ok("transactions", "draft-receipt-split", POSTED, "--proposal", str(path))

    def test_full_id_reaches_get_unchanged(self):
        result = self.ok("transactions", "get", POSTED)
        self.assertEqual(result["id"], POSTED)
        self.assertEqual(result["date"], "2001-02-02")
        self.assertEqual(self.server.calls, [("GET", BASE + "/transactions/" + POSTED, {}, None)])

    def test_ordinary_id_still_works(self):
        self.server.records = {TX: dict(self.record, id=TX)}
        self.assertEqual(self.ok("transactions", "get", TX)["id"], TX)

    def test_get_and_update_preserve_path_without_simulator_routing(self):
        calls = []
        def capture(method, url, headers, body, timeout):
            calls.append((method, url))
            return 200, json.dumps({"data": {"transaction": self.record}}).encode()
        client = YnabClient("SYNTHETIC", transport=capture)
        client.get_transaction(PLAN, POSTED)
        client.update_transaction(PLAN, POSTED, {"memo": "Synthetic"})
        url = "https://api.ynab.com" + BASE + "/transactions/" + POSTED
        self.assertEqual(calls, [("GET", url), ("PUT", url)])

    def test_unsafe_delimiters_never_reach_accepting_transport(self):
        calls = []
        def capture(*args):
            calls.append(args)
            return 200, b'{"data":{"transaction":{}}}'
        client = YnabClient("SYNTHETIC", transport=capture)
        for identifier in ("a/b", "a?query", "a#fragment", "a%2fb", "a\\b", "a\n", "é"):
            with self.subTest(identifier=identifier):
                with self.assertRaises(ValueError):
                    client.get_transaction(PLAN, identifier)
        self.assertEqual(calls, [])

    def test_match_preserves_suffixed_id_and_record_date(self):
        result = self.ok("transactions", "match", "--since", "2001-02-02", "--until", "2001-02-02",
                         "--account", "Example Account B", "--outflow", "28.80")
        self.assertEqual(result["match_status"], "unique")
        self.assertEqual(result["candidates"][0]["id"], POSTED)
        self.assertEqual(self.server.writes(), [])

    def test_missing_full_id_is_not_retried_with_a_shortened_id(self):
        calls = []
        def missing(method, url, headers, body, timeout):
            calls.append((method, url))
            return 404, b'{"error":{"name":"resource_not_found"}}'
        client = YnabClient("SYNTHETIC", transport=missing)
        with self.assertRaises(ApiError) as error:
            client.get_transaction(PLAN, POSTED)
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(calls, [("GET", "https://api.ynab.com" + BASE + "/transactions/" + POSTED)])

    def test_unsafe_resource_ids_rejected_before_transport(self):
        invalid = ["", None, 123, ".", "..", "a/b", "a\\b", "a?x=y", "a#fragment", "a%b",
                   "a%2fb", "a%252fb", "a b", "a\n", "a\r", "a\x00", "é", "a:443", "a@b", "a&b", "a=b"]
        for identifier in invalid:
            for operation in (
                lambda: self.client.get_transaction(PLAN, identifier),
                lambda: self.client.update_transaction(PLAN, identifier, {"memo": "Synthetic"}),
                lambda: self.client.get_accounts(identifier),
                lambda: self.client.get_transactions_by_account(PLAN, identifier, since_date="2001-02-01"),
            ):
                with self.subTest(identifier=identifier):
                    with self.assertRaises(ValueError):
                        operation()
        self.assertEqual(self.server.calls, [])

    def test_suffixed_id_receipt_apply_verifies_before_approval(self):
        draft = self.draft()
        self.assertEqual(self.store.load_draft(draft["id"])["transaction_id"], POSTED)
        self.assertEqual(self.server.writes(), [])
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        writes = self.server.writes()
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(row[1] == BASE + "/transactions/" + POSTED for row in writes))
        self.assertFalse(writes[0][3]["transaction"]["approved"])
        self.assertEqual(writes[1][3], {"transaction": {"approved": True}})

    def test_suffixed_id_apply_still_requires_confirmation(self):
        draft = self.draft()
        code, _, _ = self.run_cli("drafts", "apply", draft["id"], "--confirm", "wrong")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.server.writes(), [])

    def test_suffixed_id_changed_transaction_blocks_write(self):
        draft = self.draft()
        self.record["memo"] = "Changed by another synthetic client"
        code, _, _ = self.run_cli("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.server.writes(), [])

    def test_suffixed_id_uncertain_write_reconciles_without_replay(self):
        draft = self.draft()
        self.server.reply_invalid_after_commit = True
        code, _, _ = self.run_cli("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.server.writes()), 1)
        self.server.reply_invalid_after_commit = False
        reconciled = self.ok("drafts", "reconcile", draft["id"])
        self.assertEqual(reconciled["status"], "approval_pending")
        self.assertEqual(len(self.server.writes()), 1)
        self.assertEqual(self.ok("drafts", "resume", draft["id"], "--confirm", draft["id"])["stage"], "verified_approval")
        self.assertEqual(len(self.server.writes()), 2)
        self.assertEqual(self.server.writes()[1][3], {"transaction": {"approved": True}})

    def split_record(self):
        self.record.update(category_id="split-category", category_name="Split",
                           memo="Monthly bill — original parent context\nSecond line")
        self.record["subtransactions"] = [
            {"id": "line-a", "amount": -30001, "category_id": SHARED, "category_name": "Example Category D",
             "memo": "To be reimbursed — original line memo", "payee_id": "line-payee",
             "payee_name": "Example Relative", "deleted": False},
            {"id": "line-b", "amount": 1201, "category_id": FOOD, "category_name": "Example Category C",
             "memo": "", "deleted": False},
            {"id": "line-c", "amount": 0, "category_id": None, "category_name": None,
             "memo": None, "transfer_account_id": OTHER_ACCOUNT,
             "transfer_transaction_id": "transfer-entry", "deleted": False},
            {"id": "deleted-line", "amount": -9000, "memo": "Old line", "deleted": True},
        ]

    def list_records(self, *args):
        return self.ok("transactions", "list", "--since", "2001-02-01", "--until", "2001-02-03", *args)

    def test_default_list_contract_is_unchanged(self):
        self.split_record()
        self.assertEqual(self.list_records(), [{
            "id": POSTED, "date": "2001-02-02", "amount_milliunits": -28800,
            "amount_currency": "-28.8", "account_name": "Example Account B", "payee_name": "Example Shop",
            "category_name": "Split", "memo": self.record["memo"], "cleared": "cleared",
            "approved": False, "flag_name": None, "deleted": False, "subtransaction_count": 4,
        }])

    def test_bulk_splits_preserve_exact_line_and_parent_context(self):
        self.split_record()
        row, = self.list_records("--include-splits")
        self.assertEqual(row["id"], POSTED)
        self.assertEqual(row["account_id"], ACCOUNT)
        self.assertEqual(row["category_id"], "split-category")
        self.assertEqual(row["memo"], "Monthly bill — original parent context\nSecond line")
        self.assertEqual(row["amount_milliunits"], -28800)
        lines = row["subtransactions"]
        self.assertEqual([(t["id"], t["amount_milliunits"], t["amount_currency"], t["category_id"],
                           t["category_name"], t["memo"]) for t in lines], [
            ("line-a", -30001, "-30.001", SHARED, "Example Category D", "To be reimbursed — original line memo"),
            ("line-b", 1201, "1.201", FOOD, "Example Category C", ""),
            ("line-c", 0, "0", None, None, None),
        ])
        self.assertEqual((lines[0]["payee_id"], lines[0]["payee_name"]), ("line-payee", "Example Relative"))
        self.assertEqual((lines[2]["transfer_account_id"], lines[2]["transfer_transaction_id"]),
                         (OTHER_ACCOUNT, "transfer-entry"))
        self.assertEqual(sum(t["amount_milliunits"] for t in lines), row["amount_milliunits"])
        self.assertEqual(self.server.writes(), [])

    def test_bulk_uses_one_list_request_and_never_fetches_each_record(self):
        self.split_record()
        for day in ("03", "04", "05"):
            self.server.duplicate(POSTED, TX + "_2001-01-" + day)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(self.client, "get_transaction", side_effect=AssertionError("Per-record read")):
            rows = self.list_records("--include-splits")
        self.assertEqual(len(rows), 4)
        self.assertEqual(self.server.calls, [("GET", BASE + "/transactions",
                         {"since_date": ["2001-02-01"], "until_date": ["2001-02-03"]}, None)])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes()
                                 for p in self.root.rglob("*") if p.is_file()})

    def test_bulk_and_single_get_share_detail_contract(self):
        self.split_record()
        bulk, = self.list_records("--include-splits")
        self.assertEqual(bulk, self.ok("transactions", "get", POSTED))

    def test_bulk_unsplit_null_and_empty_parent_memos(self):
        for memo in (None, ""):
            with self.subTest(memo=memo):
                self.record["memo"] = memo
                row, = self.list_records("--include-splits")
                self.assertEqual(row["memo"], memo)
                self.assertEqual(row["subtransactions"], [])
                self.assertEqual(row["subtransaction_count"], 0)

    def test_bulk_limit_applies_to_parents_not_children(self):
        self.split_record()
        self.server.duplicate(POSTED, "ordinary-entry")
        row, = self.list_records("--include-splits", "--limit", "1")
        self.assertEqual(len(row["subtransactions"]), 3)
        self.assertEqual(len(self.list_records("--include-splits")), 2)

    def test_bulk_honors_account_date_type_and_amount_filters(self):
        self.split_record()
        self.server.duplicate(POSTED, "before", date="2001-01-31")
        self.server.duplicate(POSTED, "after", date="2001-02-04")
        self.server.duplicate(POSTED, "different-account", account_id=OTHER_ACCOUNT)
        self.server.duplicate(POSTED, "approved", approved=True)
        self.server.duplicate(POSTED, "different-amount", amount=-28801)
        self.server.duplicate(POSTED, "deleted", deleted=True)
        self.server.duplicate(POSTED, "refund", amount=28800)
        options = ("--include-splits", "--account", "Example Account B", "--type", "unapproved")
        self.assertEqual([r["id"] for r in self.list_records(*options, "--outflow", "28.80")], [POSTED])
        self.assertEqual(self.server.calls[-1], ("GET", BASE + f"/accounts/{ACCOUNT}/transactions",
                         {"since_date": ["2001-02-01"], "until_date": ["2001-02-03"],
                          "type": ["unapproved"]}, None))
        self.assertEqual([r["id"] for r in self.list_records(*options, "--inflow", "28.80")], ["refund"])

    def test_match_limit_still_counts_all_suffixed_candidates(self):
        self.server.duplicate(POSTED, TX + "_2001-01-26")
        result = self.ok("transactions", "match", "--since", "2001-02-02", "--until", "2001-02-02",
                         "--account", "Example Account B", "--outflow", "28.80", "--limit", "1")
        self.assertEqual((result["match_status"], result["candidate_count"], result["truncated"]),
                         ("ambiguous", 2, True))
        self.assertEqual(len(result["candidates"]), 1)
        self.assertNotIn("subtransactions", result["candidates"][0])

    def test_bulk_empty_results_and_invalid_limits(self):
        self.server.records = {}
        self.assertEqual(self.list_records("--include-splits"), [])
        for limit in ("0", "-1"):
            code, _, error = self.run_cli("transactions", "list", "--since", "2001-02-01",
                                          "--include-splits", "--limit", limit)
            self.assertNotEqual(code, 0)
            self.assertIn("--limit must be at least 1", error)
        self.assertEqual(self.server.writes(), [])
