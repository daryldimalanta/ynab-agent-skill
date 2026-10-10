# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Targeted read-contract mutations; no files, credentials, or live API writes.

Every fault must be exercised and cause a test assertion failure. Loader,
fixture and other test errors invalidate the experiment, not count as kills.
"""
import copy
import io
import json
import unittest
from unittest.mock import MagicMock

from budget_tools import api, transactions
from validation.mutation_support import WORKFLOW_MODULES as WORKFLOW, patch_everywhere

API = (api,)

SUITE = "tests.test_transaction_reads.PostedTransactionTests"


def run(names):
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
        unittest.defaultTestLoader.loadTestsFromNames(names))
    return {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}


def main():
    control = run([SUITE])
    print(json.dumps({"control": control}), flush=True)
    if control["errors"] or control["failures"] or not control["tests"]:
        raise SystemExit("The unmodified read-contract tests must pass")

    safe_id, detail, summary, query = api._safe_id, transactions._transaction_detail, transactions._transaction_summary, transactions._transaction_query

    def reject_suffix(value):
        if "_" in value:
            raise ValueError("Injected legacy ID rejection")
        return safe_id(value)

    def alter_detail(kind):
        def broken(record):
            result = copy.deepcopy(detail(record))
            if kind == "omit_lines":
                result["subtransactions"] = []
            elif kind == "drop_memos":
                result["memo"] = None
                for line in result["subtransactions"]:
                    line["memo"] = None
            elif kind == "lose_sign":
                for line in result["subtransactions"]:
                    line["amount_milliunits"] = abs(line["amount_milliunits"])
            return result
        return broken

    def skip_suffixed(*args, **kwargs):
        records, account = query(*args, **kwargs)
        return [r for r in records if "_" not in r["id"]], account

    def fetch_individually(client, plan_id, **kwargs):
        records, account = query(client, plan_id, **kwargs)
        return [client.get_transaction(plan_id, r["id"]) for r in records], account

    def change_default(record):
        return dict(summary(record), unexpected_detail=True)

    def infer_date(record):
        result = summary(record)
        if "_" in result["id"]:
            result["date"] = result["id"].split("_")[-1]
        return result

    faults = [
        ("reject_suffixed_ids", API, "_safe_id", reject_suffix, "test_full_id_reaches_get_unchanged"),
        ("strip_id_suffix", API, "_safe_id", lambda value: safe_id(value).split("_")[0],
         "test_get_and_update_preserve_path_without_simulator_routing"),
        ("allow_unsafe_id_delimiters", API, "_safe_id", lambda value: value,
         "test_unsafe_delimiters_never_reach_accepting_transport"),
        ("omit_split_lines", WORKFLOW, "_transaction_detail", alter_detail("omit_lines"),
         "test_bulk_splits_preserve_exact_line_and_parent_context"),
        ("drop_parent_and_line_memos", WORKFLOW, "_transaction_detail", alter_detail("drop_memos"),
         "test_bulk_splits_preserve_exact_line_and_parent_context"),
        ("lose_split_amount_signs", WORKFLOW, "_transaction_detail", alter_detail("lose_sign"),
         "test_bulk_splits_preserve_exact_line_and_parent_context"),
        ("skip_posted_recurring_entries", WORKFLOW, "_transaction_query", skip_suffixed,
         "test_bulk_uses_one_list_request_and_never_fetches_each_record"),
        ("fetch_each_parent", WORKFLOW, "_transaction_query", fetch_individually,
         "test_bulk_uses_one_list_request_and_never_fetches_each_record"),
        ("change_default_list_contract", WORKFLOW, "_transaction_summary", change_default,
         "test_default_list_contract_is_unchanged"),
        ("infer_date_from_id", WORKFLOW, "_transaction_summary", infer_date,
         "test_default_list_contract_is_unchanged"),
    ]
    invalid = []
    for name, modules, symbol, broken, test in faults:
        injection = MagicMock(side_effect=broken)
        with patch_everywhere(symbol, injection, modules):
            result = run([SUITE + "." + test])
        exercised = injection.call_count > 0
        detected = exercised and result["failures"] > 0 and result["errors"] == 0
        print(json.dumps({"fault": name, **result, "exercised": exercised, "detected": detected}), flush=True)
        if not detected:
            invalid.append(name)
    if invalid:
        raise SystemExit("Missed or invalid read-contract faults: " + ", ".join(invalid))


if __name__ == "__main__":
    main()
