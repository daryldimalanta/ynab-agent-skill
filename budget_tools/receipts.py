# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Validate receipt arithmetic and normalize proposals without direct file/HTTP I/O."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Callable


PROPOSAL_VERSION = 1
MEMO_MAX_LENGTH = 500
MAX_AMOUNT = Decimal("9223372036854775.807")


class ReceiptProposalError(ValueError):
    """Raised when a receipt proposal cannot safely drive a YNAB write."""


CategoryResolver = Callable[[str], dict[str, Any]]


def _text(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ReceiptProposalError(f"{field} must be a non-empty string")
    return value.strip() if isinstance(value, str) else None


def _memo(value: Any, field: str, *, required: bool = False) -> str | None:
    result = _text(value, field, required=required)
    if result is not None and len(result) > MEMO_MAX_LENGTH:
        raise ReceiptProposalError(f"{field} exceeds {MEMO_MAX_LENGTH} characters")
    return result


def decimal_to_milliunits(
    value: Any,
    *,
    field: str,
    decimal_digits: int,
    positive: bool = False,
) -> int:
    """Convert exact input without rounding away a discrepancy or excess precision."""
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise ReceiptProposalError(f"{field} must be an exact decimal value")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ReceiptProposalError(f"{field} is not a valid decimal value") from exc
    if not amount.is_finite():
        raise ReceiptProposalError(f"{field} must be finite")
    # copy_abs is exact; built-in abs can round or overflow under Decimal's context.
    if amount.copy_abs() > MAX_AMOUNT:
        raise ReceiptProposalError(f"{field} exceeds the API integer range")
    if positive and amount <= 0:
        raise ReceiptProposalError(f"{field} must be greater than zero")
    exponent = amount.as_tuple().exponent
    supplied_digits = max(0, -exponent)
    if supplied_digits > decimal_digits:
        raise ReceiptProposalError(
            f"{field} has more than the plan's {decimal_digits} decimal places"
        )
    # Integer-ratio arithmetic avoids Decimal context rounding during conversion.
    numerator, denominator = amount.as_integer_ratio()
    milli, remainder = divmod(numerator * 1000, denominator)
    if remainder:
        raise ReceiptProposalError(f"{field} cannot be represented in YNAB milliunits")
    return int(milli)


def _receipt_lines(
    values: Any,
    *,
    field: str,
    decimal_digits: int,
    positive: bool,
) -> list[dict[str, Any]]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ReceiptProposalError(f"{field} must be an array")
    result = []
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            raise ReceiptProposalError(f"{field}[{index}] must be an object")
        label = _text(item.get("label"), f"{field}[{index}].label")
        amount = decimal_to_milliunits(
            item.get("amount"),
            field=f"{field}[{index}].amount",
            decimal_digits=decimal_digits,
            positive=positive,
        )
        result.append({"label": label, "amount_milliunits": amount})
    return result


def validate_receipt_proposal(
    proposal: dict[str, Any],
    *,
    transaction: dict[str, Any],
    plan_id: str,
    decimal_digits: int,
    resolve_category: CategoryResolver,
) -> dict[str, Any]:
    """Validate proposal facts and return resolved IDs and signed milliunit amounts.

    The caller supplies transaction/currency context and a category resolver that
    may perform API reads. Balanced numbers and nonempty evidence do not establish
    correct receipt interpretation, tax attribution, or human authorization.
    """
    if not isinstance(proposal, dict):
        raise ReceiptProposalError("proposal must be a JSON object")
    if type(proposal.get("version")) is not int or proposal.get("version") != PROPOSAL_VERSION:
        raise ReceiptProposalError(f"proposal.version must be {PROPOSAL_VERSION}")
    if type(decimal_digits) is not int or decimal_digits not in {0, 1, 2, 3}:
        raise ReceiptProposalError("plan decimal precision must be between 0 and 3")
    if proposal.get("plan_id") != plan_id:
        raise ReceiptProposalError("proposal plan_id does not match the selected plan")
    if "transaction_id" not in proposal or proposal.get("transaction_id") != transaction.get("id"):
        raise ReceiptProposalError("proposal transaction_id does not match the transaction")

    expected = proposal.get("expected")
    if not isinstance(expected, dict):
        raise ReceiptProposalError("proposal.expected must be an object")
    invariants = {
        "account_id": transaction.get("account_id"),
        "date": transaction.get("date"),
        "amount_milliunits": transaction.get("amount"),
    }
    for field, actual in invariants.items():
        if expected.get(field) != actual:
            raise ReceiptProposalError(f"proposal.expected.{field} does not match YNAB")
    if type(expected.get("amount_milliunits")) is not int:
        raise ReceiptProposalError("proposal.expected.amount_milliunits must be an integer")
    amount = transaction.get("amount")
    if type(amount) is not int or amount == 0:
        raise ReceiptProposalError("transaction amount must be a non-zero milliunit integer")
    absolute_amount = abs(amount)

    receipt = proposal.get("receipt")
    if not isinstance(receipt, dict):
        raise ReceiptProposalError("proposal.receipt must be an object")
    reference = _text(receipt.get("reference"), "receipt.reference")
    match_evidence = _text(receipt.get("match_evidence"), "receipt.match_evidence")
    total = decimal_to_milliunits(
        receipt.get("total"),
        field="receipt.total",
        decimal_digits=decimal_digits,
        positive=True,
    )
    if total != absolute_amount:
        raise ReceiptProposalError("receipt total does not equal the YNAB transaction amount")
    unresolved = receipt.get("unresolved", [])
    if not isinstance(unresolved, list) or any(not isinstance(item, str) for item in unresolved):
        raise ReceiptProposalError("receipt.unresolved must be an array of strings")
    if unresolved:
        raise ReceiptProposalError("receipt has unresolved facts and cannot be applied")

    components = _receipt_lines(
        receipt.get("components"), field="receipt.components",
        decimal_digits=decimal_digits, positive=False,
    )
    if components and sum(item["amount_milliunits"] for item in components) != total:
        raise ReceiptProposalError("receipt components do not reconcile to the receipt total")
    tenders = _receipt_lines(
        receipt.get("tenders"), field="receipt.tenders",
        decimal_digits=decimal_digits, positive=True,
    )
    if len(tenders) > 1:
        raise ReceiptProposalError(
            "Multiple tenders are unsupported until each payment can be bound to its account and allocations"
        )
    if tenders and sum(item["amount_milliunits"] for item in tenders) != total:
        raise ReceiptProposalError("receipt tenders do not reconcile to the receipt total")

    allocations = proposal.get("allocations")
    if not isinstance(allocations, list) or not allocations:
        raise ReceiptProposalError("proposal.allocations must contain at least one allocation")
    normalized_allocations = []
    for index, allocation in enumerate(allocations):
        if not isinstance(allocation, dict):
            raise ReceiptProposalError(f"allocations[{index}] must be an object")
        category_reference = _text(
            allocation.get("category"), f"allocations[{index}].category"
        )
        category = resolve_category(category_reference)
        allocation_amount = decimal_to_milliunits(
            allocation.get("amount"),
            field=f"allocations[{index}].amount",
            decimal_digits=decimal_digits,
            positive=True,
        )
        memo = _memo(allocation.get("memo"), f"allocations[{index}].memo", required=True)
        evidence = _text(allocation.get("evidence"), f"allocations[{index}].evidence")
        # Allocation magnitudes follow the parent's sign, including pure refunds.
        # This model cannot express mixed purchase/return category signs.
        normalized_allocations.append(
            {
                "amount_milliunits": -allocation_amount if amount < 0 else allocation_amount,
                "category_id": category.get("id"),
                "category_group": category.get("group_name"),
                "category_name": category.get("name"),
                "memo": memo,
                "evidence": evidence,
            }
        )
    if sum(abs(item["amount_milliunits"]) for item in normalized_allocations) != total:
        raise ReceiptProposalError("category allocations do not reconcile to the receipt total")

    approve = proposal.get("approve_after_verification", True)
    if not isinstance(approve, bool):
        raise ReceiptProposalError("approve_after_verification must be true or false")
    return {
        "version": PROPOSAL_VERSION,
        "plan_id": plan_id,
        "transaction_id": transaction.get("id"),
        "expected": invariants,
        "receipt": {
            "reference": reference,
            "match_evidence": match_evidence,
            "total_milliunits": total,
            "components": components,
            "tenders": tenders,
            "unresolved": [],
        },
        "parent_memo": _memo(proposal.get("parent_memo"), "parent_memo"),
        "allocations": normalized_allocations,
        "approve_after_verification": approve,
        "decimal_digits": decimal_digits,
    }
