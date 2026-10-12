# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Transaction reads, draft construction, and saved-state verification.

Handlers here prepare reviewed drafts and compare authoritative readback with
reviewed intent. They never apply writes; locked application and recovery stay
in ``transaction_workflow.py`` so CLI and library callers share one policy.
"""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path
from typing import Any

from budget_tools import utils
from budget_tools.api import YnabClient
from budget_tools.errors import CliError
from budget_tools.receipts import validate_receipt_proposal
from budget_tools.state import StateStore, read_private_json
from budget_tools.utils import (
    _canonical_subtransactions,
    _currency_string,
    _fingerprint,
    _milliunits,
    _new_payee_name,
    _parse_iso_date,
    _require_missing_payee,
    _resolve_account,
    _resolve_category,
    _resolve_payee,
    _selected_plan,
    _transaction_invariants,
)


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
    client = utils._client()
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
            utils._json_output(
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
            utils._json_output(displayed)
        return
    if command == "get":
        utils._json_output(_transaction_detail(client.get_transaction(plan_id, args.transaction_id)))
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
        utils._json_output(utils._draft_public(draft))
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
            utils._json_output(
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
        utils._json_output(utils._draft_public(draft))
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
        utils._json_output(utils._draft_public(draft))
        return

    if command == "draft-restore-deletion":
        from budget_tools import deletion
        draft = deletion.create_restoration_draft(client, store, args.deletion_draft_id)
        utils._json_output(utils._draft_public(draft))
        return

    if command == "draft-delete":
        from budget_tools import deletion
        account = _resolve_account(client, plan_id, args.account)
        draft = deletion.create_draft(client, store, args.transaction_id, account["id"], args.reason,
                                      allow_protected=args.allow_protected)
        utils._json_output(utils._draft_public(draft))
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
        utils._json_output(utils._draft_public(draft))
        return
    raise CliError("Unknown transactions command")
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
