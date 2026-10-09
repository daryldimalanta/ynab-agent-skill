# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Budget safety fault injection with a passing control and assertion-only kills."""
import copy
import io
import json
import unittest
from unittest.mock import patch

from budget_tools import planning, state
from budget_tools.api import YnabClient

SUITE = "tests.test_budget_planning.BudgetPlanningTests"


def run(names):
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
        unittest.defaultTestLoader.loadTestsFromNames(names))
    return {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}


def main():
    control = run([SUITE])
    print(json.dumps({"control": control}), flush=True)
    if control["errors"] or control["failures"] or not control["tests"]:
        raise SystemExit("Unmodified budget tests must pass before injecting faults")
    original_records = planning._records
    original_update = YnabClient.update_month_category

    def lose_rollover(*args):
        summary, records = original_records(*args)
        records = copy.deepcopy(records)
        for category in records:
            category["available"] = planning.money(category["assigned"]["milliunits"] + category["activity"]["milliunits"])
        return summary, records

    def wrong_amount(client, plan, month, category, amount):
        return original_update(client, plan, month, category, amount + 1000)

    def fake_readback(client, store, draft):
        return {"status": "applied", "verification": {"verified": True}}

    faults = [
        ("discard_rollover", planning, "_records", lose_rollover,
         "test_read_month_uses_available_with_rollover_and_monthly_figures"),
        ("skip_stale_state_check", planning, "_revalidate", lambda *args: None,
         "test_changed_budget_or_currency_requires_new_review"),
        ("skip_expiry", planning, "_check_age", lambda *args: None,
         "test_expired_draft_is_not_applied"),
        ("skip_review_seal", state, "_verify_review", lambda *args: None,
         "test_review_seal_covers_month_category_amount_and_before_state"),
        ("skip_readback_verification", planning, "_readback", fake_readback,
         "test_readback_mismatch_blocks_success_and_replay"),
        ("send_different_amount", YnabClient, "update_month_category", wrong_amount,
         "test_apply_patches_exact_month_and_absolute_amount_then_reads_back"),
        ("allow_hidden_assignment", planning, "_writable", lambda *args: None,
         "test_hidden_internal_deleted_or_missing_groups_cannot_be_assigned"),
    ]
    invalid = []
    for name, module, symbol, broken, test in faults:
        # autospec preserves method binding when patching the API class.
        with patch.object(module, symbol, autospec=True, side_effect=broken) as injection:
            result = run([SUITE + "." + test])
        exercised = injection.call_count > 0
        detected = exercised and result["failures"] > 0 and result["errors"] == 0
        print(json.dumps({"fault": name, **result, "exercised": exercised, "detected": detected}), flush=True)
        if not detected:
            invalid.append(name)
    if invalid:
        raise SystemExit("Missed or invalid budget faults: " + ", ".join(invalid))


if __name__ == "__main__":
    main()
