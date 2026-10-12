# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Guarded transaction application and recovery, independent of CLI I/O.

Only execute() is a supported execution interface. Client construction is
injected; private locked helpers require its authorization and lock scope.
Confirmation is an operator assertion, not authenticated human consent.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from budget_tools import deletion
from budget_tools.api import ApiError, YnabClient
from budget_tools.errors import CliError
from budget_tools.state import StateStore
from budget_tools.transactions import (
    _extract_saved_transaction,
    _generic_expected,
    _receipt_verification,
    _transaction_detail,
    _transaction_summary,
    _verification,
)
from budget_tools.utils import (
    _fingerprint,
    _new_payee_name,
    _record_created_payee,
    _require_api_ready,
    _require_missing_payee,
)


@contextmanager
def _write_guard(
    client: YnabClient,
    revalidate: Callable[[], None],
    before_send: Callable[[], None],
    rate_limited: Callable[[], None],
) -> Iterator[None]:
    if isinstance(client, YnabClient):
        with client.write_guard(revalidate, before_send, rate_limited):
            yield
    else:
        # Lightweight offline workflow doubles have no transport or rate waits.
        before_send()
        yield


def _expected_verification(draft: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    expected = _generic_expected(draft)
    if (
        draft.get("workflow") == "deletion_restore"
        and expected.get("category_id") is None
        and saved.get("category_name") == "Split"
        and isinstance(saved.get("category_id"), str)
        and saved["category_id"]
        and isinstance(saved.get("subtransactions"), list)
        and saved["subtransactions"]
    ):
        expected["category_id"] = saved["category_id"]
    return _verification(expected, saved)


def _finish_verification(
    draft: dict[str, Any], saved: dict[str, Any], store: StateStore
) -> dict[str, Any]:
    _record_created_payee(draft, saved)
    check = _expected_verification(draft, saved)
    draft["status"] = "applied" if check["verified"] else "applied_with_warning"
    draft["stage"] = "verified" if check["verified"] else "verification_warning"
    draft["apply_finished_at"] = store.now()
    draft["transaction_id"] = saved.get("id") or draft.get("transaction_id")
    draft["after_summary"] = _transaction_summary(saved)
    draft["verification"] = check
    store.save_draft(draft)
    if draft.get("workflow") == "deletion_restore":
        store.append_deletion_history(
            {
                "at": store.now(),
                "event": "restoration_readback",
                "restoration_draft_id": draft["id"],
                "deletion_draft_id": draft["proposal"]["deletion_draft_id"],
                "new_transaction_id": draft.get("transaction_id"),
                "verification": check,
            }
        )
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
        draft["stage"] = (
            "approval_verification_failed" if approved else "categorization_verification_failed"
        )
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

    def revalidate() -> None:
        _check_draft_age(draft)
        fresh = client.get_transaction(draft["plan_id"], draft["transaction_id"])
        if not _receipt_verification(draft, fresh, approved=False)["verified"]:
            raise CliError("Transaction changed during rate-limit wait; reconcile before approval")

    def before_send() -> None:
        _check_draft_age(draft)
        draft["status"] = "applying"
        draft["stage"] = "approval_started"
        draft["approval_started_at"] = store.now()
        store.save_draft(draft)

    def rate_limited() -> None:
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
            "status": exc.status,
            "name": exc.error_name,
            "detail": exc.detail,
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
    draft["status"] = (
        "approval_pending" if draft["proposal"]["approve_after_verification"] else "applied"
    )
    draft["stage"] = (
        "categorization_verified"
        if draft["proposal"]["approve_after_verification"]
        else "verified_categorization"
    )
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


def _apply_draft_locked(
    draft: dict[str, Any], store: StateStore, client: YnabClient
) -> dict[str, Any]:
    """Attempt a pending draft once and persist enough progress for recovery.

    execute must validate the seal, confirmation, and pending status and hold
    the draft lock plus the transaction lock when a target ID is known.
    """
    _check_draft_age(draft)
    plan_id = draft["plan_id"]

    def revalidate() -> None:
        _check_draft_age(draft)
        new_payee_name = _new_payee_name(draft)
        if new_payee_name is not None:
            _require_missing_payee(client, plan_id, new_payee_name)
        if draft.get("workflow") == "deletion_restore":
            deletion.revalidate_restoration(client, store, draft)
        if draft["operation"] == "delete":
            deletion.revalidate(client, store, draft)
        if draft["operation"] in {"update", "receipt_split", "receipt_approval"}:
            if not isinstance(draft.get("before_invariants"), dict):
                raise CliError("Legacy draft lacks verification invariants; create a fresh draft")
            before = client.get_transaction(plan_id, draft["transaction_id"])
            if before.get("deleted"):
                raise CliError("The transaction is deleted")
            if _fingerprint(before) != draft["before_fingerprint"]:
                raise CliError(
                    "The transaction changed after this draft was created; create a fresh draft"
                )
            if draft["operation"] == "receipt_split" and before.get("subtransactions"):
                raise CliError("The transaction became a split after this draft was created")

    revalidate()
    if draft["operation"] == "receipt_approval":
        return _apply_receipt_approval_locked(draft, store, client)

    # Save before the request so interrupted writes cannot remain eligible for apply.
    # If persistence fails, execution stops before any transaction write is sent.
    def before_send() -> None:
        _check_draft_age(draft)
        draft["status"] = "applying"
        draft["stage"] = "write_started"
        draft["apply_started_at"] = store.now()
        store.save_draft(draft)
        if draft["operation"] == "delete":
            deletion.record_history(store, draft, "intent_saved")

    def rate_limited() -> None:
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
            elif draft["operation"] == "delete":
                response = client.delete_transaction(plan_id, draft["transaction_id"])
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
        if draft["operation"] == "delete":
            deletion.record_history(store, draft, draft["stage"])
        raise

    if draft["operation"] == "delete":
        draft["status"] = "verification_pending"
        draft["stage"] = "delete_acknowledged"
        draft["write_acknowledged_at"] = store.now()
        store.save_draft(draft)
        acknowledged = _extract_saved_transaction(response)
        if acknowledged is not None and acknowledged.get("id") != draft["transaction_id"]:
            draft["status"] = "uncertain"
            draft["stage"] = "write_acknowledged_wrong_id"
            store.save_draft(draft)
            raise CliError(
                "Deletion returned a different transaction id; reconcile the original target"
            )
        return deletion.finish(draft, deletion.readback(client, draft), store)

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
        raise CliError(
            "The API accepted the write but did not return one transaction for verification"
        )

    transaction_id = saved.get("id") or draft.get("transaction_id")
    if draft.get("transaction_id") and transaction_id != draft["transaction_id"]:
        draft["status"] = "uncertain"
        draft["stage"] = "write_acknowledged_wrong_id"
        store.save_draft(draft)
        raise CliError(
            "The write returned a different transaction id; reconcile the original transaction"
        )
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


def _reconcile_draft_locked(
    draft: dict[str, Any], store: StateStore, client: YnabClient
) -> dict[str, Any]:
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
    if draft.get("operation") == "delete":
        if draft.get("stage") not in {
            "write_started",
            "write_uncertain",
            "write_rejected",
            "delete_acknowledged",
            "write_acknowledged_wrong_id",
            "verified_deletion",
            "deletion_not_verified",
        }:
            raise CliError("Deletion has no recorded attempt to reconcile")
        check = deletion.readback(client, draft)
        if not check["verified"]:
            draft["reconciliation"] = check
            draft["last_reconciled_at"] = store.now()
            store.save_draft(draft)
            raise CliError(
                "The transaction is still present; deletion is not verified; do not replay"
            )
        return deletion.finish(draft, check, store)
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
            draft,
            saved,
            store,
            approved=bool(draft["proposal"]["approve_after_verification"]),
        )
    check = _expected_verification(draft, saved)
    if draft.get("stage") == "write_uncertain" and not check["verified"]:
        draft["last_reconciled_at"] = store.now()
        draft["reconciliation"] = check
        store.save_draft(draft)
        raise CliError("The current transaction does not prove that the uncertain write succeeded")
    return _finish_verification(draft, saved, store)


def execute(
    store: StateStore,
    draft_id: str,
    *,
    client_factory: Callable[[], YnabClient],
    command: str = "apply",
    confirm: str | None = None,
    confirm_protected: str | None = None,
) -> dict[str, Any]:
    """Apply once, reconcile with reads only, or resume receipt approval only.

    Own seal validation under locks, terms, confirmation, status and expiry.
    Persist progress before writes; never replay an uncertain operation.
    """
    if command not in {"apply", "reconcile", "resume"}:
        raise CliError("Unsupported transaction execution command")
    _require_api_ready(store)
    # Always lock draft first, then a known transaction. Reload under the lock so
    # another caller's completed attempt cannot be replayed from our earlier read.
    with store.exclusive_lock(f"draft:{draft_id}"):
        draft = store.load_draft(draft_id)
        if draft.get("operation") not in {
            "create",
            "delete",
            "update",
            "receipt_create",
            "receipt_split",
            "receipt_approval",
        }:
            raise CliError("Not a supported transaction draft")
        transaction_scope = None
        # Different drafts can target one record. A new creation has no target ID
        # yet; neither lock can coordinate with the YNAB UI or other API clients.
        if draft.get("operation") in {
            "delete",
            "update",
            "receipt_split",
            "receipt_approval",
            "receipt_create",
        } and draft.get("transaction_id"):
            transaction_scope = f"transaction:{draft.get('plan_id')}:{draft.get('transaction_id')}"

        def run_locked() -> dict[str, Any]:
            if command == "reconcile":
                return _reconcile_draft_locked(draft, store, client_factory())
            if command == "resume":
                if draft.get("operation") not in {
                    "receipt_split",
                    "receipt_approval",
                    "receipt_create",
                }:
                    raise CliError("Only receipt drafts have a resumable approval stage")
                legacy_rate_rejection = (
                    draft.get("status") == "approval_failed"
                    and draft.get("stage") == "approval_rejected"
                    and isinstance(draft.get("approval_error"), dict)
                    and draft.get("approval_error", {}).get("status") == 429
                )
                if draft.get("status") != "approval_pending" and not legacy_rate_rejection:
                    raise CliError("The receipt draft is not waiting for approval")
                if confirm != draft["id"]:
                    raise CliError("--confirm must exactly match the draft id")
                # Resume is approval-only; it must never reenter the original write.
                return _apply_receipt_approval_locked(draft, store, client_factory())
            if confirm != draft["id"]:
                raise CliError("--confirm must exactly match the draft id")
            if draft["operation"] == "delete":
                proposal = draft.get("proposal", {})
                if proposal.get("protected_states") and (
                    proposal.get("allow_protected") is not True
                    or confirm_protected != draft["transaction_id"]
                ):
                    raise CliError(
                        "Protected deletion requires explicit approval of the full removal preview and --confirm-protected matching the exact transaction id"
                    )
            elif confirm_protected is not None:
                raise CliError("--confirm-protected is only for protected transaction deletion")
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
            return _apply_draft_locked(draft, store, client_factory())

        if transaction_scope:
            with store.exclusive_lock(transaction_scope):
                result = run_locked()
        else:
            result = run_locked()
    return result
