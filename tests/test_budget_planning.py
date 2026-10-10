# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Fictional month contracts exercised through the CLI and real HTTP serializer."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from budget_tools import planning
from budget_tools.api import YnabClient
from budget_tools.cli import main
from budget_tools.state import StateStore, TERMS_VERSION


class BudgetServer:
    """Independent endpoint model; PATCH replaces the total, never adds a delta."""
    def __init__(self):
        self.category = {"id": "category-a", "category_group_id": "group-a",
                         "name": "Example Category A", "hidden": False,
                         "deleted": False, "internal": False,
                         "budgeted": 64000, "activity": -19000, "balance": 57000,
                         "goal_type": "MF", "goal_target": 90000, "goal_under_funded": 26000}
        self.month = {"month": "2001-02-01", "deleted": False, "income": 400000,
                      "budgeted": 64000, "activity": -19000, "to_be_budgeted": 120000,
                      "categories": [self.category]}
        self.groups = [{"id": "group-a", "name": "Example Group A", "hidden": False,
                        "deleted": False, "categories": [dict(self.category, budgeted=999000, balance=888000)]}]
        self.currency = {"decimal_digits": 2, "iso_code": "USD"}
        self.requests = []
        self.patch_error = None
        self.after_write = None
        self.fail_readback = False
        self.omit_write_response = False

    @property
    def writes(self):
        return [r for r in self.requests if r[0] != "GET"]

    def transport(self, method, url, headers, body, timeout):
        path = urlsplit(url).path
        payload = json.loads(body) if body else None
        self.requests.append((method, path, payload))
        prefix = "/v1/plans/plan-a"
        if method == "PATCH":
            assert path == prefix + "/months/2001-02-01/categories/category-a"
            assert set(payload) == {"category"} and set(payload["category"]) == {"budgeted"}
            if self.patch_error:
                error = self.patch_error
                self.patch_error = None
                return error, b'{"error":{"name":"synthetic_rejection"}}'
            amount = payload["category"]["budgeted"]
            delta = amount - self.category["budgeted"]
            self.category["budgeted"] = amount
            self.category["balance"] += delta
            self.month["budgeted"] += delta
            self.month["to_be_budgeted"] -= delta
            if self.after_write:
                self.after_write()
            if self.omit_write_response:
                return 200, b'{}'
            return 200, json.dumps({"data": {"category": self.category}}).encode()
        assert method == "GET"
        if path == prefix + "/months":
            data = {"months": [{k: v for k, v in self.month.items() if k != "categories"}]}
        elif path == prefix + "/months/2001-02-01":
            if self.writes and self.fail_readback:
                return 503, b'{}'
            data = {"month": self.month}
        elif path == prefix + "/categories":
            data = {"category_groups": self.groups}
        elif path == prefix + "/settings":
            data = {"settings": {"currency_format": self.currency}}
        else:
            return 404, b'{}'
        return 200, json.dumps({"data": data}).encode()


class BudgetPlanningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = patch("budget_tools.state.assert_local_storage")
        self.storage.start()
        self.addCleanup(self.storage.stop)
        self.store = StateStore(Path(self.directory.name) / "state")
        self.store.save_config({"terms_acknowledged_version": TERMS_VERSION, "plan_id": "plan-a"})
        self.server = BudgetServer()
        self.client = YnabClient("synthetic-token", transport=self.server.transport)

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("budget_tools.utils._client", return_value=self.client), redirect_stdout(out), redirect_stderr(err):
            status = main(list(args), store=self.store)
        return status, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def draft(self, *amount):
        args = amount or ("--assigned", "81.25")
        status, output, err = self.command("budgets", "draft-assign", "2001-02", "--category", "category-a", *args)
        self.assertEqual(status, 0, err)
        return output["id"]

    def apply(self, draft_id):
        return self.command("drafts", "apply", draft_id, "--confirm", draft_id)

    def test_read_month_uses_available_with_rollover_and_monthly_figures(self):
        status, output, err = self.command("budgets", "get", "2001-02")
        self.assertEqual(status, 0, err)
        category = output["categories"][0]
        self.assertEqual(category["assigned"], {"milliunits": 64000, "currency": "64"})
        self.assertEqual(category["activity"]["currency"], "-19")
        self.assertEqual(category["available"]["currency"], "57")  # includes rollover
        self.assertEqual(output["ready_to_assign"]["currency"], "120")
        self.assertEqual(output["currency_format"]["iso_code"], "USD")
        self.assertEqual(category["goal"]["underfunded"]["currency"], "26")
        self.assertFalse(self.server.writes)

    def test_month_list_returns_authoritative_totals_without_categories_request(self):
        status, output, err = self.command("budgets", "months")
        self.assertEqual(status, 0, err)
        self.assertEqual(output["months"][0]["month"], "2001-02-01")
        self.assertEqual(output["months"][0]["assigned"]["milliunits"], 64000)
        self.assertEqual(output["currency_format"]["iso_code"], "USD")
        # Currency metadata is required for readable reporting; no category fetch.
        self.assertEqual([r[1] for r in self.server.requests],
                         ["/v1/plans/plan-a/months", "/v1/plans/plan-a/settings"])

    def test_filtered_read_keeps_whole_month_scope_and_hidden_balances(self):
        self.server.category["hidden"] = True
        self.server.category["balance"] = -3000
        status, output, err = self.command("budgets", "get", "2001-02", "--category", "Example Group A: Example Category A")
        self.assertEqual(status, 0, err)
        self.assertEqual(output["totals_scope"], "whole_month")
        self.assertTrue(output["categories"][0]["overspent"])
        self.assertEqual(output["categories"][0]["available"]["currency"], "-3")

    def test_explicit_month_validation_and_future_support(self):
        self.assertEqual(planning.month_key("2100-02"), "2100-02-01")
        for month in ("current", "2001-2", "2001-02-03", "2001-13", "2001-02/other", "../current"):
            with self.subTest(month=month):
                status, _, _ = self.command("budgets", "get", month)
                self.assertEqual(status, 1)
        self.assertFalse(self.server.requests)

    def test_wrong_month_or_invalid_amount_response_fails_closed(self):
        for key, value in (("month", "2001-03-01"), ("budgeted", True)):
            original = self.server.month[key]
            self.server.month[key] = value
            self.assertEqual(self.command("budgets", "get", "2001-02")[0], 1)
            self.server.month[key] = original

    def test_draft_is_read_only_and_displays_absolute_total_and_delta(self):
        draft_id = self.draft("--increase", "17.25")
        status, shown, err = self.command("drafts", "show", draft_id)
        self.assertEqual(status, 0, err)
        self.assertEqual(shown["budget_review"], {"month": "2001-02-01", "category_id": "category-a"})
        changes = shown["changes"]
        self.assertEqual(changes["assigned_after"]["currency"], "81.25")
        self.assertEqual(changes["delta"]["currency"], "17.25")
        self.assertEqual(changes["estimated_available_after"]["currency"], "74.25")
        self.assertEqual(changes["estimated_ready_to_assign_after"]["currency"], "102.75")
        self.assertFalse(self.server.writes)

    def test_decrease_and_negative_assignment_preserve_sign(self):
        draft_id = self.draft("--decrease", "70")
        self.assertEqual(self.store.load_draft(draft_id)["payload"], {"budgeted": -6000})
        draft_id = self.draft("--assigned=-6")
        self.assertEqual(self.store.load_draft(draft_id)["payload"], {"budgeted": -6000})

    def test_no_op_invalid_precision_nonfinite_and_range_do_not_draft(self):
        for amount in ("64", "1.001", "NaN", "Infinity", "9007199254741", "0.0001"):
            with self.subTest(amount=amount):
                status, _, _ = self.command("budgets", "draft-assign", "2001-02", "--category", "category-a", "--assigned", amount)
                self.assertEqual(status, 1)
        self.assertEqual(self.store.list_drafts(), [])
        self.assertFalse(self.server.writes)

    def test_zero_and_three_decimal_currency_assignments(self):
        self.server.currency["decimal_digits"] = 3
        draft_id = self.draft("--assigned", "0.001")
        self.assertEqual(self.store.load_draft(draft_id)["payload"]["budgeted"], 1)
        draft_id = self.draft("--assigned", "0")
        self.assertEqual(self.store.load_draft(draft_id)["payload"]["budgeted"], 0)

    def test_negative_forecasts_are_visible_for_human_review(self):
        draft_id = self.draft("--assigned", "200")
        self.assertIn("Estimated Ready to Assign is negative", self.store.load_draft(draft_id)["changes"]["warnings"])
        draft_id = self.draft("--decrease", "70")
        self.assertIn("Estimated category availability is negative", self.store.load_draft(draft_id)["changes"]["warnings"])

    def test_ambiguous_category_requires_id(self):
        other = dict(self.server.category, id="category-b")
        self.server.month["categories"].append(other)
        self.assertEqual(self.command("budgets", "get", "2001-02", "--category", "Example Category A")[0], 1)
        self.assertEqual(self.command("budgets", "get", "2001-02", "--category", "category-a")[0], 0)

    def test_hidden_internal_deleted_or_missing_groups_cannot_be_assigned(self):
        for target, field in ((self.server.category, "hidden"), (self.server.category, "internal"),
                              (self.server.category, "deleted"), (self.server.groups[0], "hidden"),
                              (self.server.groups[0], "deleted"), (self.server.groups[0], "internal")):
            target[field] = True
            self.assertEqual(self.command("budgets", "draft-assign", "2001-02", "--category", "category-a", "--assigned", "81.25")[0], 1)
            target[field] = False
        self.server.groups.clear()
        self.assertEqual(self.command("budgets", "draft-assign", "2001-02", "--category", "category-a", "--assigned", "81.25")[0], 1)
        self.assertFalse(self.server.writes)

    def test_credit_card_payment_assignment_is_allowed(self):
        self.server.groups[0]["name"] = "Credit Card Payments"
        self.assertIsInstance(self.draft(), str)

    def test_apply_patches_exact_month_and_absolute_amount_then_reads_back(self):
        draft_id = self.draft()
        status, result, err = self.apply(draft_id)
        self.assertEqual(status, 0, err)
        self.assertEqual(self.server.writes, [("PATCH", "/v1/plans/plan-a/months/2001-02-01/categories/category-a", {"category": {"budgeted": 81250}})])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["readback"]["category"]["available"]["milliunits"], 74250)
        self.assertEqual(result["readback"]["ready_to_assign"]["milliunits"], 102750)
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(self.server.requests[-2][0:2], ("GET", "/v1/plans/plan-a/months/2001-02-01"))

    def test_missing_confirmation_and_terms_block_writes(self):
        draft_id = self.draft()
        self.assertEqual(self.command("drafts", "apply", draft_id, "--confirm", "wrong")[0], 1)
        self.store.save_config({"plan_id": "plan-a"})
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertFalse(self.server.writes)

    def test_selected_plan_change_blocks_application(self):
        draft_id = self.draft()
        self.store.save_config({"terms_acknowledged_version": TERMS_VERSION, "plan_id": "plan-b"})
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertFalse(self.server.writes)

    def test_changed_budget_or_currency_requires_new_review(self):
        for target, key, value in ((self.server.category, "budgeted", 66000),
                                   (self.server.category, "activity", -21000),
                                   (self.server.category, "balance", 55000),
                                   (self.server.month, "to_be_budgeted", 110000),
                                   (self.server.groups[0], "name", "Example Group B"),
                                   (self.server.currency, "decimal_digits", 0)):
            draft_id = self.draft()
            old = target[key]
            target[key] = value
            self.assertEqual(self.apply(draft_id)[0], 1)
            target[key] = old
        self.assertFalse(self.server.writes)

    def test_review_seal_covers_month_category_amount_and_before_state(self):
        for key, value in (("proposal", {"month": "2001-03-01", "category_id": "category-b"}),
                           ("payload", {"budgeted": 1}), ("before_detail", {})):
            draft_id = self.draft()
            path = self.store.draft_path(draft_id)
            data = json.loads(path.read_text())
            data[key] = value
            path.write_text(json.dumps(data))
            self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertFalse(self.server.writes)

    def test_expired_draft_is_not_applied(self):
        draft_id = self.draft()
        later = datetime.now(timezone.utc) + timedelta(hours=25)
        with patch.object(planning, "datetime", wraps=datetime) as clock:
            clock.now.return_value = later
            self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertFalse(self.server.writes)

    def test_checkpoint_failure_prevents_send(self):
        draft_id = self.draft()
        with patch.object(self.store, "save_draft", side_effect=OSError("synthetic persistence failure")):
            self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertFalse(self.server.writes)

    def test_locks_surround_read_revalidation_and_write(self):
        held = []
        original = self.store.exclusive_lock
        transport = self.server.transport
        @contextmanager
        def lock(scope):
            with original(scope):
                held.append(scope)
                try:
                    yield
                finally:
                    held.pop()
        def guarded(*args):
            self.assertEqual(held, [f"draft:{draft_id}", "budget-plan:plan-a"])
            if args[0] == "PATCH":
                self.assertEqual(self.store.load_draft(draft_id)["status"], "applying")
            return transport(*args)
        draft_id = self.draft()
        self.client._transport = guarded
        with patch.object(self.store, "exclusive_lock", side_effect=lock):
            self.assertEqual(self.apply(draft_id)[0], 0)

    def test_readback_mismatch_blocks_success_and_replay(self):
        draft_id = self.draft()
        self.server.after_write = lambda: self.server.category.update(budgeted=99000)
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.store.load_draft(draft_id)["status"], "verification_failed")
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(len(self.server.writes), 1)

    def test_timeout_after_write_reconciles_without_resending(self):
        draft_id = self.draft()
        def timeout():
            raise TimeoutError("synthetic lost response")
        self.server.after_write = timeout
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.store.load_draft(draft_id)["status"], "uncertain")
        self.assertEqual(self.apply(draft_id)[0], 1)
        status, result, err = self.command("drafts", "reconcile", draft_id)
        self.assertEqual(status, 0, err)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(self.server.writes), 1)

    def test_failed_readback_remains_recoverable_and_reconcile_is_read_only(self):
        draft_id = self.draft()
        self.server.fail_readback = True
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.store.load_draft(draft_id)["status"], "verification_pending")
        self.assertEqual(self.command("drafts", "resume", draft_id, "--confirm", draft_id)[0], 1)
        self.server.fail_readback = False
        self.assertEqual(self.command("drafts", "reconcile", draft_id)[0], 0)
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(len(self.server.writes), 1)

    def test_empty_write_response_still_requires_authoritative_readback(self):
        draft_id = self.draft()
        self.server.omit_write_response = True
        self.assertEqual(self.apply(draft_id)[0], 0)
        self.assertTrue(self.store.load_draft(draft_id)["verification"]["verified"])

    def test_429_retry_revalidates_before_resending(self):
        draft_id = self.draft()
        server = self.server
        class Limiter:
            def acquire(self):
                return False
            def observe(self, status, headers):
                if status == 429:
                    server.month["to_be_budgeted"] -= 1000
        self.client._rate_limiter = Limiter()
        self.server.patch_error = 429
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(len(self.server.writes), 1)
        self.assertEqual(self.server.category["budgeted"], 64000)
        self.assertEqual(self.store.load_draft(draft_id)["status"], "pending")

    def test_capacity_wait_revalidates_before_first_write(self):
        draft_id = self.draft()
        server = self.server
        class Limiter:
            calls = 0
            def acquire(self):
                self.calls += 1
                if self.calls == 4:  # month, groups and settings GETs precede PATCH
                    server.category["balance"] -= 1000
                    return True
                return False
            def observe(self, status, headers):
                pass
        self.client._rate_limiter = Limiter()
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertFalse(self.server.writes)

    def test_expiry_after_rate_limit_prevents_retry(self):
        draft_id = self.draft()
        server = self.server
        class Limiter:
            def acquire(self):
                return False
            def observe(self, status, headers):
                pass
        self.client._rate_limiter = Limiter()
        self.server.patch_error = 429
        now = datetime.now(timezone.utc)
        with patch.object(planning, "datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda tz: now + (timedelta(hours=25) if server.writes else timedelta())
            self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(len(self.server.writes), 1)

    def test_unchanged_429_rejection_retries_once_and_verifies(self):
        draft_id = self.draft()
        class Limiter:
            def acquire(self):
                return False
            def observe(self, status, headers):
                pass
        self.client._rate_limiter = Limiter()
        self.server.patch_error = 429
        self.assertEqual(self.apply(draft_id)[0], 0)
        self.assertEqual(len(self.server.writes), 2)
        self.assertEqual(self.server.category["budgeted"], 81250)

    def test_rejected_and_uncertain_requests_are_not_replayed(self):
        for status in (400, 403, 500):
            draft_id = self.draft()
            self.server.patch_error = status
            self.assertEqual(self.apply(draft_id)[0], 1)
            self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(len(self.server.writes), 3)

    def test_api_path_delimiters_never_reach_transport(self):
        for bad in ("../other", "x/y", "x?y", "x%2Fy"):
            with self.assertRaises(ValueError):
                self.client.get_month("plan-a", bad)
            with self.assertRaises(ValueError):
                self.client.update_month_category("plan-a", "2001-02-01", bad, 1000)
        self.assertFalse(self.server.requests)


if __name__ == "__main__":
    unittest.main()
