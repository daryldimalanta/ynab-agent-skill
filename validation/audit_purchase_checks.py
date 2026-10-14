# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Purchase-specific safety faults require passing controls and exercised assertions."""

import io
import json
import unittest
from unittest.mock import patch

from budget_tools import purchases, purchase_memos, purchase_workflow, state, transaction_workflow

SUITE = "tests.test_purchases.PurchaseTests"


def run(names):
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
        unittest.defaultTestLoader.loadTestsFromNames(names)
    )
    return {
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }


def main():
    original_parent = purchase_memos.parent_memo

    def drop_context(*args, **kwargs):
        return original_parent(*args, **kwargs).replace("May post as separate charges", "")

    def assume_unique(units, target, **options):
        key = next(k for k, u in units.items() if u["amount"] == target)
        return {
            "allocations": [[key]],
            "ambiguous": False,
            "evidence_required": False,
            "search_truncated": False,
        }

    def allow_child(store, draft):
        return store.load_draft(draft["proposal"]["parent_draft_id"])

    def ignore_owner(store, parent):
        return store.load_purchase(parent["proposal"]["model"]["id"])

    faults = [
        (
            "discard_charge_split_context",
            purchase_memos,
            "parent_memo",
            drop_context,
            "test_initial_split_real_categories_portable_context_and_private_ledger",
        ),
        (
            "assume_equal_prices_unique",
            purchases,
            "subset_candidates",
            assume_unique,
            "test_equal_prices_are_ambiguous_and_delivery_not_billing_proof",
        ),
        (
            "skip_card_total_conservation",
            purchases,
            "require_total",
            lambda *a: None,
            "test_nonconserving_item_totals_rejected_before_creation",
        ),
        (
            "treat_authorization_as_posted",
            purchases,
            "posted",
            lambda *a: True,
            "test_unposted_authorization_not_actual_even_when_approved",
        ),
        (
            "allow_standalone_child_writes",
            purchase_workflow,
            "require_step_authority",
            allow_child,
            "test_child_drafts_cannot_bypass_parent_guarded_execution",
        ),
        (
            "ignore_inflight_purchase_owner",
            purchase_workflow,
            "active_record",
            ignore_owner,
            "test_foreign_inflight_owner_blocks_batch",
        ),
        (
            "skip_purchase_readback",
            transaction_workflow,
            "_expected_verification",
            lambda *a: {"verified": True, "mismatches": {}},
            "test_bad_readback_never_reports_success",
        ),
        (
            "skip_purchase_review_seal",
            state,
            "_verify_review",
            lambda *a: None,
            "test_review_seal_protects_batch_preview",
        ),
    ]
    control = run([SUITE + "." + row[-1] for row in faults])
    print(json.dumps({"control": control}), flush=True)
    if not control["tests"] or control["failures"] or control["errors"]:
        raise SystemExit("Unmodified purchase controls must pass before fault injection")
    invalid = []
    for name, module, symbol, broken, test in faults:
        with patch.object(module, symbol, side_effect=broken) as injected:
            result = run([SUITE + "." + test])
        exercised = injected.call_count > 0
        detected = exercised and result["failures"] > 0 and result["errors"] == 0
        print(
            json.dumps({"fault": name, **result, "exercised": exercised, "detected": detected}),
            flush=True,
        )
        if not detected:
            invalid.append(name)
    if invalid:
        raise SystemExit("Missed or invalid purchase faults: " + ", ".join(invalid))


if __name__ == "__main__":
    main()
