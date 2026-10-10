# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Coordinate reviewed drafts, guarded API writes, and durable recovery.

Command handlers own workflow policy; the API client only sends requests.
Transaction reads and draft construction live in ``transactions.py``; shared
resolvers and API-readiness helpers live in ``utils.py``. See ARCHITECTURE.md
for the module map and receipt state transitions.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from budget_tools import __version__, planning, utils
from budget_tools.api import ApiError, YnabClient
from budget_tools.errors import CliError
from budget_tools.keychain import KeychainError
from budget_tools.receipts import ReceiptProposalError
from budget_tools.state import StateError, StateStore, TERMS_VERSION
from budget_tools.transactions import (
    _canonical_subtransactions,  # re-exported for existing callers
    _extract_saved_transaction,
    _generic_expected,
    _receipt_verification,
    _transaction_detail,
    _transaction_summary,
    _verification,
    cmd_transactions,
)
from budget_tools.utils import (
    _category_records,
    _currency_string,
    _draft_public,
    _fingerprint,  # re-exported for existing callers
    _milliunits,
    _new_payee_name,
    _record_created_payee,
    _require_api_ready,
    _require_missing_payee,
    _resolve_named_resource,
    _selected_plan,
)


THIRD_PARTY_NOTICE = (
    "Such OAuth Apps are not officially supported by YNAB in any way. Use of these "
    "OAuth Apps could introduce problems with YNAB, through its official support "
    "channels, will not be able to troubleshoot or fix. Please use at your own risk!"
)










@contextmanager
def _write_guard(client, revalidate, before_send, rate_limited):
    if isinstance(client, YnabClient):
        with client.write_guard(revalidate, before_send, rate_limited):
            yield
    else:
        # Lightweight offline workflow doubles have no transport or rate waits.
        before_send()
        yield


















































def cmd_terms(args: argparse.Namespace, store: StateStore) -> None:
    if args.terms_command == "status":
        config = store.load_config()
        utils._json_output(
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
    utils._json_output({"acknowledged": True, "terms_version": TERMS_VERSION})


def cmd_auth(args: argparse.Namespace, store: StateStore) -> None:
    _require_api_ready(store)
    utils._client().get_user()
    utils._json_output({"authenticated": True})


def cmd_plans(args: argparse.Namespace, store: StateStore) -> None:
    _require_api_ready(store)
    client = utils._client()
    plans = client.get_plans()
    if args.plans_command == "list":
        config = store.load_config()
        selected = config.get("plan_id")
        utils._json_output(
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
    utils._json_output({"selected": {"id": plan.get("id"), "name": plan.get("name")}})


def cmd_accounts(args: argparse.Namespace, store: StateStore) -> None:
    plan_id = _selected_plan(store)
    accounts = utils._client().get_accounts(plan_id)
    utils._json_output(
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
    groups = utils._client().get_categories(plan_id)
    utils._json_output(
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
        client = utils._client()
        months = client.get_months(plan_id)
        utils._json_output({"plan_id": plan_id, "currency_format": client.get_plan_settings(plan_id)["currency_format"],
                      "months": [planning.month_summary(m) for m in months
                                                    if not m.get("deleted")]})
        return
    month = planning.month_key(args.month)
    if args.budgets_command == "get":
        utils._json_output(planning.read_budget(utils._client(), plan_id, month, args.category))
        return
    if args.assigned is not None:
        negative = args.assigned.startswith("-")
        amount = _milliunits(args.assigned[1:] if negative else args.assigned, outflow=negative)
        relative = False
    else:
        amount = _milliunits(args.increase if args.increase is not None else args.decrease,
                            outflow=args.decrease is not None)
        relative = True
    draft = planning.create_draft(utils._client(), store, month, args.category, amount, relative=relative)
    utils._json_output(_draft_public(draft))


def cmd_payees(args: argparse.Namespace, store: StateStore) -> None:
    plan_id = _selected_plan(store)
    payees = utils._client().get_payees(plan_id)
    utils._json_output(
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
    client = utils._client()
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
    client = utils._client()
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
        utils._json_output(store.list_drafts())
        return
    draft = store.load_draft(args.draft_id)
    if command == "show":
        utils._json_output(_draft_public(draft))
        return

    if draft.get("operation") == "budget_assign":
        if command == "resume":
            raise CliError("Budget drafts cannot be resumed; reconcile an attempted assignment without replaying it")
        utils._json_output(planning.execute(store, args.draft_id, client_factory=utils._client,
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
                return _apply_receipt_approval_locked(draft, store, utils._client())
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
    utils._json_output(result)


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








