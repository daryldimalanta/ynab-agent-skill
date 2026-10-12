# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Draft and verify removal of explicitly selected provisional transactions.

Execution belongs to transaction_workflow.execute. A marker or deletion reason
is operator-supplied context, never proof of ownership or authenticated consent.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from budget_tools.api import ApiError, YnabClient
from budget_tools.errors import CliError
from budget_tools.state import StateStore
from budget_tools.transactions import _transaction_detail, _transaction_summary
from budget_tools.utils import _parse_iso_date, _selected_plan, _transaction_invariants


def fingerprint(transaction: dict[str, Any]) -> str:
    """Seal the full observed record, including split IDs and import metadata."""
    raw = json.dumps(transaction, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def protected_states(transaction: dict[str, Any]) -> list[str]:
    states = []
    if transaction.get("approved") is True:
        states.append("approved")
    if transaction.get("cleared") in {"cleared", "reconciled"}:
        states.append(transaction["cleared"])
    if any(transaction.get(field) is not None for field in (
        "import_id", "import_payee_name", "import_payee_name_original")):
        states.append("imported/posted")
    if transaction.get("matched_transaction_id") is not None:
        states.append("matched")
    return states


def require_eligible(transaction: dict[str, Any], transaction_id: str, account_id: str,
                     *, allow_protected: bool = False) -> None:
    """Fail closed on ambiguous state; transfers can remove a second record."""
    if transaction.get("id") != transaction_id or transaction.get("account_id") != account_id:
        raise CliError("Deletion target does not match the exact transaction and account")
    if transaction.get("deleted") is not False:
        raise CliError("Deletion requires a known non-deleted transaction")
    if transaction.get("cleared") not in {"uncleared", "cleared", "reconciled"} or type(transaction.get("approved")) is not bool:
        raise CliError("Deletion target lacks a known clearing or approval state")
    if protected_states(transaction) and not allow_protected:
        raise CliError("Approved, posted/imported, matched, cleared or reconciled transactions are protected; explicit protected-deletion review is required")
    if not isinstance(transaction.get("date"), str) or type(transaction.get("amount")) is not int:
        raise CliError("Deletion target lacks a valid date or amount")
    _parse_iso_date(transaction["date"])
    rows = transaction.get("subtransactions")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CliError("Deletion target has malformed split data")
    for record in (transaction, *rows):
        if record.get("transfer_account_id") is not None or record.get("transfer_transaction_id") is not None:
            raise CliError("Transfer transactions or transfer split rows cannot be deleted")
    active = [row for row in rows if row.get("deleted") is not True]
    if active and (any(type(row.get("amount")) is not int for row in active)
                   or sum(row["amount"] for row in active) != transaction["amount"]):
        raise CliError("Deletion target has unbalanced or malformed split amounts")


def create_draft(client: YnabClient, store: StateStore, transaction_id: str,
                 account_id: str, reason: str, *, allow_protected: bool = False) -> dict[str, Any]:
    """Prepare a complete removal preview; perform API reads only."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise CliError("Deletion requires a nonempty reason of at most 500 characters")
    plan_id = _selected_plan(store)
    current = client.get_transaction(plan_id, transaction_id)
    require_eligible(current, transaction_id, account_id, allow_protected=allow_protected)
    draft = {
        "id": str(uuid.uuid4()), "operation": "delete", "workflow": "transaction_delete",
        "status": "pending", "stage": "drafted", "created_at": store.now(),
        "plan_id": plan_id, "transaction_id": transaction_id,
        "before_fingerprint": fingerprint(current),
        "before_invariants": _transaction_invariants(current),
        "before_summary": _transaction_summary(current), "before_detail": _transaction_detail(current),
        "payload": {},
        "proposal": {"account_id": account_id, "reason": reason.strip(),
                     "allow_protected": allow_protected, "protected_states": protected_states(current),
                     "original_transaction": current},
        "changes": {"delete_entire_transaction": True, "includes_all_split_rows": True},
        "summary": {"transaction_id": transaction_id, "account_id": account_id,
                    "reason": reason.strip(), "irreversible": True,
                    "protected_states": protected_states(current),
                    "warning": "Removes this entire transaction and every split; can change balances and reconciliation."},
    }
    store.save_draft(draft)
    return draft


def revalidate(client: YnabClient, store: StateStore, draft: dict[str, Any]) -> None:
    if _selected_plan(store) != draft["plan_id"]:
        raise CliError("Selected plan changed; review deletion in the original plan")
    current = client.get_transaction(draft["plan_id"], draft["transaction_id"])
    require_eligible(current, draft["transaction_id"], draft["proposal"]["account_id"],
                     allow_protected=draft["proposal"].get("allow_protected") is True)
    if fingerprint(current) != draft["before_fingerprint"]:
        raise CliError("Deletion target changed after review; create a fresh draft")


def readback(client: YnabClient, draft: dict[str, Any]) -> dict[str, Any]:
    """Observe the known target after an attempt; absence does not prove causation."""
    try:
        saved = client.get_transaction(draft["plan_id"], draft["transaction_id"])
    except ApiError as exc:
        if exc.status != 404 or exc.error_name != "resource_not_found":
            raise
        # The same error can mean a missing plan. Verify its account still exists
        # before interpreting absence of the previously observed transaction.
        accounts = client.get_accounts(draft["plan_id"])
        if not any(a.get("id") == draft["proposal"]["account_id"] and a.get("deleted") is False
                   for a in accounts):
            raise CliError("Cannot verify deletion: original plan/account is unavailable") from exc
        return {"verified": True, "observed": "resource_not_found",
                "transaction_id": draft["transaction_id"], "attribution": "current_state_only"}
    if not isinstance(saved, dict) or saved.get("id") != draft["transaction_id"]:
        raise CliError("Deletion readback returned an invalid or different transaction")
    if saved.get("account_id") not in (None, draft["proposal"]["account_id"]):
        raise CliError("Deletion readback returned a different account")
    return {"verified": saved.get("deleted") is True,
            "observed": "deleted" if saved.get("deleted") is True else "still_present",
            "transaction_id": draft["transaction_id"], "attribution": "current_state_only"}


def finish(draft: dict[str, Any], check: dict[str, Any], store: StateStore) -> dict[str, Any]:
    draft["verification"] = check
    draft["status"] = "applied" if check["verified"] else "verification_failed"
    draft["stage"] = "verified_deletion" if check["verified"] else "deletion_not_verified"
    draft["apply_finished_at"] = store.now()
    store.save_draft(draft)
    record_history(store, draft, "verified" if check["verified"] else "verification_failed")
    store.append_audit({"at": store.now(), "draft_id": draft["id"], "operation": "delete",
                        "transaction_id": draft["transaction_id"],
                        "before": draft["before_summary"], "verification": check})
    return {"draft_id": draft["id"], "status": draft["status"], "stage": draft["stage"],
            "transaction_id": draft["transaction_id"], "verification": check,
            "deletion_history_path": str(store.deletion_history_path)}


def record_history(store: StateStore, draft: dict[str, Any], event: str) -> None:
    store.append_deletion_history({
        "at": store.now(), "event": event, "draft_id": draft["id"],
        "plan_id": draft["plan_id"], "transaction_id": draft["transaction_id"],
        "reason": draft["proposal"]["reason"], "protected_states": draft["proposal"]["protected_states"],
        "original_transaction": draft["proposal"]["original_transaction"],
        "verification": draft.get("verification"),
        "restore_command": f"ynab-agent-skill transactions draft-restore-deletion {draft['id']}",
    })


def create_restoration_draft(client: YnabClient, store: StateStore,
                             deletion_draft_id: str) -> dict[str, Any]:
    """Prepare a new manual recreation; never claim to restore imported identity."""
    with store.exclusive_lock(f"draft:{deletion_draft_id}"):
        source = store.load_draft(deletion_draft_id)
        if source.get("operation") != "delete" or source.get("status") != "applied" or not source.get("verification", {}).get("verified"):
            raise CliError("Restoration requires a verified deletion; reconcile its draft first")
        if _selected_plan(store) != source["plan_id"]:
            raise CliError("Select the original plan before preparing restoration")
        if not readback(client, source)["verified"]:
            raise CliError("The original transaction exists; do not recreate it")
        if source.get("restoration_draft_id"):
            raise CliError("A restoration draft already exists; show/reconcile that draft, never recreate blindly")
        original = source["proposal"]["original_transaction"]
        account = source["proposal"]["account_id"]
        accounts = client.get_accounts(source["plan_id"])
        if not any(a.get("id") == account and not a.get("deleted") and not a.get("closed") for a in accounts):
            raise CliError("Original account is unavailable for restoration")
        candidates = client.get_transactions_by_account(
            source["plan_id"], account, since_date=original["date"], until_date=original["date"])
        if any(not t.get("deleted") and t.get("date") == original["date"]
               and t.get("amount") == original["amount"] for t in candidates):
            raise CliError("A same-date, same-amount transaction exists; review potential duplicate before restoration")
        payload = {k: original.get(k) for k in ("account_id", "date", "amount", "payee_id", "category_id", "memo", "flag_color")}
        payload.update(cleared="uncleared", approved=False)
        rows = original["subtransactions"]
        if rows:
            payload["subtransactions"] = [
                {k: row.get(k) for k in ("amount", "payee_id", "category_id", "memo")}
                for row in rows if not row.get("deleted")
            ]
            payload["category_id"] = None
        draft = {
            "id": str(uuid.uuid4()), "operation": "create", "workflow": "deletion_restore",
            "status": "pending", "stage": "drafted", "created_at": store.now(),
            "plan_id": source["plan_id"], "payload": payload,
            "proposal": {"deletion_draft_id": source["id"], "original_transaction_id": source["transaction_id"],
                         "original_transaction": original},
            "before_detail": source["before_detail"],
            "changes": {"recreate_deleted_transaction": True, "cleared": "uncleared", "approved": False},
            "summary": {"deletion_draft_id": source["id"], "original_transaction_id": source["transaction_id"],
                        "warning": "Creates NEW manual ID; does not restore import/match links, approval or reconciliation history."},
        }
        # Pin intent on the source before publishing a creation draft. An
        # interruption here is fail-closed; it must not produce duplicate drafts.
        source["restoration_draft_id"] = draft["id"]
        store.save_draft(source)
        store.save_draft(draft)
        store.append_deletion_history({"at": store.now(), "event": "restoration_drafted",
                                       "deletion_draft_id": source["id"], "restoration_draft_id": draft["id"]})
        return draft


def revalidate_restoration(client: YnabClient, store: StateStore, draft: dict[str, Any]) -> None:
    source = store.load_draft(draft["proposal"]["deletion_draft_id"])
    if source.get("restoration_draft_id") != draft["id"] or _selected_plan(store) != draft["plan_id"]:
        raise CliError("Restoration source or selected plan changed")
    if not readback(client, source)["verified"]:
        raise CliError("Original transaction exists; restoration blocked")
    original = source["proposal"]["original_transaction"]
    candidates = client.get_transactions_by_account(
        draft["plan_id"], original["account_id"], since_date=original["date"], until_date=original["date"])
    if any(not t.get("deleted") and t.get("date") == original["date"]
           and t.get("amount") == original["amount"] for t in candidates):
        raise CliError("Potential duplicate appeared; restoration blocked")
