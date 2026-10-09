# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

from __future__ import annotations

import unittest
from decimal import localcontext

from budget_tools.receipts import ReceiptProposalError, decimal_to_milliunits, validate_receipt_proposal


TRANSACTION = {
    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "account_id": "11111111-1111-1111-1111-111111111111",
    "date": "2001-01-30",
    "amount": -43250,
}


def category(reference):
    categories = {
        "Example Group A: Example Category A": {
            "id": "33333333-3333-3333-3333-333333333333",
            "group_name": "Example Group A", "name": "Example Category A",
        },
        "Example Group B: Example Category B": {
            "id": "44444444-4444-4444-4444-444444444444",
            "group_name": "Example Group B", "name": "Example Category B",
        },
    }
    return categories[reference]


def proposal():
    return {
        "version": 1,
        "plan_id": "99999999-9999-9999-9999-999999999999",
        "transaction_id": TRANSACTION["id"],
        "expected": {
            "account_id": TRANSACTION["account_id"],
            "date": TRANSACTION["date"],
            "amount_milliunits": TRANSACTION["amount"],
        },
        "receipt": {
            "reference": "synthetic-receipt",
            "match_evidence": "Exact account, total, and date",
            "total": "43.25",
            "components": [
                {"label": "Example Category C", "amount": "30.00"},
                {"label": "Example Category B", "amount": "10.00"},
                {"label": "Tax", "amount": "3.25"},
            ],
            "tenders": [{"label": "Example Account A", "amount": "43.25"}],
            "unresolved": [],
        },
        "parent_memo": "Receipt reviewed",
        "allocations": [
            {
                "category": "Example Group A: Example Category A", "amount": "30.00",
                "memo": "Example Category C", "evidence": "Receipt food lines",
            },
            {
                "category": "Example Group B: Example Category B", "amount": "13.25",
                "memo": "Example Category E and tax", "evidence": "Receipt household and tax lines",
            },
        ],
        "approve_after_verification": True,
    }


class ReceiptProposalTests(unittest.TestCase):
    def test_range_check_does_not_round_under_decimal_context(self):
        with localcontext() as context:
            context.prec = 6
            with self.assertRaises(ReceiptProposalError):
                decimal_to_milliunits("9223372036854775.808", field="amount", decimal_digits=3)
            self.assertEqual(
                decimal_to_milliunits("9223372036854775.807", field="amount", decimal_digits=3),
                9223372036854775807,
            )

    def test_extreme_exponent_is_a_validation_error(self):
        with self.assertRaises(ReceiptProposalError):
            decimal_to_milliunits("1e1000000", field="amount", decimal_digits=3)

    def validate(self, value):
        return validate_receipt_proposal(
            value, transaction=TRANSACTION,
            plan_id="99999999-9999-9999-9999-999999999999",
            decimal_digits=2, resolve_category=category,
        )

    def test_balanced_proposal_normalizes_signed_allocations(self):
        result = self.validate(proposal())
        self.assertEqual(
            [item["amount_milliunits"] for item in result["allocations"]],
            [-30000, -13250],
        )
        self.assertTrue(result["approve_after_verification"])

    def test_wrong_total_or_allocation_is_rejected(self):
        wrong_total = proposal()
        wrong_total["receipt"]["total"] = "43.24"
        with self.assertRaisesRegex(ReceiptProposalError, "receipt total"):
            self.validate(wrong_total)
        wrong_allocation = proposal()
        wrong_allocation["allocations"][1]["amount"] = "13.24"
        with self.assertRaisesRegex(ReceiptProposalError, "allocations"):
            self.validate(wrong_allocation)

    def test_unresolved_and_excessive_memo_are_rejected(self):
        unresolved = proposal()
        unresolved["receipt"]["unresolved"] = ["Taxability unknown"]
        with self.assertRaisesRegex(ReceiptProposalError, "unresolved"):
            self.validate(unresolved)
        long_memo = proposal()
        long_memo["allocations"][0]["memo"] = "x" * 501
        with self.assertRaisesRegex(ReceiptProposalError, "500"):
            self.validate(long_memo)

    def test_500_character_memo_is_supported(self):
        value = proposal()
        value["allocations"][0]["memo"] = "x" * 500
        result = self.validate(value)
        self.assertEqual(len(result["allocations"][0]["memo"]), 500)

    def test_multiple_tenders_are_rejected_until_account_mapping_is_supported(self):
        value = proposal()
        value["receipt"]["tenders"] = [
            {"label": "Gift card", "amount": "10.00"},
            {"label": "Example Account A", "amount": "33.25"},
        ]
        with self.assertRaisesRegex(ReceiptProposalError, "Multiple tenders"):
            self.validate(value)

    def test_discount_and_tip_components_reconcile_exactly(self):
        value = proposal()
        value["receipt"]["components"] = [
            {"label": "Items", "amount": "44.25"},
            {"label": "Discount", "amount": "-3.00"},
            {"label": "Tip", "amount": "2.00"},
        ]
        result = self.validate(value)
        self.assertEqual(
            sum(item["amount_milliunits"] for item in result["receipt"]["components"]),
            43250,
        )

    def test_currency_precision_is_enforced(self):
        value = proposal()
        value["allocations"][0]["amount"] = "30.001"
        with self.assertRaisesRegex(ReceiptProposalError, "decimal places"):
            self.validate(value)

    def test_zero_and_three_decimal_currency_precision(self):
        zero_transaction = dict(TRANSACTION, amount=-42000)
        zero = proposal()
        zero["expected"]["amount_milliunits"] = -42000
        zero["receipt"]["total"] = "42"
        zero["receipt"]["components"] = [{"label": "Items", "amount": "42"}]
        zero["receipt"]["tenders"] = [{"label": "Cash", "amount": "42"}]
        zero["allocations"][0]["amount"] = "30"
        zero["allocations"][1]["amount"] = "12"
        result = validate_receipt_proposal(
            zero, transaction=zero_transaction,
            plan_id="99999999-9999-9999-9999-999999999999",
            decimal_digits=0, resolve_category=category,
        )
        self.assertEqual(result["receipt"]["total_milliunits"], 42000)

        three_transaction = dict(TRANSACTION, amount=-42125)
        three = proposal()
        three["expected"]["amount_milliunits"] = -42125
        three["receipt"]["total"] = "42.125"
        three["receipt"]["components"] = [{"label": "Items", "amount": "42.125"}]
        three["receipt"]["tenders"] = [{"label": "Example Account B", "amount": "42.125"}]
        three["allocations"][0]["amount"] = "30.000"
        three["allocations"][1]["amount"] = "12.125"
        result = validate_receipt_proposal(
            three, transaction=three_transaction,
            plan_id="99999999-9999-9999-9999-999999999999",
            decimal_digits=3, resolve_category=category,
        )
        self.assertEqual(result["receipt"]["total_milliunits"], 42125)


if __name__ == "__main__":
    unittest.main()
