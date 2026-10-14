# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Fictional online-order HTTP contracts; no real receipts or account access."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout, redirect_stderr
from urllib.parse import urlparse, parse_qs

from budget_tools import purchase_memos as memos, purchases, purchase_workflow as workflow
from budget_tools import transaction_workflow
from budget_tools.api import ApiError, YnabClient
from budget_tools.cli import main
from budget_tools.errors import CliError
from budget_tools.state import StateError, StateStore, TERMS_VERSION


class OrderServer:
    """Independent endpoint simulation, including immutable existing split rows."""

    def __init__(self):
        self.plan = "plan-a"
        self.currency = {"iso_code": "USD", "decimal_digits": 2}
        self.accounts = [
            {
                "id": "account-a",
                "name": "Example Account A",
                "on_budget": True,
                "closed": False,
                "deleted": False,
            }
        ]
        self.payees = [{"id": "payee-a", "name": "Example Merchant A", "deleted": False}]
        self.categories = [
            {
                "id": "group-a",
                "name": "Example Group A",
                "categories": [
                    {
                        "id": "category-" + s,
                        "name": "Example Category " + s.upper(),
                        "deleted": False,
                        "hidden": False,
                    }
                    for s in ("a", "b", "c")
                ],
            }
        ]
        self.records = {}
        self.calls = []
        self.next_id = 1
        self.before_write = None
        self.after_write = None
        self.before_read = None
        self.reject = None
        self.lost = None
        self.empty_create_response = False

    def raw(self, payload, txid):
        result = {
            "id": txid,
            "account_id": "account-a",
            "date": "2001-01-30",
            "amount": -1000,
            "payee_id": "payee-a",
            "category_id": "category-a",
            "memo": None,
            "cleared": "uncleared",
            "approved": False,
            "flag_color": None,
            "deleted": False,
            "import_id": None,
            "matched_transaction_id": None,
            "transfer_account_id": None,
            "transfer_transaction_id": None,
            "subtransactions": [],
            **copy.deepcopy(payload),
        }
        result["account_name"] = "Example Account A"
        result["payee_name"] = "Example Merchant A"
        if result.get("subtransactions"):
            self.set_rows(result, result["subtransactions"])
        return result

    def set_rows(self, target, rows):
        assert sum(r["amount"] for r in rows) == target["amount"], "Split must balance"
        target.update(category_id="split-category", category_name="Split")
        target["subtransactions"] = [
            dict(
                r,
                id="line-" + str(i) + "-" + target["id"],
                deleted=False,
                payee_id=r.get("payee_id"),
                transfer_account_id=None,
                transfer_transaction_id=None,
            )
            for i, r in enumerate(copy.deepcopy(rows))
        ]

    def actual(self, amount, *, txid="posted-a", **fields):
        self.records[txid] = self.raw(
            {
                "amount": -amount,
                "date": "2001-02-03",
                "category_id": "category-c",
                "cleared": "cleared",
                "approved": True,
                "import_id": "synthetic-bank-" + txid,
                **fields,
            },
            txid,
        )
        return txid

    def __call__(self, method, url, headers, body, timeout):
        parsed = urlparse(url)
        path = parsed.path.removeprefix("/v1")
        params = parse_qs(parsed.query)
        self.calls.append((method, path, json.loads(body) if body else None))
        assert path.startswith("/plans/" + self.plan), "Explicit exact plan required"
        if method != "GET":
            if self.before_write:
                self.before_write(method, path)
            if self.reject:
                status = self.reject
                self.reject = None
                return status, json.dumps({"error": {"name": "synthetic-rejection"}}).encode()
        if method == "GET":
            if self.before_read:
                self.before_read(path)
            if path.endswith("/settings"):
                result = {"settings": {"currency_format": self.currency}}
            elif path.endswith("/accounts"):
                result = {"accounts": self.accounts}
            elif path.endswith("/payees"):
                result = {"payees": self.payees}
            elif path.endswith("/categories"):
                result = {"category_groups": self.categories}
            elif "/transactions/" in path:
                txid = path.rsplit("/", 1)[1]
                if txid not in self.records:
                    return 404, json.dumps({"error": {"name": "resource_not_found"}}).encode()
                result = {"transaction": self.records[txid]}
            elif path.endswith("/transactions"):
                account = (
                    path.split("/accounts/")[1].split("/")[0] if "/accounts/" in path else None
                )
                since = params.get("since_date", ["0000"])[0]
                until = params.get("until_date", ["9999"])[0]
                result = {
                    "transactions": [
                        r
                        for r in self.records.values()
                        if not r["deleted"]
                        and (not account or r["account_id"] == account)
                        and since <= r["date"] <= until
                    ]
                }
            else:
                raise AssertionError("Unexpected GET " + path)
        elif method == "POST":
            assert body and set(json.loads(body)) == {"transaction"}
            txid = "created-" + str(self.next_id)
            self.next_id += 1
            payload = json.loads(body)["transaction"]
            assert "import_id" not in payload, "No fake import id for idempotency"
            self.records[txid] = self.raw(payload, txid)
            result = {"transaction": self.records[txid]}
        elif method == "PUT":
            txid = path.rsplit("/", 1)[1]
            payload = json.loads(body)["transaction"]
            target = self.records[txid]
            if target.get("subtransactions") and any(
                k in payload for k in ("subtransactions", "category_id", "date", "amount")
            ):
                raise AssertionError("Existing split rows/amount/date cannot be updated")
            target.update(copy.deepcopy(payload))
            if "subtransactions" in payload:
                self.set_rows(target, payload["subtransactions"])
            result = {"transaction": target}
        elif method == "DELETE":
            txid = path.rsplit("/", 1)[1]
            self.records[txid]["deleted"] = True
            result = {"transaction": self.records[txid]}
        else:
            raise AssertionError("Unexpected method")
        if method != "GET":
            if self.after_write:
                self.after_write(method, path, result)
            if self.lost == method:
                self.lost = None
                raise socket.timeout()
            if method == "POST" and self.empty_create_response:
                self.empty_create_response = False
                return 201, b"{}"
        return (201 if method == "POST" else 200), json.dumps({"data": result}).encode()

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def order():
    return {
        "schema_version": 1,
        "plan_id": "plan-a",
        "account": "account-a",
        "payee": "payee-a",
        "merchant": "Amazon",
        "order_reference": "FICTIONAL-ORDER-A",
        "order_date": "2001-01-30",
        "currency": "USD",
        "order_total": "96.00",
        "card_total": "96.00",
        "payment_type": "single_card",
        "purchase_kind": "physical",
        "eligibility_evidence": "Fictional online order can charge separate shipments",
        "evidence": "Fictional screenshot with complete order/payment totals",
        "unresolved": [],
        "items": [
            {
                "id": code,
                "description": "Fictional item " + code,
                "quantity": 1,
                "amount": amount,
                "category": "category-" + category,
                "allocation_basis": "observed",
                "evidence": "Fictional invoice including allocated tax",
                "delivery": "2001-02-04" if code != "C" else "2001-02-06",
            }
            for code, amount, category in [
                ("A", "24.00", "a"),
                ("B", "36.00", "b"),
                ("C", "36.00", "c"),
            ]
        ],
    }


class PurchaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.store = StateStore(Path(self.temporary.name))
        self.store.save_config({"plan_id": "plan-a", "terms_acknowledged_version": TERMS_VERSION})
        self.server = OrderServer()
        self.client = YnabClient("synthetic-token", transport=self.server)

    def tearDown(self):
        self.temporary.cleanup()

    def draft(self, proposal=None):
        return purchases.draft_create(self.client, self.store, proposal or order())

    def apply(self, draft, **options):
        return workflow.execute(
            self.store,
            draft["id"],
            client_factory=lambda: self.client,
            confirm=draft["id"],
            **options,
        )

    def open(self, proposal=None):
        draft = self.draft(proposal)
        result = self.apply(draft)
        self.assertTrue(result["verification"]["verified"])
        return self.store.load_purchase(result["purchase_id"])

    def correction(self, record, charges):
        return purchases.draft_reconcile(
            self.client,
            self.store,
            record["id"],
            {
                "schema_version": 1,
                "purchase_id": record["id"],
                "charges": [
                    {
                        "transaction_id": tx,
                        "units": units,
                        "evidence": "Fictional Amazon payment/invoice identifies covered items",
                    }
                    for tx, units in charges
                ],
                "unresolved": [],
            },
        )

    def test_initial_split_real_categories_portable_context_and_private_ledger(self):
        draft = self.draft()
        self.assertEqual(self.server.writes(), [])
        payload = draft["payload"]["steps"][0]["payload"]
        self.assertEqual(payload["amount"], -96000)
        self.assertEqual(
            [r["category_id"] for r in payload["subtransactions"]],
            ["category-a", "category-b", "category-c"],
        )
        self.assertEqual(
            [r["amount"] for r in payload["subtransactions"]], [-24000, -36000, -36000]
        )
        self.assertIn("AI Created", payload["memo"])
        self.assertIn("May post as separate charges", payload["memo"])
        self.assertNotIn("v1", payload["memo"])
        self.assertTrue(all("PENDING" not in row["memo"] for row in payload["subtransactions"]))
        result = self.apply(draft)
        self.assertEqual([c[0] for c in self.server.writes()], ["POST"])
        self.assertEqual(result["purchase_status"], "awaiting_charges")
        self.assertEqual(
            self.store.purchase_path(result["purchase_id"]).stat().st_mode & 0o777, 0o600
        )
        self.assertEqual(self.store.purchases_dir.stat().st_mode & 0o777, 0o700)

    def test_partial_a_b_posts_create_verified_remainder_then_delete_old(self):
        record = self.open()
        old = record["pending"]["id"]
        actual = self.server.actual(60000)
        draft = self.correction(record, [(actual, ["A.1", "B.1"])])
        self.server.calls = []
        result = self.apply(draft)
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual([c[0] for c in self.server.writes()], ["PUT", "POST", "DELETE"])
        pending = self.server.records[result["pending_transaction_id"]]
        self.assertEqual(pending["amount"], -36000)
        self.assertEqual(pending["category_id"], "category-c")
        self.assertFalse(pending["subtransactions"])
        self.assertIn("C", pending["memo"])
        self.assertIn("Covered: 60", pending["memo"])
        self.assertIn("Original: 96", pending["memo"])
        self.assertTrue(self.server.records[old]["deleted"])
        self.assertFalse(self.server.records[actual]["deleted"])
        self.assertEqual(self.server.records[actual]["import_id"], "synthetic-bank-" + actual)
        self.assertTrue(self.server.records[actual]["approved"])
        self.assertEqual(
            [r["amount"] for r in self.server.records[actual]["subtransactions"]], [-24000, -36000]
        )

    def test_sequential_partial_charges_and_final_settlement(self):
        record = self.open()
        first = record["pending"]["id"]
        a = self.server.actual(24000)
        result = self.apply(self.correction(record, [(a, ["A.1"])]))
        pending = self.server.records[result["pending_transaction_id"]]
        self.assertEqual(pending["amount"], -72000)
        self.assertEqual(len(pending["subtransactions"]), 2)
        self.assertNotEqual(first, pending["id"])
        record = self.store.load_purchase(record["id"])
        b = self.server.actual(36000, txid="posted-b")
        result = self.apply(self.correction(record, [(b, ["B.1"])]))
        second = result["pending_transaction_id"]
        self.assertEqual(self.server.records[second]["amount"], -36000)
        record = self.store.load_purchase(record["id"])
        c = self.server.actual(36000, txid="posted-c")
        result = self.apply(self.correction(record, [(c, ["C.1"])]))
        self.assertEqual(result["purchase_status"], "settled")
        self.assertIsNone(result["pending_transaction_id"])
        self.assertEqual(len(result["actual_transaction_ids"]), 3)
        self.assertEqual(
            sum(-t["amount"] for t in self.server.records.values() if not t["deleted"]), 96000
        )

    def test_native_full_match_keeps_id_and_rows_parent_memo_only(self):
        record = self.open()
        txid = record["pending"]["id"]
        before = copy.deepcopy(self.server.records[txid]["subtransactions"])
        self.server.records[txid].update(
            cleared="cleared", import_id="synthetic-native-import", approved=True
        )
        draft = self.correction(record, [(txid, ["A.1", "B.1", "C.1"])])
        self.server.calls = []
        result = self.apply(draft)
        self.assertEqual(result["actual_transaction_ids"], [txid])
        self.assertIsNone(result["pending_transaction_id"])
        self.assertEqual(self.server.records[txid]["subtransactions"], before)
        self.assertEqual(len(self.server.writes()), 1)
        self.assertEqual(set(self.server.writes()[0][2]["transaction"]), {"memo"})
        self.assertNotIn("PENDING", self.server.records[txid]["memo"])

    def test_equal_prices_are_ambiguous_and_delivery_not_billing_proof(self):
        record = self.open()
        self.server.actual(36000)
        result = purchases.inspect(self.client, self.store, record["id"], until="2001-04-01")
        candidate = result["candidates"][0]
        self.assertTrue(candidate["ambiguous"])
        self.assertTrue(candidate["evidence_required"])
        self.assertEqual({tuple(a) for a in candidate["allocations"]}, {("B.1",), ("C.1",)})
        self.assertEqual(len(self.server.writes()), 1)

    def test_fifty_items_same_category_one_native_charge(self):
        proposal = order()
        proposal.update(order_total="50.00", card_total="50.00")
        proposal["items"] = [
            dict(
                proposal["items"][0],
                id="X" + str(i),
                description="Fictional product " + str(i),
                amount="1.00",
                category="category-a",
            )
            for i in range(50)
        ]
        record = self.open(proposal)
        txid = record["pending"]["id"]
        self.assertEqual(len(self.server.records[txid]["subtransactions"]), 50)
        self.server.records[txid].update(cleared="cleared", import_id="synthetic-full-order")
        result = self.apply(self.correction(record, [(txid, list(record["remaining"]))]))
        self.assertEqual(result["purchase_status"], "settled")
        self.assertEqual(len(self.server.records[txid]["subtransactions"]), 50)
        self.assertEqual([c[0] for c in self.server.writes()], ["POST", "PUT"])

    def test_quantities_components_and_no_invented_unit_rounding(self):
        proposal = order()
        proposal["items"][0].update(quantity=2, unit_amounts=["12.00", "12.00"])
        record = self.open(proposal)
        result = self.apply(self.correction(record, [(self.server.actual(12000), ["A.1"])]))
        self.assertIn("A.2", result["remaining_units"])
        bad = order()
        bad["items"][0].update(quantity=7)
        with self.assertRaisesRegex(CliError, "unit_amounts"):
            self.draft(bad)

    def test_wrong_plan_currency_tender_missing_tax_and_future_date_rejected(self):
        for field, value in [
            ("plan_id", "other"),
            ("currency", "EUR"),
            ("card_total", "95.00"),
            ("payment_type", "mixed"),
            ("purchase_kind", "subscription"),
            ("order_date", "2999-01-01"),
            ("unresolved", ["Tax is cropped"]),
        ]:
            with self.subTest(field=field), self.assertRaises((CliError, ValueError)):
                proposal = order()
                proposal[field] = value
                self.draft(proposal)
        self.assertEqual(self.server.writes(), [])

    def test_duplicate_order_drafts_and_repeated_screenshot_prevent_creation(self):
        self.draft()
        with self.assertRaisesRegex(CliError, "already tracked"):
            self.draft()
        self.assertEqual(self.server.writes(), [])

    def test_changed_or_protected_pending_never_deleted(self):
        record = self.open()
        actual = self.server.actual(24000)
        draft = self.correction(record, [(actual, ["A.1"])])
        old = self.server.records[record["pending"]["id"]]
        old["approved"] = True
        writes = len(self.server.writes())
        with self.assertRaisesRegex(CliError, "protected"):
            self.apply(draft)
        self.assertEqual(len(self.server.writes()), writes)
        self.assertFalse(old["deleted"])

    def test_same_sum_wrong_account_or_unposted_authorization_rejected(self):
        for fields in (
            {"account_id": "other-account"},
            {"import_id": None, "cleared": "uncleared"},
            {"payee_id": "other-payee"},
        ):
            with self.subTest(fields=fields):
                record = (
                    self.open()
                    if not self.store.list_purchases()
                    else self.store.list_purchases()[0]
                )
                actual = self.server.actual(24000, **fields)
                with self.assertRaises(CliError):
                    self.correction(record, [(actual, ["A.1"])])
        self.assertEqual([c[0] for c in self.server.writes()], ["POST"])

    def test_memo_overflow_never_truncates_evidence(self):
        proposal = order()
        proposal["items"][0]["description"] = "x" * 500
        with self.assertRaisesRegex(CliError, "nothing was truncated"):
            self.draft(proposal)
        self.assertEqual(self.server.writes(), [])

    def test_child_drafts_cannot_bypass_parent_guarded_execution(self):
        draft = self.draft()
        step = draft["payload"]["steps"][0]
        workflow.ensure_child(self.store, draft, step)
        with self.assertRaisesRegex(CliError, "cannot execute directly"):
            transaction_workflow.execute(
                self.store, step["id"], client_factory=lambda: self.client, confirm=step["id"]
            )
        self.assertEqual(self.server.writes(), [])

    def test_read_only_recovery_and_explicit_adoption_without_old_chat(self):
        record = self.open()
        old = record["pending"]["id"]
        # A second host shares YNAB but has no original local ledger/drafts.
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            fresh = StateStore(Path(directory))
            fresh.save_config(self.store.load_config())
            recovered = purchases.recover(self.client, fresh, old, "account-a", until="2001-04-01")
            self.assertEqual(set(recovered["remaining"]), {"A.1", "B.1", "C.1"})
            self.assertEqual(recovered["ownership"], "unverified")
            adopted = purchases.draft_adopt(
                self.client,
                fresh,
                old,
                "account-a",
                "Fictional order reviewed; this exact manual estimate is intended",
                until="2001-04-01",
            )
            with self.assertRaisesRegex(CliError, "confirm-adopt"):
                workflow.execute(
                    fresh, adopted["id"], client_factory=lambda: self.client, confirm=adopted["id"]
                )
            result = workflow.execute(
                fresh,
                adopted["id"],
                client_factory=lambda: self.client,
                confirm=adopted["id"],
                confirm_adopt=old,
            )
            self.assertEqual(result["stage"], "verified_adoption")
            self.assertEqual(len(self.server.writes()), 1)

    def test_recovery_after_partial_posting_reconstructs_covered_items(self):
        record = self.open()
        actual = self.server.actual(60000)
        result = self.apply(self.correction(record, [(actual, ["A.1", "B.1"])]))
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            fresh = StateStore(Path(directory))
            fresh.save_config(self.store.load_config())
            recovered = purchases.recover(
                self.client,
                fresh,
                result["pending_transaction_id"],
                "account-a",
                until="2001-04-01",
            )
            self.assertEqual(recovered["remaining"], ["C.1"])
            self.assertEqual(recovered["model"]["total"], 96000)
            self.assertEqual(recovered["actuals"][0]["units"], ["A.1", "B.1"])
            self.assertEqual(len(self.server.writes()), 4)

    def test_expiry_bad_confirmation_and_tampered_seal_block_writes(self):
        draft = self.draft()
        with self.assertRaisesRegex(CliError, "confirm"):
            workflow.execute(
                self.store, draft["id"], client_factory=lambda: self.client, confirm="wrong"
            )
        with patch("budget_tools.transaction_workflow.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=25)
            with self.assertRaisesRegex(CliError, "24 hours"):
                self.apply(draft)
        path = self.store.draft_path(draft["id"])
        value = json.loads(path.read_text())
        value["payload"]["steps"][0]["payload"]["amount"] = -1
        path.write_text(json.dumps(value))
        with self.assertRaises(StateError):
            self.apply(draft)
        self.assertEqual(self.server.writes(), [])

    def test_failure_after_remainder_creation_resumes_without_duplicate(self):
        record = self.open()
        actual = self.server.actual(60000)
        draft = self.correction(record, [(actual, ["A.1", "B.1"])])

        def stop(method, path):
            if method == "DELETE":
                raise socket.timeout()

        self.server.before_write = stop
        with self.assertRaises(ApiError):
            self.apply(draft)
        self.assertEqual(self.store.load_draft(draft["id"])["status"], "uncertain")
        self.server.before_write = None
        # Unknown DELETE did not commit; reconcile refuses to retry it.
        with self.assertRaisesRegex(CliError, "still present"):
            self.apply(draft, command="reconcile")
        self.assertEqual(sum(c[0] == "POST" for c in self.server.writes()), 2)

    def test_lost_committed_delete_reconciles_read_only(self):
        record = self.open()
        actual = self.server.actual(60000)
        draft = self.correction(record, [(actual, ["A.1", "B.1"])])
        self.server.lost = "DELETE"
        with self.assertRaises(ApiError):
            self.apply(draft)
        before = len(self.server.writes())
        result = self.apply(draft, command="reconcile")
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(len(self.server.writes()), before)

    def test_lost_creation_id_requires_independent_unique_recovery_no_replay(self):
        draft = self.draft()
        self.server.lost = "POST"
        with self.assertRaises(ApiError):
            self.apply(draft)
        with self.assertRaisesRegex(CliError, "known transaction id"):
            self.apply(draft, command="reconcile")
        result = purchases.recover_step(
            self.client,
            self.store,
            draft["id"],
            0,
            "created-1",
            "Fictional independently identified exact entry",
        )
        self.assertEqual(result["financial_writes"], 0)
        result = self.apply(draft, command="reconcile")
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(len(self.server.writes()), 1)

    def test_ledger_free_duplicate_generation_is_not_auto_retired(self):
        record = self.open()
        old = record["pending"]["id"]
        self.server.records["duplicate"] = copy.deepcopy(self.server.records[old])
        self.server.records["duplicate"]["id"] = "duplicate"
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            fresh = StateStore(Path(directory))
            fresh.save_config(self.store.load_config())
            with self.assertRaisesRegex(CliError, "Competing pending"):
                purchases.recover(self.client, fresh, old, "account-a", until="2001-04-01")
        self.assertFalse(self.server.records[old]["deleted"])

    def test_cli_preview_create_show_apply_and_recovery_help(self):
        path = Path(self.temporary.name) / "order.json"
        path.write_text(json.dumps(order()))
        path.chmod(0o600)

        def cli(args):
            out = io.StringIO()
            err = io.StringIO()
            with (
                patch("budget_tools.utils._client", return_value=self.client),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                code = main(args, store=self.store)
            self.assertEqual((code, err.getvalue()), (0, ""))
            return json.loads(out.getvalue())

        preview = cli(["purchases", "preview", "--proposal", str(path)])
        self.assertEqual(preview["remaining"], "96")
        draft = cli(["purchases", "draft-create", "--proposal", str(path)])
        self.assertIn("purchase_review", draft)
        result = cli(["drafts", "apply", draft["id"], "--confirm", draft["id"]])
        self.assertEqual(result["purchase_status"], "awaiting_charges")
        self.assertEqual(
            cli(["purchases", "get", result["purchase_id"]])["status"], "awaiting_charges"
        )

    def test_existing_posted_items_at_intake_reserve_only_uncovered_balance(self):
        actual = self.server.actual(24000)
        proposal = order()
        proposal["existing_charges"] = [
            {
                "transaction_id": actual,
                "units": ["A.1"],
                "evidence": "Fictional payment details confirm A already posted",
            }
        ]
        result = self.apply(self.draft(proposal))
        self.assertEqual(self.server.records[result["pending_transaction_id"]]["amount"], -72000)
        self.assertEqual(result["actual_transaction_ids"], [actual])
        self.assertEqual(
            sum(-r["amount"] for r in self.server.records.values() if not r["deleted"]), 96000
        )

    def test_possible_existing_full_charge_requires_explicit_evidence(self):
        actual = self.server.actual(96000)
        with self.assertRaisesRegex(CliError, "already-posted full order"):
            self.draft()
        proposal = order()
        proposal["exclude_charges"] = [
            {
                "transaction_id": actual,
                "evidence": "Fictional separate order invoice proves different purchase",
            }
        ]
        draft = self.draft(proposal)
        self.assertTrue(draft["proposal"]["excluded_charges"])
        self.server.records[actual]["memo"] = "Changed after exclusion review"
        with self.assertRaisesRegex(CliError, "exclusion changed"):
            self.apply(draft)
        self.assertEqual(self.server.writes(), [])

    def test_fifty_separate_charges_represent_every_item_once(self):
        proposal = order()
        proposal.update(order_total="50.00", card_total="50.00")
        proposal["items"] = [
            dict(
                proposal["items"][0],
                id="X" + str(i),
                description="Fictional item " + str(i),
                amount="1.00",
            )
            for i in range(50)
        ]
        proposal["existing_charges"] = [
            {
                "transaction_id": self.server.actual(1000, txid="posted-" + str(i)),
                "units": ["X" + str(i) + ".1"],
                "evidence": "Fictional invoice separately captures item " + str(i),
            }
            for i in range(50)
        ]
        result = self.apply(self.draft(proposal))
        self.assertEqual(result["purchase_status"], "settled")
        self.assertIsNone(result["pending_transaction_id"])
        self.assertEqual(len(result["actual_transaction_ids"]), 50)
        self.assertEqual(sum(c[0] == "POST" for c in self.server.writes()), 0)
        self.assertEqual(sum(c[0] == "DELETE" for c in self.server.writes()), 0)
        self.assertEqual(
            sum(-r["amount"] for r in self.server.records.values() if not r["deleted"]), 50000
        )

    def test_nonconserving_item_totals_rejected_before_creation(self):
        proposal = order()
        proposal["items"][2]["amount"] = "35.99"
        with self.assertRaisesRegex(CliError, "conserve"):
            self.draft(proposal)
        self.assertEqual(self.server.writes(), [])

    def test_review_seal_protects_batch_preview(self):
        draft = self.draft()
        path = self.store.draft_path(draft["id"])
        data = json.loads(path.read_text())
        data["summary"]["writes"] = []
        path.write_text(json.dumps(data))
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["drafts", "show", draft["id"]], store=self.store)
        self.assertEqual(code, 1)
        self.assertIn("Reviewed draft content changed", err.getvalue())
        self.assertEqual(self.server.writes(), [])

    def test_bad_readback_never_reports_success(self):
        draft = self.draft()

        def corrupt(method, path, result):
            if method == "POST":
                result["transaction"]["memo"] = "Corrupted context"

        self.server.after_write = corrupt
        with self.assertRaisesRegex(CliError, "did not verify"):
            self.apply(draft)
        self.assertEqual(self.store.load_draft(draft["id"])["status"], "uncertain")
        self.assertEqual(len(self.server.writes()), 1)

    def test_foreign_inflight_owner_blocks_batch(self):
        draft = self.draft()
        record = self.store.load_purchase(draft["proposal"]["model"]["id"])
        record["inflight_draft_id"] = "other-unrelated-batch"
        self.store.save_purchase(record)
        with self.assertRaisesRegex(CliError, "Another batch"):
            self.apply(draft)
        self.assertEqual(self.server.writes(), [])

    def test_unposted_authorization_not_actual_even_when_approved(self):
        record = self.open()
        txid = self.server.actual(24000, import_id=None, cleared="uncleared", approved=True)
        with self.assertRaisesRegex(CliError, "not identified as posted"):
            self.correction(record, [(txid, ["A.1"])])
        self.assertEqual(len(self.server.writes()), 1)

    def test_applied_actual_split_requires_native_correction_instead_of_recreation(self):
        record = self.open()
        txid = self.server.actual(60000)
        target = self.server.records[txid]
        self.server.set_rows(
            target,
            [
                {
                    "amount": -60000,
                    "category_id": "category-c",
                    "memo": "Meaningful unrelated allocation",
                }
            ],
        )
        with self.assertRaisesRegex(CliError, "needs_native_correction"):
            self.correction(record, [(txid, ["A.1", "B.1"])])
        self.assertFalse(target["deleted"])
        self.assertEqual(len(self.server.writes()), 1)

    def test_incoming_refund_and_known_mixed_tender_cannot_force_balance(self):
        proposal = order()
        proposal["gift_balance"] = "5.00"
        with self.assertRaisesRegex(CliError, "mixed tender"):
            self.draft(proposal)
        proposal = order()
        proposal["tenders"] = [{"amount": "48.00"}, {"amount": "48.00"}]
        with self.assertRaisesRegex(CliError, "one exact"):
            self.draft(proposal)
        record = self.open()
        txid = self.server.actual(-24000)
        with self.assertRaises(CliError):
            self.correction(record, [(txid, ["A.1"])])
        self.assertEqual(len(self.server.writes()), 1)

    def test_paid_charge_memo_preserves_original_context_and_marker_suffix(self):
        proposal = order()
        proposal["automation_marker"] = "(Example Marker)"
        record = self.open(proposal)
        self.assertTrue(record["pending"]["snapshot"]["memo"].endswith("(Example Marker)"))
        txid = self.server.actual(24000, memo="Original meaningful charge context")
        self.apply(self.correction(record, [(txid, ["A.1"])]))
        self.assertTrue(
            self.server.records[txid]["memo"].startswith("Original meaningful charge context")
        )
        self.assertTrue(self.server.records[txid]["memo"].endswith("(Example Marker)"))
        self.assertEqual(self.server.records[txid]["memo"].count("(Example Marker)"), 1)
        self.assertNotIn("PENDING", self.server.records[txid]["memo"])

    def test_fresh_host_finalizes_already_native_matched_estimate_without_deletion(self):
        record = self.open()
        txid = record["pending"]["id"]
        self.server.records[txid].update(import_id="synthetic-native", cleared="cleared")
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            fresh = StateStore(Path(directory))
            fresh.save_config(self.store.load_config())
            recovered = purchases.recover(self.client, fresh, txid, "account-a", until="2001-04-01")
            self.assertTrue(recovered["already_posted_target"])
            finalized = purchases.draft_finalize(
                self.client,
                fresh,
                txid,
                "account-a",
                "Fictional bank charge and original order verify all items",
                until="2001-04-01",
            )
            result = workflow.execute(
                fresh, finalized["id"], client_factory=lambda: self.client, confirm=finalized["id"]
            )
            self.assertEqual(result["purchase_status"], "settled")
        self.assertEqual([c[0] for c in self.server.writes()], ["POST", "PUT"])

    def test_pending_becomes_reconciled_before_retirement_stops_delete(self):
        record = self.open()
        actual = self.server.actual(60000)
        draft = self.correction(record, [(actual, ["A.1", "B.1"])])
        old = self.server.records[record["pending"]["id"]]

        def race(method, path):
            if method == "POST":
                old["cleared"] = "reconciled"

        self.server.before_write = race
        with self.assertRaisesRegex(CliError, "protected"):
            self.apply(draft)
        self.assertFalse(old["deleted"])
        self.assertEqual(sum(c[0] == "DELETE" for c in self.server.writes()), 0)

    def test_crash_after_remainder_creation_before_delete_resumes_existing_id(self):
        record = self.open()
        actual = self.server.actual(60000)
        draft = self.correction(record, [(actual, ["A.1", "B.1"])])
        original = workflow.save_progress

        def interrupt(client, store, parent, saved):
            original(client, store, parent, saved)
            if saved["operation"] == "create":
                raise KeyboardInterrupt()

        with patch("budget_tools.purchase_workflow.save_progress", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(draft)
        before = sum(c[0] == "POST" for c in self.server.writes())
        self.apply(draft, command="reconcile")
        result = self.apply(draft, command="resume")
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(sum(c[0] == "POST" for c in self.server.writes()), before)
        self.assertTrue(self.server.records[record["pending"]["id"]]["deleted"])

    def test_fresh_review_resolves_known_uncommitted_delete_without_recreating_remainder(self):
        record = self.open()
        actual = self.server.actual(60000)
        draft = self.correction(record, [(actual, ["A.1", "B.1"])])
        self.server.lost = "DELETE"
        # Timeout before deletion commits, so old estimate remains.
        self.server.before_write = (
            lambda method, path: (_ for _ in ()).throw(socket.timeout())
            if method == "DELETE"
            else None
        )
        with self.assertRaises(ApiError):
            self.apply(draft)
        self.server.before_write = None
        self.server.lost = None
        with self.assertRaisesRegex(CliError, "still present"):
            self.apply(draft, command="reconcile")
        replacement = purchases.draft_resume(
            self.client,
            self.store,
            draft["id"],
            "Fresh fictional review confirms actuals/remainder and exact old removal",
        )
        self.assertEqual(self.store.load_draft(draft["id"])["status"], "superseded")
        before = sum(c[0] == "POST" for c in self.server.writes())
        result = self.apply(replacement)
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(sum(c[0] == "POST" for c in self.server.writes()), before)

    def test_fresh_review_of_expired_unattempted_batch_is_not_replay(self):
        draft = self.draft()
        with patch("budget_tools.transaction_workflow.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=25)
            with self.assertRaisesRegex(CliError, "24 hours"):
                self.apply(draft)
            replacement = purchases.draft_resume(
                self.client,
                self.store,
                draft["id"],
                "Fresh fictional order evidence and exact pending entry review",
            )
        self.assertNotEqual(replacement["id"], draft["id"])
        self.assertEqual(self.store.load_draft(draft["id"])["status"], "superseded")
        self.assertTrue(self.apply(replacement)["verification"]["verified"])

    def test_missing_creation_id_cannot_be_replanned_or_guessed_by_amount(self):
        draft = self.draft()
        self.server.lost = "POST"
        with self.assertRaises(ApiError):
            self.apply(draft)
        with self.assertRaisesRegex(CliError, "Missing creation ID"):
            purchases.draft_resume(self.client, self.store, draft["id"], "Fresh order evidence")
        duplicate = copy.deepcopy(self.server.records["created-1"])
        duplicate["id"] = "duplicate-created"
        self.server.records[duplicate["id"]] = duplicate
        with self.assertRaisesRegex(CliError, "ambiguous"):
            purchases.recover_step(
                self.client,
                self.store,
                draft["id"],
                0,
                "created-1",
                "Fictional independently selected record",
            )
        self.assertEqual(sum(c[0] == "POST" for c in self.server.writes()), 1)

    def test_cancel_unattempted_batch_allows_reviewed_redraft(self):
        draft = self.draft()
        result = purchases.cancel_draft(self.store, draft["id"], draft["id"])
        self.assertEqual(result["financial_writes"], 0)
        replacement = self.draft()
        self.assertNotEqual(replacement["id"], draft["id"])
        self.assertTrue(self.apply(replacement)["verification"]["verified"])
        with self.assertRaises(CliError):
            purchases.cancel_draft(self.store, replacement["id"], replacement["id"])

    def test_unknown_versions_malformed_quantities_duplicate_memo_fields_fail_closed(self):
        for update in ({"schema_version": True}, {"schema_version": 2}, {"items": [None]}):
            proposal = order()
            proposal.update(update)
            with self.assertRaises(CliError):
                self.draft(proposal)
        proposal = order()
        proposal["automation_marker"] = "Original: 999"
        with self.assertRaisesRegex(CliError, "Duplicate recovery memo fields"):
            self.draft(proposal)
        record = self.open()
        self.server.records[record["pending"]["id"]]["memo"] += "\nCurrency: EUR"
        with self.assertRaisesRegex(CliError, "Duplicate recovery memo fields"):
            purchases.recover(self.client, self.store, record["pending"]["id"], "account-a")

    def test_bounded_search_abstains_when_state_space_truncated(self):
        units = {"X" + str(i): {"amount": i + 1} for i in range(25)}
        result = purchases.subset_candidates(units, 40, max_states=3)
        self.assertTrue(result["search_truncated"])
        self.assertTrue(result["ambiguous"])
        self.assertTrue(result["evidence_required"])

    def test_backorder_over_ten_days_discovered_without_guessing_date(self):
        record = self.open()
        txid = self.server.actual(36000, date="2001-04-30")
        result = purchases.inspect(self.client, self.store, record["id"], until="2001-05-01")
        self.assertEqual(result["candidates"][0]["transaction"]["id"], txid)
        self.assertTrue(result["candidates"][0]["ambiguous"])
        self.assertEqual(record["pending"]["snapshot"]["date"], "2001-01-30")

    def test_persistence_before_send_and_both_parent_and_child_locks(self):
        draft = self.draft()
        stepid = draft["payload"]["steps"][0]["id"]

        def before(method, path):
            parent = self.store.load_draft(draft["id"])
            child = self.store.load_draft(stepid)
            self.assertEqual(parent["status"], "applying")
            self.assertEqual(child["status"], "applying")

        self.server.before_write = before
        self.assertTrue(self.apply(draft)["verification"]["verified"])
        proposal = order()
        proposal["order_reference"] = "FICTIONAL-ORDER-B"
        another = self.draft(proposal)
        with patch.object(
            self.store, "save_draft", side_effect=StateError("Fictional persistence failure")
        ):
            with self.assertRaises(StateError):
                self.apply(another)
        self.assertEqual(sum(c[0] == "POST" for c in self.server.writes()), 1)

    def test_rate_limit_wait_revalidates_changed_pending_before_next_write(self):
        class Limiter:
            waiting = False

            def acquire(inner):
                if inner.waiting:
                    inner.waiting = False
                    self.server.records[old]["approved"] = True
                    return True
                return False

            def observe(inner, status, headers):
                if status == 429:
                    inner.waiting = True

        record = self.open()
        old = record["pending"]["id"]
        txid = self.server.actual(24000)
        draft = self.correction(record, [(txid, ["A.1"])])
        self.server.reject = 429
        self.client = YnabClient("synthetic-token", transport=self.server, rate_limiter=Limiter())
        with self.assertRaisesRegex(CliError, "protected"):
            self.apply(draft)
        self.assertEqual([c[0] for c in self.server.writes()], ["POST", "PUT"])
        self.assertFalse(self.server.records[old]["deleted"])

    def test_correct_remainder_native_match_during_interruption_requires_fresh_review(self):
        record = self.open()
        txid = self.server.actual(60000)
        draft = self.correction(record, [(txid, ["A.1", "B.1"])])
        original = workflow.save_progress

        def interrupt(client, store, parent, saved):
            original(client, store, parent, saved)
            if saved["operation"] == "create":
                self.server.records[saved["transaction_id"]].update(
                    import_id="synthetic-native-remainder", cleared="cleared"
                )
                raise KeyboardInterrupt()

        with patch("budget_tools.purchase_workflow.save_progress", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(draft)
        replacement = purchases.draft_resume(
            self.client,
            self.store,
            draft["id"],
            "Fictional bank/order evidence confirms remaining C native capture",
        )
        before = sum(c[0] == "POST" for c in self.server.writes())
        result = self.apply(replacement)
        self.assertEqual(result["purchase_status"], "settled")
        self.assertIsNone(result["pending_transaction_id"])
        self.assertEqual(sum(c[0] == "POST" for c in self.server.writes()), before)
        self.assertEqual(len(result["actual_transaction_ids"]), 2)

    def test_month_activity_deltas_show_old_estimate_removal_and_actual_reallocation(self):
        record = self.open()
        txid = self.server.actual(60000)
        draft = self.correction(record, [(txid, ["A.1", "B.1"])])
        changes = draft["summary"]["category_activity_changes_by_month"]
        self.assertEqual(changes["2001-01"], {"category-a": 24000, "category-b": 36000})
        self.assertEqual(
            changes["2001-02"], {"category-c": 60000, "category-a": -24000, "category-b": -36000}
        )
        self.assertIn("not exact Available", draft["summary"]["notice"])

    def test_shipment_hypotheses_never_create_additional_estimates(self):
        preview = purchases.draft_create(self.client, self.store, order(), preview=True)
        self.assertEqual(len(preview["proposed_steps"]), 1)
        groups = preview["charge_hypotheses"]["groups"]
        self.assertEqual([g["amount"] for g in groups], ["60", "36"])
        self.assertFalse(preview["charge_hypotheses"]["creates_extra_transactions"])
        self.assertEqual(self.server.writes(), [])

    def test_purchase_state_rejects_symlink_and_wrong_version(self):
        draft = self.draft()
        purchase_id = draft["proposal"]["model"]["id"]
        path = self.store.purchase_path(purchase_id)
        snapshot = path.read_bytes()
        path.unlink()
        alternate = self.store.root / "fictional-other.json"
        alternate.write_bytes(snapshot)
        alternate.chmod(0o600)
        path.symlink_to(alternate)
        with self.assertRaises(StateError):
            self.store.load_purchase(purchase_id)
        path.unlink()
        value = json.loads(snapshot)
        value["schema_version"] = True
        with self.assertRaises(StateError):
            self.store.save_purchase(value)

    def test_correction_rejects_boolean_schema_before_any_write(self):
        record = self.open()
        txid = self.server.actual(24000)
        before = len(self.server.writes())
        with self.assertRaisesRegex(CliError, "mismatch"):
            purchases.draft_reconcile(
                self.client,
                self.store,
                record["id"],
                {
                    "schema_version": True,
                    "purchase_id": record["id"],
                    "unresolved": [],
                    "charges": [
                        {"transaction_id": txid, "units": ["A.1"], "evidence": "Fictional invoice"}
                    ],
                },
            )
        self.assertEqual(len(self.server.writes()), before)

    def test_inspection_rejects_changed_currency_before_amount_suggestions(self):
        record = self.open()
        self.server.actual(24000)
        self.server.currency["iso_code"] = "EUR"
        before = len(self.server.writes())
        with self.assertRaisesRegex(CliError, "currency changed"):
            purchases.inspect(self.client, self.store, record["id"])
        self.assertEqual(len(self.server.writes()), before)

    def test_later_correction_rejects_prior_charge_with_changed_merchant_or_posted_state(self):
        record = self.open()
        first = self.server.actual(24000)
        self.apply(self.correction(record, [(first, ["A.1"])]))
        record = self.store.load_purchase(record["id"])
        second = self.server.actual(36000)
        before = len(self.server.writes())
        original = copy.deepcopy(self.server.records[first])
        for change in ({"payee_id": "wrong-payee"}, {"import_id": None, "cleared": "uncleared"}):
            with self.subTest(change=change):
                self.server.records[first] = dict(original, **change)
                with self.assertRaises(CliError):
                    self.correction(record, [(second, ["B.1"])])
                self.assertEqual(len(self.server.writes()), before)
        self.server.records[first] = original

    def test_fresh_host_preserves_user_marker_during_adoption_and_correction(self):
        proposal = order()
        proposal["automation_marker"] = "(Example Marker)"
        record = self.open(proposal)
        old = record["pending"]["id"]
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            fresh = StateStore(Path(directory))
            fresh.save_config(self.store.load_config())
            recovered = purchases.recover(self.client, fresh, old, "account-a")
            self.assertEqual(recovered["model"]["automation_marker"], "(Example Marker)")
            adopted = purchases.draft_adopt(
                self.client, fresh, old, "account-a", "Fictional order reviewed"
            )
            workflow.execute(
                fresh,
                adopted["id"],
                client_factory=lambda: self.client,
                confirm=adopted["id"],
                confirm_adopt=old,
            )
            txid = self.server.actual(24000)
            correction = purchases.draft_reconcile(
                self.client,
                fresh,
                record["id"],
                {
                    "schema_version": 1,
                    "purchase_id": record["id"],
                    "unresolved": [],
                    "charges": [
                        {"transaction_id": txid, "units": ["A.1"], "evidence": "Fictional invoice"}
                    ],
                },
            )
            result = workflow.execute(
                fresh,
                correction["id"],
                client_factory=lambda: self.client,
                confirm=correction["id"],
            )
            self.assertTrue(
                self.server.records[result["pending_transaction_id"]]["memo"].endswith(
                    "(Example Marker)"
                )
            )
            self.assertTrue(self.server.records[txid]["memo"].endswith("(Example Marker)"))

    def test_recovery_rejects_conflicting_linked_order_context(self):
        record = self.open()
        txid = self.server.actual(24000)
        result = self.apply(self.correction(record, [(txid, ["A.1"])]))
        self.server.records[txid]["memo"] = self.server.records[txid]["memo"].replace(
            "Currency: USD", "Currency: EUR"
        )
        before = len(self.server.writes())
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            fresh = StateStore(Path(directory))
            fresh.save_config(self.store.load_config())
            with self.assertRaisesRegex(CliError, "context is inconsistent"):
                purchases.recover(self.client, fresh, result["pending_transaction_id"], "account-a")
        self.assertEqual(len(self.server.writes()), before)

    def test_interrupted_adoption_reconcile_rereads_current_estimate_before_claiming_verified(self):
        record = self.open()
        old = record["pending"]["id"]
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            fresh = StateStore(Path(directory))
            fresh.save_config(self.store.load_config())
            adopted = purchases.draft_adopt(
                self.client, fresh, old, "account-a", "Fictional reviewed estimate"
            )
            with patch("budget_tools.purchase_workflow.finalize", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    workflow.execute(
                        fresh,
                        adopted["id"],
                        client_factory=lambda: self.client,
                        confirm=adopted["id"],
                        confirm_adopt=old,
                    )
            self.server.records[old]["approved"] = True
            with self.assertRaisesRegex(CliError, "protected"):
                workflow.execute(
                    fresh, adopted["id"], client_factory=lambda: self.client, command="reconcile"
                )
            self.assertNotEqual(fresh.load_draft(adopted["id"])["status"], "applied")
            self.assertIsNone(fresh.load_purchase(record["id"])["pending"])
            self.assertEqual(len(self.server.writes()), 1)
            self.server.records[old]["approved"] = False
            result = workflow.execute(
                fresh, adopted["id"], client_factory=lambda: self.client, command="reconcile"
            )
            self.assertEqual(result["stage"], "verified_adoption")
            self.assertEqual(len(self.server.writes()), 1)
