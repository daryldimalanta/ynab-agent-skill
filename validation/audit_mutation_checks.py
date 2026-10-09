# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Bounded test-effectiveness experiment; mutates functions in memory only.

Run from the workspace: python3 -m validation.audit_mutation_checks
No production file changes, personal data, credentials, or live API calls.
The result is targeted sensitivity evidence, not a general mutation score.
"""
import copy
import io
import json
import re
import unittest
from unittest.mock import patch

from budget_tools import api, cli

BASELINE = ["tests.test_budget_tools", "tests.test_receipts", "tests.test_receipt_creation",
            "validation.test_receipt_workflow_requirements", "validation.test_post_implementation_safety",
            "validation.test_repair_safety"]
ADDED = ["tests.test_receipt_contract_workflow"]


def run(names):
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    failures = result.failures
    return {"tests": result.testsRun, "detected": bool(failures) and not result.errors,
            "failure_count": len(failures), "errors": len(result.errors),
            "first_failure": failures[0][0].id() if failures else None}


def request_fault(kind):
    original = api.YnabClient.request

    def broken(self, method, path, *, params=None, body=None):
        params, body = copy.deepcopy(params), copy.deepcopy(body)
        if kind == "drop_until_date" and params:
            params.pop("until_date", None)
        if kind == "lose_account_scope":
            path = re.sub(r"/accounts/[^/]+/transactions$", "/transactions", path)
        if kind == "wrong_write_wrapper" and method in {"POST", "PUT"} and body:
            body = body.get("transaction", body)
        if kind == "drop_split_memos" and body and "transaction" in body:
            for line in body["transaction"].get("subtransactions", []):
                line.pop("memo", None)
        if kind == "drop_parent_marker" and body and "transaction" in body:
            memo = body["transaction"].get("memo")
            suffix = " (Example Marker)"
            if isinstance(memo, str) and memo.endswith(suffix):
                body["transaction"]["memo"] = memo[:-len(suffix)]
        return original(self, method, path, params=params, body=body)

    return patch.object(api.YnabClient, "request", broken)


def main():
    control = run(BASELINE + ADDED)
    print(json.dumps({"control": control}), flush=True)
    if control["failure_count"] or control["errors"] or not control["tests"]:
        raise SystemExit("Unmodified tests must pass before mutation results can be interpreted")
    invalid = []
    for name in ("drop_until_date", "lose_account_scope", "wrong_write_wrapper", "drop_split_memos", "drop_parent_marker",
                 "skip_expiry", "skip_receipt_verification"):
        if name == "skip_expiry":
            mutation = patch.object(cli, "_check_draft_age", lambda draft: None)
        elif name == "skip_receipt_verification":
            mutation = patch.object(cli, "_receipt_verification",
                                    lambda *args, **kwargs: {"verified": True, "mismatches": {}})
        else:
            mutation = request_fault(name)
        with mutation:
            baseline, added = run(BASELINE), run(ADDED)
        detected = (baseline["detected"] or added["detected"]) and not (baseline["errors"] or added["errors"])
        print(json.dumps({"fault": name, "baseline_suite": baseline, "added_contract_suite": added,
                          "combined_detected": detected}), flush=True)
        if not detected:
            invalid.append(name)
    if invalid:
        raise SystemExit("Missed or invalid general faults: " + ", ".join(invalid))


if __name__ == "__main__":
    main()
