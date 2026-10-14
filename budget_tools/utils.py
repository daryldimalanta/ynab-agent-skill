# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Shared helpers for command modules: output, client creation, and resolvers.

Command modules call ``utils._client()`` and ``utils._json_output()`` through
the module so tests can replace them in one place. This module must not import
``cli`` or ``transactions``; doing so would create an import cycle.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from budget_tools.api import YnabClient
from budget_tools.errors import CliError
from budget_tools.keychain import read_access_token
from budget_tools.rate_limit import RateLimiter
from budget_tools.receipts import MAX_AMOUNT
from budget_tools.state import StateStore


def _json_output(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))

def _client() -> YnabClient:
    token = read_access_token()
    return YnabClient(token, rate_limiter=RateLimiter(StateStore(), token))

def _parse_iso_date(value: str, *, allow_future: bool = False) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CliError(f"Invalid ISO date: {value}") from exc
    if not allow_future and parsed > date.today():
        raise CliError("Future dates require the scheduled-transactions API and are not supported here")
    return parsed.isoformat()

def _milliunits(value: str, *, outflow: bool) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise CliError(f"Invalid currency amount: {value}") from exc
    if not amount.is_finite() or amount < 0:
        raise CliError("Currency amounts must be finite, positive decimal values")
    if amount.copy_abs() > MAX_AMOUNT:
        raise CliError("Currency amount exceeds the supported API integer range")
    # Reject nonzero digits beyond milliunit precision before constructing a ratio.
    # Trailing zeroes remain accepted; very small exponents cannot create huge integers.
    parts = amount.as_tuple()
    excess_digits = max(0, -parts.exponent - 3)
    if excess_digits and any(parts.digits[-excess_digits:]):
        raise CliError("Currency amounts may have at most three decimal places")
    numerator, denominator = amount.as_integer_ratio()
    result, remainder = divmod(numerator * 1000, denominator)
    if remainder:
        raise CliError("Currency amount cannot be represented in YNAB milliunits")
    return -result if outflow else result

def _currency_string(milliunits: Any) -> str | None:
    if not isinstance(milliunits, int):
        return None
    # Formatting reviewed amounts must not depend on the ambient Decimal context.
    whole, fraction = divmod(abs(milliunits), 1000)
    sign = "-" if milliunits < 0 else ""
    if fraction:
        return f"{sign}{whole}.{fraction:03d}".rstrip("0")
    return f"{sign}{whole}"

def _resolve_named_resource(
    records: list[dict[str, Any]],
    reference: str,
    *,
    kind: str,
    active_predicate,
) -> dict[str, Any]:
    for record in records:
        if record.get("id") == reference:
            if not active_predicate(record):
                raise CliError(f"The selected {kind} is not active")
            return record
    matches = [
        record
        for record in records
        if active_predicate(record)
        and isinstance(record.get("name"), str)
        and record["name"].casefold() == reference.casefold()
    ]
    if not matches:
        if kind == "payee":
            raise CliError(
                f"No active payee exactly matched {reference!r}. Ask the user whether to create "
                "this payee. For explicitly requested new receipt transactions, use --new-payee "
                "only after they confirm the exact name, then review the draft before applying. "
                "Do not create a replacement for an existing transaction."
            )
        raise CliError(f"No active {kind} exactly matched {reference!r}")
    if len(matches) > 1:
        raise CliError(f"Multiple {kind} records exactly matched {reference!r}; use an id")
    return matches[0]

def _resolve_account(client: YnabClient, plan_id: str, reference: str) -> dict[str, Any]:
    return _resolve_named_resource(
        client.get_accounts(plan_id),
        reference,
        kind="account",
        active_predicate=lambda item: not item.get("deleted") and not item.get("closed"),
    )

def _category_records(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for group in groups:
        group_name = group.get("name")
        for category in group.get("categories", []):
            item = dict(category)
            item["group_name"] = group_name
            item["group_hidden"] = bool(group.get("hidden"))
            item["group_deleted"] = bool(group.get("deleted"))
            item["group_internal"] = bool(group.get("internal"))
            records.append(item)
    return records

def _resolve_category(client: YnabClient, plan_id: str, reference: str) -> dict[str, Any]:
    records = _category_records(client.get_categories(plan_id))
    def unusable(record):
        return any(record.get(field) for field in (
            "deleted", "hidden", "internal", "group_hidden", "group_deleted", "group_internal"))
    for record in records:
        if record.get("id") == reference:
            if unusable(record):
                raise CliError(f"Category {reference!r} is not active")
            if str(record.get("group_name", "")).casefold() == "credit card payments":
                raise CliError("Credit Card Payment categories cannot be assigned to transactions")
            return record

    if ":" in reference:
        group_name, category_name = [part.strip() for part in reference.split(":", 1)]
        matches = [
            item
            for item in records
            if not unusable(item)
            and str(item.get("group_name", "")).casefold() == group_name.casefold()
            and str(item.get("name", "")).casefold() == category_name.casefold()
        ]
    else:
        matches = [
            item
            for item in records
            if not unusable(item)
            and str(item.get("name", "")).casefold() == reference.casefold()
        ]
    if not matches:
        raise CliError(f"No active category exactly matched {reference!r}")
    if len(matches) > 1:
        raise CliError(
            f"Multiple categories exactly matched {reference!r}; use 'Group: Category' or an id"
        )
    match = matches[0]
    if str(match.get("group_name", "")).casefold() == "credit card payments":
        raise CliError("Credit Card Payment categories cannot be assigned to transactions")
    return match

def _resolve_payee(client: YnabClient, plan_id: str, reference: str) -> dict[str, Any]:
    return _resolve_named_resource(
        client.get_payees(plan_id),
        reference,
        kind="payee",
        active_predicate=lambda item: not item.get("deleted"),
    )

def _require_missing_payee(client: YnabClient, plan_id: str, name: str) -> None:
    """A new-name draft must never silently select an existing or transfer payee."""
    if not name or name != name.strip() or len(name) > 500 or not name.isprintable():
        raise CliError("New payee name must be 1-500 printable characters without surrounding whitespace")
    if name.casefold().startswith("transfer:"):
        raise CliError("Receipt creation does not support transfer payees")
    for payee in client.get_payees(plan_id):
        existing = payee.get("name")
        if isinstance(existing, str) and existing.strip().casefold() == name.casefold():
            raise CliError(
                "A payee with this name already exists (possibly inactive). "
                "Resolve the exact existing non-transfer payee and review a fresh draft using --payee."
            )

def _new_payee_name(draft: dict[str, Any]) -> str | None:
    if draft.get("operation") in {"receipt_create", "update"}:
        return draft["payload"].get("payee_name")
    return None

def _record_created_payee(draft: dict[str, Any], saved: dict[str, Any]) -> None:
    """Pin API-assigned identity as recovery progress; never change reviewed intent."""
    payee_id = saved.get("payee_id")
    if (_new_payee_name(draft) is not None and not draft.get("resolved_payee_id")
            and isinstance(payee_id, str) and payee_id.strip()):
        draft["resolved_payee_id"] = payee_id

def _require_api_ready(store: StateStore) -> None:
    store.require_terms_acknowledged()

def _selected_plan(store: StateStore) -> str:
    _require_api_ready(store)
    return store.configured_plan_id()


def _fingerprint(transaction: dict[str, Any]) -> str:
    """Detect changes to the reviewed target before applying an existing draft."""
    selected = _transaction_invariants(transaction)
    selected["subtransactions"] = _canonical_subtransactions(selected["subtransactions"])
    raw = json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _transaction_invariants(transaction: dict[str, Any]) -> dict[str, Any]:
    return {field: transaction.get(field, False if field == "deleted" else None)
            for field in FINGERPRINT_FIELDS}


def _canonical_subtransactions(value: Any) -> list[tuple[Any, Any, Any, Any]]:
    """Compare active split contents without generated IDs or ordering.

    Keep a sorted list, not a set: identical lines still count separately.
    """
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise CliError("Malformed split data cannot be verified")
    entries = [
        (
            item.get("amount"), item.get("category_id"),
            item.get("payee_id"), item.get("memo"),
        )
        for item in value
        if isinstance(item, dict) and not item.get("deleted")
    ]
    return sorted(entries, key=repr)


FINGERPRINT_FIELDS = (
    "id",
    "account_id",
    "date",
    "amount",
    "payee_id",
    "category_id",
    "memo",
    "cleared",
    "approved",
    "flag_color",
    "subtransactions",
    "deleted",
    "transfer_account_id",
    "transfer_transaction_id",
    "matched_transaction_id",
    "import_id",
)


def _draft_public(draft: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": draft.get("id"),
        "operation": draft.get("operation"),
        "plan_id": draft.get("plan_id"),
        "status": draft.get("status"),
        "stage": draft.get("stage", "drafted"),
        "created_at": draft.get("created_at"),
        "summary": draft.get("summary"),
        "before": draft.get("before_summary"),
        "changes": draft.get("changes"),
        "apply_command": f"ynab-agent-skill drafts apply {draft.get('id')} --confirm {draft.get('id')}",
    }
    if draft.get("workflow") == "purchase_batch":
        result["purchase_review"] = {
            "model": draft.get("proposal", {}).get("model"),
            "remaining_units": draft.get("proposal", {}).get("remaining"),
            "steps": draft.get("payload", {}).get("steps"),
            "adoption": draft.get("proposal", {}).get("recovered"),
            "adoption_evidence": draft.get("proposal", {}).get("adoption_evidence"),
            "warning": "Review exact amounts, dates, categories and memos. Replacement temporarily duplicates estimates until verified deletion.",
        }
        result["progress"] = draft.get("progress", {})
        result["resume_command"] = f"ynab-agent-skill drafts resume {draft.get('id')} --confirm {draft.get('id')}"
        if draft.get("operation") == "purchase_adopt":
            target = draft["proposal"]["recovered"]["pending"]["id"]
            result["apply_command"] += f" --confirm-adopt {target}"
            result["resume_command"] += f" --confirm-adopt {target}"
    if draft.get("workflow") == "deletion_restore":
        result["restoration_review"] = {
            "original_transaction": draft.get("before_detail"),
            "new_transaction_payload": draft.get("payload"),
            "warning": draft.get("summary", {}).get("warning"),
        }
    if draft.get("operation") == "delete":
        result["deletion_review"] = {
            "transaction": draft.get("before_detail"),
            "reason": draft.get("proposal", {}).get("reason"),
            "delete_entire_transaction": True,
            "includes_all_split_rows": True,
            "irreversible": True,
            "protected_states": draft.get("proposal", {}).get("protected_states", []),
            "warning": "Deletes this entire transaction and ALL split rows; can change balances and reconciliation.",
        }
        if draft.get("proposal", {}).get("protected_states"):
            result["apply_command"] += f" --confirm-protected {draft.get('transaction_id')}"
        result["verification"] = draft.get("verification")
    if draft.get("operation") == "budget_assign":
        result["budget_review"] = draft.get("proposal")
        result["verification"] = draft.get("verification")
        result["readback"] = draft.get("readback")
    if draft.get("workflow") == "receipt_split":
        proposal = draft.get("proposal", {})
        result["receipt_review"] = {
            "reference": proposal.get("receipt", {}).get("reference"),
            "match_evidence": proposal.get("receipt", {}).get("match_evidence"),
            "expected": proposal.get("expected"),
            "parent_memo": draft.get("payload", {}).get("memo"),
            "receipt": proposal.get("receipt"),
            "allocations": proposal.get("allocations"),
            "approve_after_verification": proposal.get("approve_after_verification"),
        }
    return result
