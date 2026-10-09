# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Customer workflow through the real client, with an offline HTTP contract simulator.

Expected allocations are hand-calculated from the synthetic receipt below, not
copied from production payloads. This tests neither OCR nor a live YNAB server.
"""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from budget_tools.api import YnabClient
from budget_tools.cli import main
from budget_tools.state import StateStore, TERMS_VERSION
from validation.test_post_implementation_safety import OfflineCase

PLAN = "10000000-0000-0000-0000-000000000001"
ACCOUNT = "20000000-0000-0000-0000-000000000001"
OTHER_ACCOUNT = "20000000-0000-0000-0000-000000000002"
PAYEE = "30000000-0000-0000-0000-000000000001"
FOOD = "40000000-0000-0000-0000-000000000001"
SUPPLIES = "40000000-0000-0000-0000-000000000002"
FUEL = "40000000-0000-0000-0000-000000000003"
SHARED = "40000000-0000-0000-0000-000000000004"
TX = "50000000-0000-0000-0000-000000000001"
BASE = f"/v1/plans/{PLAN}"
EXPECTED_LINES = [
    {"amount": -18000, "category_id": FOOD, "memo": "Bread, fruit, less food coupon"},
    {"amount": -10800, "category_id": SUPPLIES, "memo": "Soap plus sales tax"},
]


def raw_proposal():
    # Receipt: bread 12 + fruit 8 - food coupon 2 + soap 10 + soap tax .80 = 28.80.
    # Broad instruction: edible goods to Example Category C; soap and its tax to Example Category E.
    return {
        "version": 1, "plan_id": PLAN, "transaction_id": TX,
        "expected": {"account_id": ACCOUNT, "date": "2001-02-02", "amount_milliunits": -28800},
        "receipt": {
            "reference": "synthetic-audit-receipt", "total": "28.80",
            "match_evidence": "Exact card account, merchant, date and amount; unique candidate",
            "components": [{"label": "Bread", "amount": "12.00"},
                           {"label": "Fruit", "amount": "8.00"},
                           {"label": "Example Category C coupon", "amount": "-2.00"},
                           {"label": "Soap", "amount": "10.00"},
                           {"label": "Soap sales tax", "amount": "0.80"}],
            "tenders": [{"label": "Card payment", "amount": "28.80"}], "unresolved": [],
        },
        "allocations": [
            {"category": "Example Group A: Example Category C", "amount": "18.00",
             "memo": "Bread, fruit, less food coupon", "evidence": "12 + 8 - 2 on receipt"},
            {"category": "Example Group A: Example Category E", "amount": "10.80",
             "memo": "Soap plus sales tax", "evidence": "10 + 0.80 receipt-marked soap tax"}],
        "approve_after_verification": True,
    }


class ContractServer:
    """Strict routes/wrappers, real filtering, independent snapshots, generated IDs.

    Existing split rewriting is a test tripwire, not a simulated undocumented
    server error. Only the contract surface used by these scenarios is modeled.
    """
    def __init__(self):
        self.records = {TX: {
            "id": TX, "account_id": ACCOUNT, "account_name": "Example Account B", "date": "2001-02-02",
            "payee_id": PAYEE, "payee_name": "Example Shop", "amount": -28800,
            "category_id": None, "memo": "Keep invoice note", "cleared": "cleared",
            "approved": False, "deleted": False, "subtransactions": [],
            "flag_color": None, "transfer_account_id": None, "transfer_transaction_id": None,
            "matched_transaction_id": None, "import_id": "synthetic-import-reference",
        }}
        self.calls = []
        self.alter_split_memo = False
        self.alter_parent_memo = False
        self.split_parent_category_id = None
        self.reply_invalid_after_commit = False
        self.duplicate(TX, "outside-before", date="2001-01-31")
        self.duplicate(TX, "outside-after", date="2001-02-04")
        self.duplicate(TX, "different-account", account_id=OTHER_ACCOUNT)
        self.duplicate(TX, "one-cent-off", amount=-28810)
        self.duplicate(TX, "refund", amount=28800)
        self.duplicate(TX, "deleted", deleted=True)
        self.duplicate(TX, "pending", _pending=True)

    def duplicate(self, source, identifier, **changes):
        self.records[identifier] = dict(copy.deepcopy(self.records[source]), id=identifier, **changes)

    @staticmethod
    def response(data, status=200):
        return status, json.dumps({"data": data}).encode()

    def transport(self, method, url, headers, body, timeout):
        parsed = urlsplit(url)
        assert (parsed.scheme, parsed.netloc) == ("https", "api.ynab.com"), "Wrong API destination"
        assert headers["Authorization"] == "Bearer SYNTHETIC-AUDIT-ONLY"
        payload = json.loads(body) if body else None
        path, query = parsed.path, parse_qs(parsed.query)
        self.calls.append((method, path, copy.deepcopy(query), copy.deepcopy(payload)))
        assert path.startswith(BASE + "/"), "Wrong plan or path prefix"
        if path == BASE + "/accounts" and method == "GET":
            return self.response({"accounts": [
                {"id": ACCOUNT, "name": "Example Account B", "closed": False, "deleted": False, "on_budget": True},
                {"id": OTHER_ACCOUNT, "name": "Example Account C", "closed": False, "deleted": False}]})
        if path == BASE + "/settings" and method == "GET":
            return self.response({"settings": {"currency_format": {"decimal_digits": 2}}})
        if path == BASE + "/categories" and method == "GET":
            return self.response({"category_groups": [{"name": "Example Group A", "hidden": False,
                "deleted": False, "internal": False, "categories": [
                    {"id": FOOD, "name": "Example Category C", "hidden": False, "deleted": False},
                    {"id": SUPPLIES, "name": "Example Category E", "hidden": False, "deleted": False},
                    {"id": FUEL, "name": "Example Category F", "hidden": False, "deleted": False},
                    {"id": SHARED, "name": "Example Category D", "hidden": False, "deleted": False}]}]})
        if path == BASE + "/payees" and method == "GET":
            return self.response({"payees": [{"id": PAYEE, "name": "Example Shop", "deleted": False}]})
        if method == "GET" and path in (BASE + "/transactions", BASE + f"/accounts/{ACCOUNT}/transactions"):
            rows = [copy.deepcopy(t) for t in self.records.values() if not t.get("_pending") and not t["deleted"]]
            if "/accounts/" in path:
                rows = [t for t in rows if t["account_id"] == ACCOUNT]
            if "since_date" in query:
                rows = [t for t in rows if t["date"] >= query["since_date"][0]]
            if "until_date" in query:
                rows = [t for t in rows if t["date"] <= query["until_date"][0]]
            if query.get("type") == ["unapproved"]:
                rows = [t for t in rows if not t["approved"]]
            return self.response({"transactions": rows})
        if path.startswith(BASE + "/transactions/"):
            transaction_id = path.rsplit("/", 1)[1]
            assert transaction_id in self.records, "Unknown transaction ID"
            current = self.records[transaction_id]
            if method == "GET":
                return self.response({"transaction": copy.deepcopy(current)})
            assert method == "PUT", "Wrong update HTTP method"
            assert isinstance(payload, dict) and set(payload) == {"transaction"}, "Wrong update wrapper"
            changes = payload["transaction"]
            if "subtransactions" in changes:
                assert not current["subtransactions"], "Attempted unsupported rewrite of existing split"
                assert sum(line["amount"] for line in changes["subtransactions"]) == current["amount"]
            current.update(copy.deepcopy(changes))
            if "subtransactions" in changes:
                self.generate_lines(current)
                if self.alter_split_memo:
                    current["subtransactions"][0]["memo"] = "Wrong persisted line memo"
                if self.alter_parent_memo:
                    current["memo"] = "Wrong persisted parent memo"
            if self.reply_invalid_after_commit:
                return 200, b"not-json"
            return self.response({"transaction": copy.deepcopy(current)})
        if path == BASE + "/transactions" and method == "POST":
            assert isinstance(payload, dict) and set(payload) == {"transaction"}, "Wrong creation wrapper"
            changes = payload["transaction"]
            assert {"account_id", "date", "amount"}.issubset(changes)
            assert sum(line["amount"] for line in changes["subtransactions"]) == changes["amount"]
            created = dict(copy.deepcopy(changes), id="new-synthetic-entry", deleted=False)
            self.generate_lines(created)
            self.records[created["id"]] = created
            return self.response({"transaction": created, "transaction_ids": [created["id"]]}, 201)
        raise AssertionError(f"Unexpected contract request: {method} {path}")

    def generate_lines(self, transaction):
        if self.split_parent_category_id:
            transaction.update(category_id=self.split_parent_category_id, category_name="Split")
        for index, line in enumerate(transaction["subtransactions"]):
            line.update(id=f"generated-{index}", deleted=False, payee_id=None)
        transaction["subtransactions"].reverse()

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


class ReceiptContractWorkflow(OfflineCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = StateStore(self.root / "state")
        self.store.save_config({"terms_acknowledged_version": TERMS_VERSION, "plan_id": PLAN})
        self.server = ContractServer()
        self.client = YnabClient("SYNTHETIC-AUDIT-ONLY", transport=self.server.transport)
        self.value = raw_proposal()
        self.path = self.root / "proposal.json"

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("budget_tools.cli._client", return_value=self.client), redirect_stdout(out), redirect_stderr(err):
            code = main(list(args), store=self.store)
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def ok(self, *args):
        code, result, error = self.run_cli(*args)
        self.assertEqual((code, error), (0, ""))
        return result

    def save_proposal(self):
        self.path.write_text(json.dumps(self.value))
        self.path.chmod(0o600)

    def draft(self):
        self.save_proposal()
        return self.ok("transactions", "draft-receipt-split", TX, "--proposal", str(self.path))

    def match(self, since="2001-02-01", until="2001-02-03", amount="28.80", direction="--outflow", limit="10"):
        return self.ok("transactions", "match", "--account", "Example Account B", "--since", since,
                       "--until", until, direction, amount, "--limit", limit)

    @staticmethod
    def lines(transaction):
        return sorted([{key: line[key] for key in ("amount", "category_id", "memo")}
                       for line in transaction["subtransactions"]], key=lambda line: line["category_id"])

    def test_receipt_match_preview_persist_exact_categories_memos_then_approve(self):
        match = self.match()
        self.assertEqual(match["match_status"], "unique")
        self.assertEqual([row["id"] for row in match["candidates"]], [TX])
        draft = self.draft()
        preview = draft["receipt_review"]
        self.assertEqual(preview["parent_memo"], "Keep invoice note")
        self.assertEqual([(a["category_id"], a["amount_milliunits"], a["memo"]) for a in preview["allocations"]],
                         [(FOOD, -18000, "Bread, fruit, less food coupon"), (SUPPLIES, -10800, "Soap plus sales tax")])
        self.assertEqual(self.server.writes(), [], "Matching and preview must not mutate")
        self.server.calls.clear()
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual([call[0] for call in self.server.calls], ["GET", "PUT", "GET", "GET", "PUT", "GET"])
        first, second = self.server.writes()
        self.assertEqual(first[3], {"transaction": {"category_id": None, "approved": False,
                         "memo": "Keep invoice note", "subtransactions": EXPECTED_LINES}})
        self.assertEqual(second[3], {"transaction": {"approved": True}})
        saved = self.server.records[TX]
        self.assertEqual(self.lines(saved), EXPECTED_LINES)
        self.assertEqual((saved["account_id"], saved["date"], saved["amount"], saved["cleared"]),
                         (ACCOUNT, "2001-02-02", -28800, "cleared"))
        self.assertEqual(saved["import_id"], "synthetic-import-reference")
        self.assertTrue(saved["approved"])

    def test_matching_honors_account_dates_exact_amount_and_sign(self):
        match = self.match()
        self.assertEqual([item["id"] for item in match["candidates"]], [TX])
        query_call = [c for c in self.server.calls if c[1].endswith("/transactions")][-1]
        self.assertEqual(query_call[1], BASE + f"/accounts/{ACCOUNT}/transactions")
        self.assertEqual(query_call[2], {"since_date": ["2001-02-01"], "until_date": ["2001-02-03"]})
        refund = self.match(direction="--inflow")
        self.assertEqual([item["id"] for item in refund["candidates"]], ["refund"])
        self.assertEqual(self.match(amount="28.79")["match_status"], "not_found")
        self.assertEqual(self.server.writes(), [])

    def check_memo_round_trip(self, memo, category="Example Category C", category_id=FOOD, components=None):
        """Exercise authored text through preview, HTTP serialization, and readback.

        This does not evaluate whether a model generated the right memo from a receipt.
        """
        self.value["parent_memo"] = memo
        self.value["receipt"]["components"] = components or [{"label": "Purchase", "amount": "28.80"}]
        self.value["allocations"] = [{"category": "Example Group A: " + category, "amount": "28.80",
                                      "memo": memo, "evidence": "Synthetic receipt, one category"}]
        draft = self.draft()
        self.assertEqual(draft["receipt_review"]["parent_memo"], memo)
        self.assertEqual(draft["receipt_review"]["allocations"][0]["memo"], memo)
        self.assertEqual(self.server.writes(), [], "Draft review must remain read-only")
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        payload = self.server.writes()[0][3]["transaction"]
        expected = [{"amount": -28800, "category_id": category_id, "memo": memo}]
        self.assertEqual((payload["memo"], payload["subtransactions"]), (memo, expected))
        self.assertEqual((self.server.records[TX]["memo"], self.lines(self.server.records[TX])), (memo, expected))
        self.assertEqual(self.server.records[TX]["amount"], -28800)

    def test_whole_dollar_tip_memo_preserved(self):
        self.check_memo_round_trip("Example Venue; subtotal 24.80; tip 4", components=[
            {"label": "Example Category C including tax", "amount": "24.80"}, {"label": "Tip", "amount": "4.00"}])

    def test_fractional_tip_and_venue_memo_preserved(self):
        self.check_memo_round_trip("Example Venue; subtotal 24.00; tip 4.80", components=[
            {"label": "Example Category C including tax", "amount": "24.00"}, {"label": "Tip", "amount": "4.80"}])

    def test_fuel_unit_price_and_quantity_precision_preserved(self):
        self.check_memo_round_trip("Example fuel: 6.250 gal at 4.6080 USD/gal", "Example Category F", FUEL)

    def test_fuel_trip_context_preserved(self):
        self.check_memo_round_trip(
            "Example fuel: 6.250 gal at 4.6080 USD/gal - Example purpose", "Example Category F", FUEL)

    def test_explicit_alternative_memo_style_preserved(self):
        self.check_memo_round_trip("Fictional unit price 4.6080 USD/gal; quantity 6.250 gal; receipt A", "Example Category F", FUEL)

    def test_split_tip_memos_keep_only_allocated_amounts(self):
        self.value["parent_memo"] = "Example Venue; subtotal 24.00; tip 4.80"
        self.value["receipt"]["components"] = [
            {"label": "Example Category C including tax", "amount": "24.00"}, {"label": "Tip", "amount": "4.80"}]
        self.value["allocations"] = [
            {"category": "Example Group A: Example Category C", "amount": "14.40",
             "memo": "Example Venue; allocation A: 12.00; tip share 2.40",
             "evidence": "User explicitly requested equal split including tip"},
            {"category": "Example Group A: Example Category D", "amount": "14.40",
             "memo": "Example Venue; allocation B: 12.00; tip share 2.40",
             "evidence": "User explicitly requested equal split including tip"}]
        draft = self.draft()
        self.assertEqual(self.server.writes(), [])
        expected = [
            {"amount": -14400, "category_id": FOOD,
             "memo": "Example Venue; allocation A: 12.00; tip share 2.40"},
            {"amount": -14400, "category_id": SHARED,
             "memo": "Example Venue; allocation B: 12.00; tip share 2.40"}]
        self.assertEqual([a["memo"] for a in draft["receipt_review"]["allocations"]],
                         [line["memo"] for line in expected])
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(self.lines(self.server.records[TX]), expected)
        self.assertEqual(self.server.records[TX]["memo"], self.value["parent_memo"])

    def test_memo_length_boundary_preserves_full_text(self):
        prefix = "Example fuel: 6.250 gal at 4.6080 USD/gal - "
        self.check_memo_round_trip(prefix + "x" * (500 - len(prefix)), "Example Category F", FUEL)

    def test_overlong_split_memo_rejected_without_draft_or_write(self):
        self.value["allocations"][0]["memo"] = "x" * 501
        self.save_proposal()
        code, _, error = self.run_cli("transactions", "draft-receipt-split", TX, "--proposal", str(self.path))
        self.assertEqual(code, 1)
        self.assertIn("500", error)
        self.assertEqual(self.store.list_drafts(), [])
        self.assertEqual(self.server.writes(), [])

    def test_changed_parent_memo_blocks_approval(self):
        self.value["parent_memo"] = "Example Venue; subtotal 24.80; tip 4"
        self.value["receipt"]["components"] = [
            {"label": "Example Category C including tax", "amount": "24.80"}, {"label": "Tip", "amount": "4.00"}]
        draft = self.draft()
        self.server.alter_parent_memo = True
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "categorization_verification_failed")
        self.assertFalse(self.server.records[TX]["approved"])
        self.assertEqual(len(self.server.writes()), 1)

    def test_automation_marker_persists_only_on_parent_memo(self):
        memo = "Receipt reviewed (Example Marker)"
        self.value["parent_memo"] = memo
        draft = self.draft()
        self.assertEqual(draft["receipt_review"]["parent_memo"], memo)
        self.assertEqual([a["memo"] for a in draft["receipt_review"]["allocations"]],
                         [line["memo"] for line in EXPECTED_LINES])
        self.assertEqual(self.server.writes(), [])
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        payload = self.server.writes()[0][3]["transaction"]
        self.assertEqual(payload["memo"], memo)
        self.assertEqual(payload["subtransactions"], EXPECTED_LINES)
        self.assertEqual(self.server.records[TX]["memo"], memo)
        self.assertEqual(self.lines(self.server.records[TX]), EXPECTED_LINES)

    def test_server_split_parent_category_allows_verified_approval(self):
        self.server.split_parent_category_id = "synthetic-internal-split"
        draft = self.draft()
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(self.lines(self.server.records[TX]), EXPECTED_LINES)
        self.assertEqual(self.server.writes()[1][3], {"transaction": {"approved": True}})

    def test_internal_split_parent_does_not_hide_wrong_line_memo(self):
        self.server.split_parent_category_id = "synthetic-internal-split"
        self.server.alter_split_memo = True
        draft = self.draft()
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "categorization_verification_failed")
        self.assertFalse(self.server.records[TX]["approved"])
        self.assertEqual(len(self.server.writes()), 1)

    def test_non_split_parent_category_still_rejected(self):
        self.save_proposal()
        self.server.records[TX].update(category_id=FOOD, category_name="Example Category C", subtransactions=copy.deepcopy(EXPECTED_LINES))
        result = self.ok("transactions", "assess-receipt", TX, "--proposal", str(self.path))
        self.assertEqual(result["route"], "manual_split_correction_required")
        self.assertIn("category_id", result["verification"]["mismatches"])
        self.assertEqual(self.server.writes(), [])

    def test_internal_split_parent_recovery_only_sends_remaining_approval(self):
        self.server.split_parent_category_id = "synthetic-internal-split"
        self.server.alter_parent_memo = True
        draft = self.draft()
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "categorization_verification_failed")
        self.server.records[TX]["memo"] = "Keep invoice note"
        reconciled = self.ok("drafts", "reconcile", draft["id"])
        self.assertEqual(reconciled["status"], "approval_pending")
        result = self.ok("drafts", "resume", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(len(self.server.writes()), 2)
        self.assertEqual(self.server.writes()[1][3], {"transaction": {"approved": True}})
        self.assertEqual(self.lines(self.server.records[TX]), EXPECTED_LINES)

    def test_parent_marker_counts_toward_memo_length_boundary(self):
        suffix = " (Example Marker)"
        memo = "x" * (500 - len(suffix)) + suffix
        self.value["parent_memo"] = memo
        draft = self.draft()
        self.assertEqual(draft["receipt_review"]["parent_memo"], memo)
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(self.server.records[TX]["memo"], memo)
        self.assertEqual(self.lines(self.server.records[TX]), EXPECTED_LINES)

    def test_parent_marker_overflow_rejected_without_draft_or_write(self):
        suffix = " (Example Marker)"
        self.value["parent_memo"] = "x" * (501 - len(suffix)) + suffix
        self.save_proposal()
        code, _, error = self.run_cli("transactions", "draft-receipt-split", TX, "--proposal", str(self.path))
        self.assertEqual(code, 1)
        self.assertIn("500", error)
        self.assertEqual(self.store.list_drafts(), [])
        self.assertEqual(self.server.writes(), [])

    def test_date_endpoints_are_inclusive_and_limit_does_not_hide_ambiguity(self):
        self.server.records[TX]["date"] = "2001-02-01"
        self.server.duplicate(TX, "last-day", date="2001-02-03")
        match = self.match(limit="1")
        self.assertEqual((match["match_status"], match["candidate_count"], match["truncated"]), ("ambiguous", 2, True))
        self.assertEqual(self.server.writes(), [])

    def test_already_approved_does_not_generate_an_unapproval_or_conversion(self):
        self.server.records[TX]["approved"] = True
        self.save_proposal()
        assessed = self.ok("transactions", "assess-receipt", TX, "--proposal", str(self.path))
        self.assertEqual(assessed["route"], "already_approved")
        self.assertEqual(self.run_cli("transactions", "draft-receipt-split", TX, "--proposal", str(self.path))[0], 1)
        self.assertEqual(self.server.writes(), [])

    def test_existing_mismatched_split_routes_to_manual_without_rewrite(self):
        self.server.records[TX]["subtransactions"] = [
            {"id": "old-line", "amount": -28800, "category_id": FOOD, "memo": "All food", "payee_id": None}]
        self.save_proposal()
        assessment = self.ok("transactions", "assess-receipt", TX, "--proposal", str(self.path))
        self.assertEqual(assessment["route"], "manual_split_correction_required")
        self.assertIn("subtransactions", assessment["verification"]["mismatches"])
        self.assertEqual(self.run_cli("transactions", "draft-receipt-split", TX, "--proposal", str(self.path))[0], 1)
        self.assertEqual(self.server.writes(), [])

    def test_wrong_saved_line_memo_blocks_approval_even_when_totals_balance(self):
        draft = self.draft()
        self.server.alter_split_memo = True
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "categorization_verification_failed")
        self.assertFalse(self.server.records[TX]["approved"])
        self.assertEqual(len(self.server.writes()), 1)

    def test_invalid_json_after_server_commit_is_uncertain_and_never_replayed(self):
        draft = self.draft()
        self.server.reply_invalid_after_commit = True
        self.assertEqual(self.run_cli("drafts", "apply", draft["id"], "--confirm", draft["id"])[0], 1)
        self.assertEqual(self.store.load_draft(draft["id"])["status"], "uncertain")
        self.assertEqual(self.run_cli("drafts", "apply", draft["id"], "--confirm", draft["id"])[0], 1)
        self.server.reply_invalid_after_commit = False
        self.assertEqual(self.ok("drafts", "reconcile", draft["id"])["status"], "approval_pending")
        self.ok("drafts", "resume", draft["id"], "--confirm", draft["id"])
        self.assertEqual(len(self.server.writes()), 2)
        self.assertEqual(self.server.writes()[1][3], {"transaction": {"approved": True}})

    def test_explicit_new_receipt_creation_uses_post_wrapper_and_exact_lines(self):
        before = copy.deepcopy(self.server.records)
        self.value["transaction_id"] = None
        self.value["parent_memo"] = "New receipt"
        self.save_proposal()
        draft = self.ok("transactions", "draft-receipt-create", "--account", "Example Account B", "--payee", "Example Shop",
                        "--date", "2001-02-02", "--outflow", "28.80", "--proposal", str(self.path))
        self.assertEqual(self.server.writes(), [])
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        first, second = self.server.writes()
        self.assertEqual((first[0], first[1]), ("POST", BASE + "/transactions"))
        self.assertEqual(first[3]["transaction"]["subtransactions"], EXPECTED_LINES)
        self.assertEqual(first[3]["transaction"]["memo"], "New receipt")
        self.assertEqual(second[3], {"transaction": {"approved": True}})
        self.assertEqual(self.lines(self.server.records["new-synthetic-entry"]), EXPECTED_LINES)
        self.assertEqual(self.server.records["new-synthetic-entry"]["memo"], "New receipt")
        self.assertEqual(len(self.server.records), len(before) + 1)
        self.assertEqual({key: self.server.records[key] for key in before}, before,
                         "Creating a new entry must not overwrite any existing transaction")

    def test_pure_refund_keeps_positive_split_amounts_and_exact_memos(self):
        self.server.records[TX]["amount"] = 28800
        self.value["expected"]["amount_milliunits"] = 28800
        self.value["receipt"]["match_evidence"] = "Exact synthetic full-refund account, date and amount"
        draft = self.draft()
        result = self.ok("drafts", "apply", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(self.lines(self.server.records[TX]), [
            {"amount": 18000, "category_id": FOOD, "memo": "Bread, fruit, less food coupon"},
            {"amount": 10800, "category_id": SUPPLIES, "memo": "Soap plus sales tax"}])
        self.assertEqual(self.server.records[TX]["amount"], 28800)

    def test_inconsistent_or_incomplete_receipt_facts_cannot_create_a_draft(self):
        cases = [
            (("plan_id",), "different-plan", "plan_id"),
            (("transaction_id",), "different-transaction", "transaction_id"),
            (("expected", "account_id"), OTHER_ACCOUNT, "account_id"),
            (("expected", "date"), "2001-02-03", "date"),
            (("expected", "amount_milliunits"), -28810, "amount_milliunits"),
            (("receipt", "match_evidence"), "", "match_evidence"),
            (("receipt", "reference"), "", "reference"),
            (("receipt", "unresolved"), ["Unclear item price"], "unresolved"),
            (("receipt", "total"), "28.79", "receipt total"),
            (("parent_memo",), "x" * 501, "500"),
        ]
        for keys, invalid, error_fragment in cases:
            with self.subTest(field=keys):
                self.value = raw_proposal()
                target = self.value
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = invalid
                self.save_proposal()
                code, _, error = self.run_cli("transactions", "draft-receipt-split", TX,
                                              "--proposal", str(self.path))
                self.assertEqual(code, 1)
                self.assertIn(error_fragment, error)
                self.assertEqual(self.server.writes(), [])
                self.assertEqual(self.store.list_drafts(), [])


if __name__ == "__main__":
    unittest.main()
