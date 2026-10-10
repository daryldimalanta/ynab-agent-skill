# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Bounded missing-payee fault injection. Synthetic API and temporary state only."""
import copy
import io
import json
import unittest
from unittest.mock import patch

from budget_tools import api, cli
from validation.mutation_support import patch_everywhere


def run(names):
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    return {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}


def main():
    suite = "tests.test_new_payee.NewPayeeTests"
    control = run([suite])
    print(json.dumps({"control": control}), flush=True)
    if control["failures"] or control["errors"] or not control["tests"]:
        raise SystemExit("The unmodified new-payee tests must pass")

    request = api.YnabClient.request
    verify = cli._receipt_verification

    def drop_name(self, method, path, *, params=None, body=None):
        body = copy.deepcopy(body)
        if method == "POST" and body:
            body.get("transaction", {}).pop("payee_name", None)
        return request(self, method, path, params=params, body=body)

    def ignore_name(*args, **kwargs):
        result = verify(*args, **kwargs)
        result["mismatches"].pop("payee_name", None)
        result["verified"] = not result["mismatches"]
        return result

    faults = [
        ("skip_missing_name_checks", patch_everywhere("_require_missing_payee", lambda *args: None),
         ["test_new_payee_appearing_before_apply_requires_fresh_review",
          "test_new_payee_appearing_during_429_wait_blocks_retry"]),
        ("drop_new_payee_name_in_request", patch.object(api.YnabClient, "request", drop_name),
         ["test_success_creates_once_then_verifies_and_approves"]),
        ("ignore_wrong_payee_name", patch_everywhere("_receipt_verification", ignore_name),
         ["test_name_changed_before_approval_blocks_approval"]),
        ("forget_assigned_payee_id", patch_everywhere("_record_created_payee", lambda *args: None),
         ["test_same_name_different_id_after_ack_blocks_approval",
          "test_identity_change_during_approval_wait_blocks_approval"]),
    ]
    missed = []
    for name, fault, tests in faults:
        with fault:
            result = run([suite + "." + test for test in tests])
        detected = bool(result["failures"]) and not result["errors"]
        print(json.dumps({"fault": name, **result, "detected": detected}), flush=True)
        if not detected:
            missed.append(name)
    if missed:
        raise SystemExit("Missed or invalid new-payee faults: " + ", ".join(missed))


if __name__ == "__main__":
    main()
