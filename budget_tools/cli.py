# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Coordinate reviewed drafts, guarded API writes, and durable recovery.

Command handlers own workflow policy; the API client only sends requests.
See ARCHITECTURE.md for the module map and receipt state transitions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence
from pathlib import Path

from budget_tools import __version__
from budget_tools import planning
from budget_tools.api import ApiError, YnabClient
from budget_tools.keychain import KeychainError, read_access_token
from budget_tools.receipts import MAX_AMOUNT, ReceiptProposalError, validate_receipt_proposal
from budget_tools.rate_limit import RateLimiter
from budget_tools.state import StateError, StateStore, TERMS_VERSION, read_private_json


THIRD_PARTY_NOTICE = (
    "Such OAuth Apps are not officially supported by YNAB in any way. Use of these "
    "OAuth Apps could introduce problems with YNAB, through its official support "
    "channels, will not be able to troubleshoot or fix. Please use at your own risk!"
)

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


class CliError(RuntimeError):
    pass


def _json_output(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _client() -> YnabClient:
    token = read_access_token()
    return YnabClient(token, rate_limiter=RateLimiter(StateStore(), token))


@contextmanager
def _write_guard(client, revalidate, before_send, rate_limited):
    if isinstance(client, YnabClient):
        with client.write_guard(revalidate, before_send, rate_limited):
            yield
    else:
        # Lightweight offline workflow doubles have no transport or rate waits.
        before_send()
        yield


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


def _transaction_summary(transaction: dict[str, Any]) -> dict[str, Any]:
    subtransactions = transaction.get("subtransactions")
    return {
        "id": transaction.get("id"),
        "date": transaction.get("date"),
        "amount_milliunits": transaction.get("amount"),
        "amount_currency": transaction.get("amount_currency")
        if transaction.get("amount_currency") is not None
        else _currency_string(transaction.get("amount")),
        "account_name": transaction.get("account_name"),
        "payee_name": transaction.get("payee_name"),
        "category_name": transaction.get("category_name"),
        "memo": transaction.get("memo"),
        "cleared": transaction.get("cleared"),
        "approved": transaction.get("approved"),
        "flag_name": transaction.get("flag_name"),
        "deleted": transaction.get("deleted", False),
        "subtransaction_count": len(subtransactions) if isinstance(subtransactions, list) else 0,
    }


def _transaction_detail(transaction: dict[str, Any]) -> dict[str, Any]:
    result = _transaction_summary(transaction)
    result.update(
        {
            "account_id": transaction.get("account_id"),
            "payee_id": transaction.get("payee_id"),
            "category_id": transaction.get("category_id"),
            "transfer_account_id": transaction.get("transfer_account_id"),
            "transfer_transaction_id": transaction.get("transfer_transaction_id"),
            "matched_transaction_id": transaction.get("matched_transaction_id"),
            "import_id": transaction.get("import_id"),
            "flag_color": transaction.get("flag_color"),
            "subtransactions": [
                {
                    "id": item.get("id"),
                    "amount_milliunits": item.get("amount"),
                    "amount_currency": item.get("amount_currency")
                    if item.get("amount_currency") is not None
                    else _currency_string(item.get("amount")),
                    "payee_id": item.get("payee_id"),
                    "payee_name": item.get("payee_name"),
                    "category_id": item.get("category_id"),
                    "category_name": item.get("category_name"),
                    "memo": item.get("memo"),
                    "transfer_account_id": item.get("transfer_account_id"),
                    "transfer_transaction_id": item.get("transfer_transaction_id"),
                    "deleted": item.get("deleted", False),
                }
                for item in transaction.get("subtransactions", [])
                if isinstance(item, dict) and not item.get("deleted")
            ],
        }
    )
    return result


def _fingerprint(transaction: dict[str, Any]) -> str:
    """Detect changes to the reviewed target before applying an existing draft."""
    selected = _transaction_invariants(transaction)
    selected["subtransactions"] = _canonical_subtransactions(selected["subtransactions"])
    raw = json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _transaction_invariants(transaction: dict[str, Any]) -> dict[str, Any]:
    return {field: transaction.get(field, False if field == "deleted" else None)
            for field in FINGERPRINT_FIELDS}


def _generic_expected(draft: dict[str, Any]) -> dict[str, Any]:
    """Overlay requested edits on the baseline so unrequested changes also fail."""
    expected = {}
    if draft["operation"] == "update":
        if not isinstance(draft.get("before_invariants"), dict):
            raise CliError("Legacy draft lacks verification invariants; create a fresh draft")
        expected.update(draft["before_invariants"])
    expected.update(draft["payload"])
    new_payee_name = _new_payee_name(draft)
    if new_payee_name is not None:
        expected["payee_name"] = new_payee_name
        expected["payee_id"] = draft.get("resolved_payee_id")
    if draft.get("transaction_id"):
        expected["id"] = draft["transaction_id"]
    expected["deleted"] = False
    return expected


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


def _transaction_query(
    client: YnabClient,
    plan_id: str,
    *,
    since: str,
    until: str | None,
    transaction_type: str | None,
    account_reference: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    account = None
    if account_reference:
        account = _resolve_account(client, plan_id, account_reference)
        transactions = client.get_transactions_by_account(
            plan_id,
            account["id"],
            since_date=since,
            until_date=until,
            transaction_type=transaction_type,
        )
    else:
        transactions = client.get_transactions(
            plan_id,
            since_date=since,
            until_date=until,
            transaction_type=transaction_type,
        )
    return [item for item in transactions if not item.get("deleted")], account


def _validated_receipt_proposal(
    client: YnabClient,
    plan_id: str,
    current: dict[str, Any],
    proposal_path: str,
) -> dict[str, Any]:
    """Read private input and normalize it using live currency/category context.

    This performs API reads through the client and resolver, but no API writes.
    """
    settings = client.get_plan_settings(plan_id)
    currency_format = settings.get("currency_format", {})
    decimal_digits = currency_format.get("decimal_digits")
    if type(decimal_digits) is not int:
        raise CliError("The plan returned an invalid currency precision")
    return validate_receipt_proposal(
        read_private_json(Path(proposal_path)),
        transaction=current,
        plan_id=plan_id,
        decimal_digits=decimal_digits,
        resolve_category=lambda reference: _resolve_category(client, plan_id, reference),
    )


def _receipt_payload(
    proposal: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Build an unapproved split; approval requires a separate verified write."""
    parent_memo = proposal.get("parent_memo")
    # None means preserve the existing memo; an explicit empty string clears it.
    return {
        "category_id": None,
        "memo": current.get("memo") if parent_memo is None else parent_memo,
        "approved": False,
        "subtransactions": [
            {
                "amount": item["amount_milliunits"],
                "category_id": item["category_id"],
                "memo": item["memo"],
            }
            for item in proposal["allocations"]
        ],
    }


def _receipt_draft_shape(
    *,
    draft_id: str,
    operation: str,
    plan_id: str,
    current: dict[str, Any],
    proposal: dict[str, Any],
    payload: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    """Capture the reviewed intent and baseline before StateStore seals them."""
    return {
        "id": draft_id,
        "operation": operation,
        "workflow": "receipt_split",
        "status": "pending",
        "stage": "drafted",
        "created_at": created_at,
        "plan_id": plan_id,
        "transaction_id": current["id"],
        "before_fingerprint": _fingerprint(current),
        "before_summary": _transaction_summary(current),
        "before_detail": _transaction_detail(current),
        "before_invariants": _transaction_invariants(current),
        "payload": payload,
        "proposal": proposal,
        "changes": {
            "parent_memo": payload["memo"],
            "allocations": proposal["allocations"],
            "approve_after_verification": proposal["approve_after_verification"],
        },
        "summary": {
            "transaction_id": current["id"],
            "date": current.get("date"),
            "amount_currency": _currency_string(current.get("amount")),
            "payee": current.get("payee_name"),
            "allocation_count": len(proposal["allocations"]),
        },
    }


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


def _extract_saved_transaction(response: dict[str, Any]) -> dict[str, Any] | None:
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    transaction = data.get("transaction")
    if isinstance(transaction, dict):
        return transaction
    transactions = data.get("transactions")
    if isinstance(transactions, list) and len(transactions) == 1 and isinstance(transactions[0], dict):
        return transactions[0]
    ids = data.get("transaction_ids")
    if isinstance(ids, list) and len(ids) == 1 and isinstance(ids[0], str) and ids[0]:
        return {"id": ids[0]}
    return None


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


def cmd_terms(args: argparse.Namespace, store: StateStore) -> None:
    if args.terms_command == "status":
        config = store.load_config()
        _json_output(
            {
                "acknowledged": config.get("terms_acknowledged_version") == TERMS_VERSION,
                "terms_version": TERMS_VERSION,
            }
        )
        return

    print(THIRD_PARTY_NOTICE)
    print()
    if not sys.stdin.isatty():
        raise CliError("Acknowledgment must be completed interactively in a terminal")
    response = input("Type I ACKNOWLEDGE to continue: ")
    if response != "I ACKNOWLEDGE":
        raise CliError("The notice was not acknowledged")
    config = store.load_config()
    config["terms_acknowledged_version"] = TERMS_VERSION
    config["terms_acknowledged_at"] = store.now()
    store.save_config(config)
    _json_output({"acknowledged": True, "terms_version": TERMS_VERSION})


def cmd_auth(args: argparse.Namespace, store: StateStore) -> None:
    _require_api_ready(store)
    _client().get_user()
    _json_output({"authenticated": True})


def cmd_plans(args: argparse.Namespace, store: StateStore) -> None:
    _require_api_ready(store)
    client = _client()
    plans = client.get_plans()
    if args.plans_command == "list":
        config = store.load_config()
        selected = config.get("plan_id")
        _json_output(
            [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "selected": item.get("id") == selected,
                    "last_modified_on": item.get("last_modified_on"),
                }
                for item in plans
            ]
        )
        return

    reference = args.plan
    plan = _resolve_named_resource(
        plans,
        reference,
        kind="plan",
        active_predicate=lambda item: True,
    )
    config = store.load_config()
    config["plan_id"] = plan["id"]
    config["plan_name"] = plan.get("name")
    config["plan_selected_at"] = store.now()
    store.save_config(config)
    _json_output({"selected": {"id": plan.get("id"), "name": plan.get("name")}})


def cmd_accounts(args: argparse.Namespace, store: StateStore) -> None:
    plan_id = _selected_plan(store)
    accounts = _client().get_accounts(plan_id)
    _json_output(
        [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "type": item.get("type"),
                "closed": item.get("closed"),
                "deleted": item.get("deleted"),
                "balance_currency": item.get("balance_currency")
                if item.get("balance_currency") is not None
                else _currency_string(item.get("balance")),
            }
            for item in accounts
            if not item.get("deleted")
        ]
    )


def cmd_categories(args: argparse.Namespace, store: StateStore) -> None:
    plan_id = _selected_plan(store)
    groups = _client().get_categories(plan_id)
    _json_output(
        [
            {
                "id": item.get("id"),
                "group": item.get("group_name"),
                "name": item.get("name"),
                "hidden": item.get("hidden"),
                "deleted": item.get("deleted"),
            }
            for item in _category_records(groups)
            if not item.get("deleted")
        ]
    )


def cmd_budgets(args: argparse.Namespace, store: StateStore) -> None:
    plan_id = _selected_plan(store)
    if args.budgets_command == "months":
        client = _client()
        months = client.get_months(plan_id)
        _json_output({"plan_id": plan_id, "currency_format": client.get_plan_settings(plan_id)["currency_format"],
                      "months": [planning.month_summary(m) for m in months
                                                    if not m.get("deleted")]})
        return
    month = planning.month_key(args.month)
    if args.budgets_command == "get":
        _json_output(planning.read_budget(_client(), plan_id, month, args.category))
        return
    if args.assigned is not None:
        negative = args.assigned.startswith("-")
        amount = _milliunits(args.assigned[1:] if negative else args.assigned, outflow=negative)
        relative = False
    else:
        amount = _milliunits(args.increase if args.increase is not None else args.decrease,
                            outflow=args.decrease is not None)
        relative = True
    draft = planning.create_draft(_client(), store, month, args.category, amount, relative=relative)
    _json_output(_draft_public(draft))


def cmd_payees(args: argparse.Namespace, store: StateStore) -> None:
    plan_id = _selected_plan(store)
    payees = _client().get_payees(plan_id)
    _json_output(
        [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "transfer_account_id": item.get("transfer_account_id"),
            }
            for item in payees
            if not item.get("deleted")
        ]
    )


def _transaction_changes(args: argparse.Namespace, client: YnabClient, plan_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    changes: dict[str, Any] = {}
    labels: dict[str, Any] = {}
    if getattr(args, "account", None):
        account = _resolve_account(client, plan_id, args.account)
        changes["account_id"] = account["id"]
        labels["account"] = account.get("name")
    if getattr(args, "date", None):
        changes["date"] = _parse_iso_date(args.date)
        labels["date"] = changes["date"]
    if getattr(args, "outflow", None) is not None:
        changes["amount"] = _milliunits(args.outflow, outflow=True)
        labels["amount_currency"] = _currency_string(changes["amount"])
    if getattr(args, "inflow", None) is not None:
        changes["amount"] = _milliunits(args.inflow, outflow=False)
        labels["amount_currency"] = _currency_string(changes["amount"])
    if getattr(args, "payee", None):
        payee = _resolve_payee(client, plan_id, args.payee)
        changes["payee_id"] = payee["id"]
        labels["payee"] = payee.get("name")
    if getattr(args, "new_payee", None):
        _require_missing_payee(client, plan_id, args.new_payee)
        # YNAB resolves or atomically creates a non-transfer payee when a
        # transaction update supplies its name without a payee ID.
        changes["payee_id"] = None
        changes["payee_name"] = args.new_payee
        labels["payee"] = args.new_payee
        labels["new_payee"] = {"name": args.new_payee, "create_if_missing": True}
    if getattr(args, "clear_payee", False):
        changes["payee_id"] = None
        labels["payee"] = None
    if getattr(args, "category", None):
        category = _resolve_category(client, plan_id, args.category)
        changes["category_id"] = category["id"]
        labels["category"] = f"{category.get('group_name')}: {category.get('name')}"
    if getattr(args, "uncategorized", False):
        changes["category_id"] = None
        labels["category"] = None
    if getattr(args, "memo", None) is not None:
        if len(args.memo) > 500:
            raise CliError("Transaction memo exceeds 500 characters")
        changes["memo"] = args.memo
        labels["memo"] = args.memo
    if getattr(args, "clear_memo", False):
        changes["memo"] = None
        labels["memo"] = None
    if getattr(args, "cleared", None):
        changes["cleared"] = args.cleared
        labels["cleared"] = args.cleared
    if getattr(args, "approved", None) is not None:
        changes["approved"] = args.approved
        labels["approved"] = args.approved
    if getattr(args, "flag", None) is not None:
        changes["flag_color"] = None if args.flag == "none" else args.flag
        labels["flag"] = args.flag
    return changes, labels


def cmd_transactions(args: argparse.Namespace, store: StateStore) -> None:
    plan_id = _selected_plan(store)
    client = _client()
    command = args.transactions_command
    if command in {"list", "match"}:
        since = _parse_iso_date(args.since, allow_future=True)
        until = _parse_iso_date(args.until, allow_future=True) if args.until else None
        if until and until < since:
            raise CliError("--until must be on or after --since")
        transactions, account = _transaction_query(
            client, plan_id, since=since, until=until,
            transaction_type=getattr(args, "type", None),
            account_reference=getattr(args, "account", None),
        )
        requested_amount = None
        if getattr(args, "outflow", None) is not None:
            requested_amount = _milliunits(args.outflow, outflow=True)
        elif getattr(args, "inflow", None) is not None:
            requested_amount = _milliunits(args.inflow, outflow=False)
        if requested_amount is not None:
            transactions = [item for item in transactions if item.get("amount") == requested_amount]
        # List responses already contain split lines; reuse the single-record
        # formatter without an additional request for every transaction.
        formatter = _transaction_detail if command == "list" and args.include_splits else _transaction_summary
        summaries = [formatter(item) for item in transactions]
        limit = args.limit
        if limit is not None and limit < 1:
            raise CliError("--limit must be at least 1")
        displayed = summaries[:limit] if limit is not None else summaries
        if command == "match":
            # A display limit must not turn several candidates into a unique match.
            count = len(summaries)
            _json_output(
                {
                    "match_status": "unique" if count == 1 else "not_found" if count == 0 else "ambiguous",
                    "candidate_count": count,
                    "truncated": limit is not None and count > limit,
                    "criteria": {
                        "account_id": account.get("id") if account else None,
                        "account_name": account.get("name") if account else None,
                        "amount_milliunits": requested_amount,
                        "since": since,
                        "until": until,
                    },
                    "candidates": displayed,
                }
            )
        else:
            _json_output(displayed)
        return
    if command == "get":
        _json_output(_transaction_detail(client.get_transaction(plan_id, args.transaction_id)))
        return

    if command == "draft-receipt-create":
        account = _resolve_account(client, plan_id, args.account)
        if account.get("on_budget") is False:
            raise CliError("Receipt split creation requires an on-budget account")
        if args.new_payee is not None:
            _require_missing_payee(client, plan_id, args.new_payee)
            payee = {"id": None, "name": args.new_payee}
        else:
            payee = _resolve_payee(client, plan_id, args.payee)
        if payee.get("transfer_account_id"):
            raise CliError("Receipt split creation does not support transfer payees")
        current = {
            "id": None, "account_id": account["id"], "account_name": account.get("name"),
            "date": _parse_iso_date(args.date),
            "amount": _milliunits(args.outflow if args.outflow is not None else args.inflow,
                                  outflow=args.outflow is not None),
            "payee_id": payee["id"], "payee_name": payee.get("name"),
            "cleared": args.cleared, "approved": False, "deleted": False,
            "category_id": None, "memo": None, "subtransactions": [],
        }
        proposal = _validated_receipt_proposal(client, plan_id, current, args.proposal)
        payload = _receipt_payload(proposal, current)
        payload.update({field: current[field] for field in
                        ("account_id", "date", "amount", "payee_id", "cleared")})
        if args.new_payee is not None:
            payload["payee_name"] = args.new_payee
        draft = _receipt_draft_shape(
            draft_id=str(uuid.uuid4()), operation="receipt_create", plan_id=plan_id,
            current=current, proposal=proposal, payload=payload, created_at=store.now())
        draft["before_summary"] = None
        draft["summary"]["account"] = account.get("name")
        draft["summary"]["creates_new_transaction"] = True
        if args.new_payee is not None:
            draft["summary"]["creates_new_payee"] = True
            draft["changes"]["new_payee"] = {"name": args.new_payee, "create_if_missing": True}
        store.save_draft(draft)
        _json_output(_draft_public(draft))
        return

    if command in {"assess-receipt", "draft-receipt-split", "draft-receipt-approval"}:
        current = client.get_transaction(plan_id, args.transaction_id)
        if current.get("deleted"):
            raise CliError("The transaction is deleted")
        if current.get("transfer_account_id") or current.get("transfer_transaction_id"):
            raise CliError("Receipt categorization does not support transfer transactions")
        proposal = _validated_receipt_proposal(client, plan_id, current, args.proposal)
        payload = _receipt_payload(proposal, current)
        probe = _receipt_draft_shape(
            draft_id="assessment", operation="receipt_split", plan_id=plan_id,
            current=current, proposal=proposal, payload=payload, created_at=store.now(),
        )
        existing_split = bool(current.get("subtransactions"))
        split_check = _receipt_verification(probe, current, approved=None) if existing_split else None

        if command == "assess-receipt":
            if current.get("approved"):
                route = "already_approved"
            elif not existing_split:
                route = "draft_split_available"
            elif split_check and split_check["verified"]:
                route = "existing_split_matches"
            else:
                route = "manual_split_correction_required"
            _json_output(
                {
                    "route": route,
                    "transaction": _transaction_detail(current),
                    "proposal": {
                        "receipt": proposal["receipt"],
                        "parent_memo": proposal["parent_memo"],
                        "allocations": proposal["allocations"],
                        "approve_after_verification": proposal["approve_after_verification"],
                    },
                    "verification": split_check,
                    "next_command": (
                        "transactions draft-receipt-split" if route == "draft_split_available"
                        else "transactions draft-receipt-approval" if route == "existing_split_matches"
                        and proposal["approve_after_verification"] else None
                    ),
                }
            )
            return

        if command == "draft-receipt-split":
            if existing_split:
                raise CliError(
                    "Existing split lines cannot be changed through the YNAB API; run assess-receipt"
                )
            if current.get("approved"):
                raise CliError("Receipt split drafting requires an unapproved transaction")
            operation = "receipt_split"
        else:
            if not existing_split or not split_check or not split_check["verified"]:
                raise CliError("The existing split does not exactly match the receipt proposal")
            if current.get("approved"):
                raise CliError("The matching transaction is already approved")
            if not proposal["approve_after_verification"]:
                raise CliError("The proposal does not request approval")
            operation = "receipt_approval"

        draft_id = str(uuid.uuid4())
        draft = _receipt_draft_shape(
            draft_id=draft_id, operation=operation, plan_id=plan_id,
            current=current, proposal=proposal, payload=payload, created_at=store.now(),
        )
        store.save_draft(draft)
        _json_output(_draft_public(draft))
        return

    if command == "draft-create":
        changes, labels = _transaction_changes(args, client, plan_id)
        required = {"account_id", "date", "amount"}
        missing = sorted(required.difference(changes))
        if missing:
            raise CliError(f"Create draft is missing required fields: {', '.join(missing)}")
        draft_id = str(uuid.uuid4())
        draft = {
            "id": draft_id,
            "operation": "create",
            "status": "pending",
            "stage": "drafted",
            "created_at": store.now(),
            "plan_id": plan_id,
            "payload": changes,
            "changes": labels,
            "summary": {
                "date": changes["date"],
                "amount_currency": _currency_string(changes["amount"]),
                "account": labels.get("account"),
                "payee": labels.get("payee"),
                "category": labels.get("category"),
            },
        }
        store.save_draft(draft)
        _json_output(_draft_public(draft))
        return

    if command == "draft-update":
        current = client.get_transaction(plan_id, args.transaction_id)
        changes, labels = _transaction_changes(args, client, plan_id)
        if not changes:
            raise CliError("At least one transaction change is required")
        if changes.get("approved") is True and len(changes) > 1:
            raise CliError("Approval must be drafted separately from other transaction changes")
        if current.get("subtransactions") and ({"date", "amount"} & set(changes)):
            raise CliError("Date and amount changes are blocked for existing split transactions")
        draft_id = str(uuid.uuid4())
        draft = {
            "id": draft_id,
            "operation": "update",
            "status": "pending",
            "stage": "drafted",
            "created_at": store.now(),
            "plan_id": plan_id,
            "transaction_id": current["id"],
            "before_fingerprint": _fingerprint(current),
            "before_summary": _transaction_summary(current),
            "before_invariants": _transaction_invariants(current),
            "payload": changes,
            "changes": labels,
            "summary": {
                "transaction_id": current["id"],
                "date": current.get("date"),
                "amount_currency": _currency_string(current.get("amount")),
                "payee": current.get("payee_name"),
            },
        }
        if "payee_name" in changes:
            draft["summary"]["creates_new_payee"] = True
        store.save_draft(draft)
        _json_output(_draft_public(draft))
        return
    raise CliError("Unknown transactions command")


def _verification(payload: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    mismatches = {}
    for field, expected in payload.items():
        actual = saved.get(field)
        if field == "subtransactions":
            expected_value = _canonical_subtransactions(expected)
            actual_value = _canonical_subtransactions(actual)
        else:
            expected_value = expected
            actual_value = actual
        if actual_value != expected_value:
            mismatches[field] = {"expected": expected, "actual": actual}
    return {"verified": not mismatches, "mismatches": mismatches}


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


def _finish_verification(
    draft: dict[str, Any], saved: dict[str, Any], store: StateStore
) -> dict[str, Any]:
    _record_created_payee(draft, saved)
    check = _verification(_generic_expected(draft), saved)
    draft["status"] = "applied" if check["verified"] else "applied_with_warning"
    draft["stage"] = "verified" if check["verified"] else "verification_warning"
    draft["apply_finished_at"] = store.now()
    draft["transaction_id"] = saved.get("id") or draft.get("transaction_id")
    draft["after_summary"] = _transaction_summary(saved)
    draft["verification"] = check
    store.save_draft(draft)
    store.append_audit(
        {
            "at": store.now(),
            "draft_id": draft["id"],
            "operation": draft["operation"],
            "transaction_id": draft.get("transaction_id"),
            "before": draft.get("before_summary"),
            "after": _transaction_summary(saved),
            "verified": check["verified"],
        }
    )
    return {
        "draft_id": draft["id"],
        "status": draft["status"],
        "stage": draft["stage"],
        "transaction": _transaction_summary(saved),
        "verification": check,
    }


def _receipt_verification(
    draft: dict[str, Any], saved: dict[str, Any], *, approved: bool | None
) -> dict[str, Any]:
    """Compare receipt intent plus preserved fields against a saved transaction.

    approved=None checks categorization regardless of approval state, allowing
    reconciliation to discover an approval that succeeded before a response was lost.
    """
    if not isinstance(draft.get("before_invariants"), dict):
        raise CliError("Legacy receipt draft lacks verification invariants; create a fresh draft")
    expected = dict(draft["before_invariants"])
    expected.update({
        "id": draft["transaction_id"],
        "account_id": draft["proposal"]["expected"]["account_id"],
        "date": draft["proposal"]["expected"]["date"],
        "amount": draft["proposal"]["expected"]["amount_milliunits"],
        "category_id": None,
        "memo": draft["payload"].get("memo"),
        "subtransactions": draft["payload"]["subtransactions"],
        "deleted": False,
    })
    new_payee_name = _new_payee_name(draft)
    if new_payee_name is not None:
        expected["payee_name"] = new_payee_name
        # Full responses pin the ID before readback. ID-only acknowledgments
        # learn it from the first verified read of the known transaction.
        expected["payee_id"] = draft.get("resolved_payee_id") or saved.get("payee_id")
    # A null category in the write payload requests a split, but YNAB can
    # return its plan-specific "Split" category ID on the saved parent.
    # Accept only that representation; every child category, amount, payee,
    # memo and duplicate-line count is still compared by _verification.
    if (saved.get("category_name") == "Split"
            and isinstance(saved.get("category_id"), str) and saved["category_id"]
            and isinstance(saved.get("subtransactions"), list) and saved["subtransactions"]):
        expected["category_id"] = saved["category_id"]
    if approved is not None:
        expected["approved"] = approved
    else:
        expected.pop("approved", None)
    check = _verification(expected, saved)
    if new_payee_name is not None:
        payee_id = saved.get("payee_id")
        if not isinstance(payee_id, str) or not payee_id.strip():
            check["mismatches"]["payee_id"] = {
                "expected": "A nonempty API-assigned payee ID", "actual": payee_id,
            }
            check["verified"] = False
    return check


def _receipt_result(draft: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    return {
        "draft_id": draft["id"],
        "status": draft["status"],
        "stage": draft["stage"],
        "transaction": _transaction_detail(saved),
        "categorization_verification": draft.get("categorization_verification"),
        "approval_verification": draft.get("approval_verification"),
    }


def _audit_receipt(draft: dict[str, Any], saved: dict[str, Any], store: StateStore) -> None:
    store.append_audit(
        {
            "at": store.now(),
            "draft_id": draft["id"],
            "operation": draft["operation"],
            "transaction_id": draft.get("transaction_id"),
            "before": draft.get("before_summary"),
            "after": _transaction_summary(saved),
            "stage": draft.get("stage"),
            "verified": draft.get("stage") in {"verified_categorization", "verified_approval"},
        }
    )


def _finalize_receipt(
    draft: dict[str, Any], saved: dict[str, Any], store: StateStore, *, approved: bool
) -> dict[str, Any]:
    """Persist readback evidence and a private audit record; perform no API write."""
    check = _receipt_verification(draft, saved, approved=approved)
    if approved:
        draft["approval_verification"] = check
    else:
        draft["categorization_verification"] = check
    draft["after_summary"] = _transaction_summary(saved)
    draft["after_detail"] = _transaction_detail(saved)
    draft["apply_finished_at"] = store.now()
    if check["verified"]:
        draft["status"] = "applied"
        draft["stage"] = "verified_approval" if approved else "verified_categorization"
    else:
        draft["status"] = "applied_with_warning"
        draft["stage"] = "approval_verification_failed" if approved else "categorization_verification_failed"
    store.save_draft(draft)
    _audit_receipt(draft, saved, store)
    return _receipt_result(draft, saved)


def _apply_receipt_approval_locked(
    draft: dict[str, Any], store: StateStore, client: YnabClient
) -> dict[str, Any]:
    """Send only remaining approval under the caller's workflow locks.

    The caller must establish draft authorization. Recheck age and current
    categorization here because both initial apply and resume enter this path.
    """
    _check_draft_age(draft)
    current = client.get_transaction(draft["plan_id"], draft["transaction_id"])
    categorization = _receipt_verification(draft, current, approved=None)
    if not categorization["verified"]:
        draft["status"] = "approval_blocked"
        draft["stage"] = "categorization_changed_before_approval"
        draft["categorization_verification"] = categorization
        store.save_draft(draft)
        raise CliError("The verified categorization changed before approval")
    if current.get("approved") is True:
        return _finalize_receipt(draft, current, store, approved=True)

    draft["categorization_verification"] = categorization

    def revalidate():
        _check_draft_age(draft)
        fresh = client.get_transaction(draft["plan_id"], draft["transaction_id"])
        if not _receipt_verification(draft, fresh, approved=False)["verified"]:
            raise CliError("Transaction changed during rate-limit wait; reconcile before approval")

    def before_send():
        _check_draft_age(draft)
        draft["status"] = "applying"
        draft["stage"] = "approval_started"
        draft["approval_started_at"] = store.now()
        store.save_draft(draft)

    def rate_limited():
        draft["status"] = "approval_pending"
        draft["stage"] = "approval_rate_limited"
        store.save_draft(draft)

    try:
        with _write_guard(client, revalidate, before_send, rate_limited):
            response = client.update_transaction(
                draft["plan_id"], draft["transaction_id"], {"approved": True}
            )
    except ApiError as exc:
        rejected = exc.status is not None and 400 <= exc.status < 500
        draft["status"] = "approval_failed" if rejected else "approval_uncertain"
        draft["stage"] = "approval_rejected" if rejected else "approval_uncertain"
        draft["approval_error"] = {
            "status": exc.status, "name": exc.error_name, "detail": exc.detail,
        }
        store.save_draft(draft)
        raise

    draft["status"] = "approval_verification_pending"
    draft["stage"] = "approval_acknowledged"
    draft["approval_acknowledged_at"] = store.now()
    store.save_draft(draft)
    saved = _extract_saved_transaction(response)
    try:
        saved = client.get_transaction(draft["plan_id"], draft["transaction_id"])
    except ApiError:
        draft["stage"] = "approval_verification_pending"
        store.save_draft(draft)
        raise
    return _finalize_receipt(draft, saved, store, approved=True)


def _finish_receipt_categorization(
    draft: dict[str, Any], saved: dict[str, Any], store: StateStore, client: YnabClient
) -> dict[str, Any]:
    """Verify the unapproved write before optionally entering separate approval.

    A mismatch records a warning and returns without approval. The caller keeps
    the workflow locks held across verification and the optional approval step.
    """
    check = _receipt_verification(draft, saved, approved=False)
    draft["categorization_verification"] = check
    draft["after_summary"] = _transaction_summary(saved)
    draft["after_detail"] = _transaction_detail(saved)
    if not check["verified"]:
        draft["status"] = "applied_with_warning"
        draft["stage"] = "categorization_verification_failed"
        draft["apply_finished_at"] = store.now()
        store.save_draft(draft)
        _audit_receipt(draft, saved, store)
        return _receipt_result(draft, saved)
    _record_created_payee(draft, saved)
    draft["status"] = "approval_pending" if draft["proposal"]["approve_after_verification"] else "applied"
    draft["stage"] = "categorization_verified" if draft["proposal"]["approve_after_verification"] else "verified_categorization"
    draft["categorization_verified_at"] = store.now()
    store.save_draft(draft)
    if not draft["proposal"]["approve_after_verification"]:
        _audit_receipt(draft, saved, store)
        return _receipt_result(draft, saved)
    return _apply_receipt_approval_locked(draft, store, client)


def _check_draft_age(draft: dict[str, Any]) -> None:
    """Bound write authorization in time; read-only reconciliation stays available."""
    created_at = datetime.fromisoformat(draft["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created_at > timedelta(hours=24):
        raise CliError("Draft is older than 24 hours; create a fresh draft")
    if created_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise CliError("Draft creation time is in the future; create a fresh draft")


def _apply_draft_locked(draft: dict[str, Any], store: StateStore) -> dict[str, Any]:
    """Attempt a pending draft once and persist enough progress for recovery.

    cmd_drafts must validate the seal, confirmation, and pending status and hold
    the draft lock plus the transaction lock when a target ID is known.
    """
    _check_draft_age(draft)
    client = _client()
    plan_id = draft["plan_id"]

    def revalidate():
        _check_draft_age(draft)
        new_payee_name = _new_payee_name(draft)
        if new_payee_name is not None:
            _require_missing_payee(client, plan_id, new_payee_name)
        if draft["operation"] in {"update", "receipt_split", "receipt_approval"}:
            if not isinstance(draft.get("before_invariants"), dict):
                raise CliError("Legacy draft lacks verification invariants; create a fresh draft")
            before = client.get_transaction(plan_id, draft["transaction_id"])
            if before.get("deleted"):
                raise CliError("The transaction is deleted")
            if _fingerprint(before) != draft["before_fingerprint"]:
                raise CliError("The transaction changed after this draft was created; create a fresh draft")
            if draft["operation"] == "receipt_split" and before.get("subtransactions"):
                raise CliError("The transaction became a split after this draft was created")

    revalidate()
    if draft["operation"] == "receipt_approval":
        return _apply_receipt_approval_locked(draft, store, client)

    # Save before the request so interrupted writes cannot remain eligible for apply.
    # If persistence fails, execution stops before any transaction write is sent.
    def before_send():
        _check_draft_age(draft)
        draft["status"] = "applying"
        draft["stage"] = "write_started"
        draft["apply_started_at"] = store.now()
        store.save_draft(draft)

    def rate_limited():
        draft["status"] = "pending"
        draft["stage"] = "write_rate_limited"
        store.save_draft(draft)

    try:
        with _write_guard(client, revalidate, before_send, rate_limited):
            if draft["operation"] in {"create", "receipt_create"}:
                response = client.create_transaction(plan_id, draft["payload"])
            elif draft["operation"] in {"update", "receipt_split"}:
                response = client.update_transaction(
                    plan_id, draft["transaction_id"], draft["payload"]
                )
            else:
                raise CliError("Unsupported draft operation")
    except ApiError as exc:
        rejected = exc.status is not None and 400 <= exc.status < 500
        # A timeout, invalid response, or server failure can follow a committed write.
        # Record uncertainty instead of making the draft pending for an automatic retry.
        draft["status"] = "failed" if rejected else "uncertain"
        draft["stage"] = "write_rejected" if rejected else "write_uncertain"
        draft["apply_finished_at"] = store.now()
        draft["error"] = {
            "status": exc.status,
            "name": exc.error_name,
            "detail": exc.detail,
        }
        store.save_draft(draft)
        raise

    saved = _extract_saved_transaction(response)
    if saved is None:
        if draft["operation"] in {"update", "receipt_split"}:
            draft["status"] = "verification_pending"
            draft["stage"] = "write_acknowledged"
        else:
            draft["status"] = "uncertain"
            draft["stage"] = "write_acknowledged_without_id"
        draft["apply_finished_at"] = store.now()
        store.save_draft(draft)
        raise CliError("The API accepted the write but did not return one transaction for verification")

    transaction_id = saved.get("id") or draft.get("transaction_id")
    if draft.get("transaction_id") and transaction_id != draft["transaction_id"]:
        draft["status"] = "uncertain"
        draft["stage"] = "write_acknowledged_wrong_id"
        store.save_draft(draft)
        raise CliError("The write returned a different transaction id; reconcile the original transaction")
    if not isinstance(transaction_id, str):
        draft["status"] = "uncertain"
        draft["stage"] = "write_acknowledged_without_id"
        draft["apply_finished_at"] = store.now()
        store.save_draft(draft)
        raise CliError("The API accepted the write but did not return a transaction id")

    # Persist the acknowledged resource before a readback that may fail. Recovery
    # needs this exact ID; recreating from matching date/amount could duplicate a write.
    draft["transaction_id"] = transaction_id
    _record_created_payee(draft, saved)
    draft["status"] = "verification_pending"
    draft["stage"] = "write_acknowledged"
    draft["write_acknowledged_at"] = store.now()
    store.save_draft(draft)
    try:
        saved = client.get_transaction(plan_id, transaction_id)
    except ApiError:
        draft["stage"] = "verification_pending"
        draft["apply_finished_at"] = store.now()
        store.save_draft(draft)
        raise
    if draft["operation"] in {"receipt_split", "receipt_create"}:
        return _finish_receipt_categorization(draft, saved, store, client)
    return _finish_verification(draft, saved, store)


def _reconcile_draft_locked(draft: dict[str, Any], store: StateStore) -> dict[str, Any]:
    """Read the known target and update local recovery evidence without API writes.

    The caller holds the workflow locks. This may save drafts/audit records and
    establish approval_pending, but never recreates, recategorizes, or approves.
    """
    if draft.get("status") == "pending":
        raise CliError("This draft has not been applied; review and apply it before reconciliation")
    transaction_id = draft.get("transaction_id")
    if not isinstance(transaction_id, str):
        raise CliError(
            "This draft has no known transaction id; reconcile it using independent evidence"
        )
    client = _client()
    saved = client.get_transaction(draft["plan_id"], transaction_id)
    if draft.get("operation") in {"receipt_split", "receipt_approval", "receipt_create"}:
        categorization = _receipt_verification(draft, saved, approved=None)
        draft["categorization_verification"] = categorization
        if not categorization["verified"]:
            draft["last_reconciled_at"] = store.now()
            store.save_draft(draft)
            raise CliError("The current transaction does not match the receipt categorization")
        _record_created_payee(draft, saved)
        if draft["proposal"]["approve_after_verification"] and not saved.get("approved"):
            draft["status"] = "approval_pending"
            draft["stage"] = "categorization_verified"
            draft["last_reconciled_at"] = store.now()
            store.save_draft(draft)
            return _receipt_result(draft, saved)
        return _finalize_receipt(
            draft, saved, store,
            approved=bool(draft["proposal"]["approve_after_verification"]),
        )
    check = _verification(_generic_expected(draft), saved)
    if draft.get("stage") == "write_uncertain" and not check["verified"]:
        draft["last_reconciled_at"] = store.now()
        draft["reconciliation"] = check
        store.save_draft(draft)
        raise CliError("The current transaction does not prove that the uncertain write succeeded")
    return _finish_verification(draft, saved, store)


def cmd_drafts(args: argparse.Namespace, store: StateStore) -> None:
    """Own authorization checks and lock scopes for apply, reconcile, and resume."""
    command = args.drafts_command
    if command == "list":
        _json_output(store.list_drafts())
        return
    draft = store.load_draft(args.draft_id)
    if command == "show":
        _json_output(_draft_public(draft))
        return

    if draft.get("operation") == "budget_assign":
        if command == "resume":
            raise CliError("Budget drafts cannot be resumed; reconcile an attempted assignment without replaying it")
        _json_output(planning.execute(store, args.draft_id, client_factory=_client,
                                     confirm=getattr(args, "confirm", None), reconcile=command == "reconcile"))
        return

    _require_api_ready(store)
    # Always lock draft first, then a known transaction. Reload under the lock so
    # another caller's completed attempt cannot be replayed from our earlier read.
    with store.exclusive_lock(f"draft:{args.draft_id}"):
        draft = store.load_draft(args.draft_id)
        transaction_scope = None
        # Different drafts can target one record. A new creation has no target ID
        # yet; neither lock can coordinate with the YNAB UI or other API clients.
        if draft.get("operation") in {"update", "receipt_split", "receipt_approval", "receipt_create"} and draft.get("transaction_id"):
            transaction_scope = f"transaction:{draft.get('plan_id')}:{draft.get('transaction_id')}"

        def run_locked() -> dict[str, Any]:
            if command == "reconcile":
                return _reconcile_draft_locked(draft, store)
            if command == "resume":
                if draft.get("operation") not in {"receipt_split", "receipt_approval", "receipt_create"}:
                    raise CliError("Only receipt drafts have a resumable approval stage")
                legacy_rate_rejection = (
                    draft.get("status") == "approval_failed"
                    and draft.get("stage") == "approval_rejected"
                    and isinstance(draft.get("approval_error"), dict)
                    and draft.get("approval_error", {}).get("status") == 429
                )
                if draft.get("status") != "approval_pending" and not legacy_rate_rejection:
                    raise CliError("The receipt draft is not waiting for approval")
                if args.confirm != draft["id"]:
                    raise CliError("--confirm must exactly match the draft id")
                # Resume is approval-only; it must never reenter the original write.
                return _apply_receipt_approval_locked(draft, store, _client())
            if args.confirm != draft["id"]:
                raise CliError("--confirm must exactly match the draft id")
            legacy_rate_rejection = (
                draft.get("status") == "failed"
                and draft.get("stage") == "write_rejected"
                and isinstance(draft.get("error"), dict)
                and draft.get("error", {}).get("status") == 429
            )
            if draft.get("status") != "pending" and not legacy_rate_rejection:
                raise CliError(
                    f"Draft status is {draft.get('status')!r}; only pending drafts or confirmed rate-limit rejections can be applied"
                )
            return _apply_draft_locked(draft, store)

        if transaction_scope:
            with store.exclusive_lock(transaction_scope):
                result = run_locked()
        else:
            result = run_locked()
    _json_output(result)


def _add_mutation_fields(parser: argparse.ArgumentParser, *, create: bool) -> None:
    parser.add_argument("--account", required=create, help="Exact account name or id")
    parser.add_argument("--date", required=create, help="ISO date, YYYY-MM-DD")
    amount = parser.add_mutually_exclusive_group(required=create)
    amount.add_argument("--outflow", help="Positive decimal amount to store as an outflow")
    amount.add_argument("--inflow", help="Positive decimal amount to store as an inflow")
    payee = parser.add_mutually_exclusive_group()
    payee.add_argument("--payee", help="Exact existing payee name or id")
    if not create:
        payee.add_argument("--clear-payee", action="store_true")
        payee.add_argument(
            "--new-payee",
            help="Exact missing payee name to create through an explicitly authorized transaction update",
        )
    category = parser.add_mutually_exclusive_group()
    category.add_argument("--category", help="Exact category, 'Group: Category', or id")
    category.add_argument("--uncategorized", action="store_true")
    memo = parser.add_mutually_exclusive_group()
    memo.add_argument("--memo")
    if not create:
        memo.add_argument("--clear-memo", action="store_true")
    parser.add_argument("--cleared", choices=["cleared", "uncleared", "reconciled"])
    approved = parser.add_mutually_exclusive_group()
    approved.add_argument("--approved", dest="approved", action="store_true")
    approved.add_argument("--unapproved", dest="approved", action="store_false")
    parser.set_defaults(approved=None)
    parser.add_argument(
        "--flag",
        choices=["red", "orange", "yellow", "green", "blue", "purple", "none"],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ynab-agent-skill",
        description="Read YNAB budgets and transactions, and prepare explicitly reviewed changes.",
    )
    parser.add_argument("--version", action="version", version=f"ynab-agent-skill {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    terms = commands.add_parser("terms", help="Review and acknowledge the required third-party notice")
    terms_commands = terms.add_subparsers(dest="terms_command", required=True)
    terms_commands.add_parser("status")
    terms_commands.add_parser("acknowledge")

    auth = commands.add_parser("auth", help="Check API authentication without showing the token")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_commands.add_parser("status")

    plans = commands.add_parser("plans", help="List and select an exact YNAB plan")
    plans_commands = plans.add_subparsers(dest="plans_command", required=True)
    plans_commands.add_parser("list")
    use = plans_commands.add_parser("use")
    use.add_argument("plan", help="Exact plan name or id")

    commands.add_parser("accounts", help="List active plan accounts")
    commands.add_parser("categories", help="List plan categories")
    commands.add_parser("payees", help="List plan payees")

    budgets = commands.add_parser("budgets", help="Read monthly budgets and draft category assignments")
    budget_commands = budgets.add_subparsers(dest="budgets_command", required=True)
    budget_commands.add_parser("months", help="List monthly assigned, activity and Ready to Assign amounts")
    budget_get = budget_commands.add_parser("get", help="Read assigned, activity and available by category")
    budget_get.add_argument("month", help="Explicit YYYY-MM or YYYY-MM-01")
    budget_get.add_argument("--category", help="Exact category, 'Group: Category', or id")
    budget_assign = budget_commands.add_parser("draft-assign", help="Prepare a reviewed assignment; makes no API write")
    budget_assign.add_argument("month", help="Explicit YYYY-MM or YYYY-MM-01")
    budget_assign.add_argument("--category", required=True, help="Exact category, 'Group: Category', or id")
    assignment = budget_assign.add_mutually_exclusive_group(required=True)
    assignment.add_argument("--assigned", help="New total assigned amount; use --assigned=-AMOUNT for negatives")
    assignment.add_argument("--increase", help="Positive amount to add to the current assignment")
    assignment.add_argument("--decrease", help="Positive amount to subtract from the current assignment")

    transactions = commands.add_parser("transactions", help="Read transactions and create write drafts")
    transaction_commands = transactions.add_subparsers(dest="transactions_command", required=True)
    list_parser = transaction_commands.add_parser("list")
    list_parser.add_argument("--since", required=True, help="Required inclusive start date")
    list_parser.add_argument("--until", help="Optional inclusive end date")
    list_parser.add_argument("--type", choices=["uncategorized", "unapproved"])
    list_parser.add_argument("--account", help="Exact active account name or id")
    list_amount = list_parser.add_mutually_exclusive_group()
    list_amount.add_argument("--outflow", help="Exact positive outflow amount")
    list_amount.add_argument("--inflow", help="Exact positive inflow amount")
    list_parser.add_argument("--limit", type=int)
    list_parser.add_argument("--include-splits", action="store_true",
                             help="Include full details and split lines from the list response (no per-record reads)")
    match_parser = transaction_commands.add_parser("match")
    match_parser.add_argument("--since", required=True, help="Required inclusive start date")
    match_parser.add_argument("--until", required=True, help="Required inclusive end date")
    match_parser.add_argument("--account", required=True, help="Exact active account name or id")
    match_amount = match_parser.add_mutually_exclusive_group(required=True)
    match_amount.add_argument("--outflow", help="Exact positive outflow amount")
    match_amount.add_argument("--inflow", help="Exact positive inflow amount")
    match_parser.add_argument("--limit", type=int, default=10)
    get_parser = transaction_commands.add_parser("get")
    get_parser.add_argument("transaction_id")
    receipt_parser = transaction_commands.add_parser("draft-receipt-split")
    receipt_parser.add_argument("transaction_id")
    receipt_parser.add_argument(
        "--proposal", required=True,
        help="Path to a local mode-600 receipt proposal JSON file",
    )
    assess_parser = transaction_commands.add_parser("assess-receipt")
    assess_parser.add_argument("transaction_id")
    assess_parser.add_argument("--proposal", required=True)
    approval_parser = transaction_commands.add_parser("draft-receipt-approval")
    approval_parser.add_argument("transaction_id")
    approval_parser.add_argument("--proposal", required=True)
    receipt_create = transaction_commands.add_parser("draft-receipt-create",
        help="Explicitly draft a NEW receipt split; never a fallback for an unmatched import")
    receipt_create.add_argument("--account", required=True)
    receipt_create.add_argument("--date", required=True)
    receipt_payee = receipt_create.add_mutually_exclusive_group(required=True)
    receipt_payee.add_argument("--payee", help="Exact existing, non-transfer payee")
    receipt_payee.add_argument("--new-payee",
        help="Exact missing payee name, only after the user agrees to create it; draft review still required")
    receipt_create.add_argument("--proposal", required=True)
    receipt_create.add_argument("--cleared", choices=["uncleared", "cleared"], default="uncleared")
    receipt_amount = receipt_create.add_mutually_exclusive_group(required=True)
    receipt_amount.add_argument("--outflow")
    receipt_amount.add_argument("--inflow")
    create_parser = transaction_commands.add_parser("draft-create")
    _add_mutation_fields(create_parser, create=True)
    update_parser = transaction_commands.add_parser("draft-update")
    update_parser.add_argument("transaction_id")
    _add_mutation_fields(update_parser, create=False)

    drafts = commands.add_parser("drafts", help="Review or explicitly apply immutable write drafts")
    draft_commands = drafts.add_subparsers(dest="drafts_command", required=True)
    draft_commands.add_parser("list")
    show = draft_commands.add_parser("show")
    show.add_argument("draft_id")
    apply = draft_commands.add_parser("apply")
    apply.add_argument("draft_id")
    apply.add_argument("--confirm", required=True, help="Must exactly match the draft id")
    reconcile = draft_commands.add_parser("reconcile")
    reconcile.add_argument("draft_id")
    resume = draft_commands.add_parser("resume")
    resume.add_argument("draft_id")
    resume.add_argument("--confirm", required=True, help="Must exactly match the draft id")
    return parser


def main(argv: Sequence[str] | None = None, *, store: StateStore | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        state = store or StateStore()
        if args.command == "terms":
            cmd_terms(args, state)
        elif args.command == "auth":
            cmd_auth(args, state)
        elif args.command == "plans":
            cmd_plans(args, state)
        elif args.command == "accounts":
            cmd_accounts(args, state)
        elif args.command == "categories":
            cmd_categories(args, state)
        elif args.command == "payees":
            cmd_payees(args, state)
        elif args.command == "budgets":
            cmd_budgets(args, state)
        elif args.command == "transactions":
            cmd_transactions(args, state)
        elif args.command == "drafts":
            cmd_drafts(args, state)
        else:
            parser.error("Unknown command")
    except (
        ApiError, CliError, KeychainError, ReceiptProposalError,
        StateError, ValueError, KeyError,
    ) as exc:
        print(f"ynab-agent-skill: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("ynab-agent-skill: Local state I/O failed; reconcile any attempted write before retrying", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ynab-agent-skill: Interrupted. Progress is saved; inspect the draft and reconcile any uncertain write before resuming.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
