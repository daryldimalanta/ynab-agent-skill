# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Reviewed purchase batches reuse guarded transaction execution for every write."""

from __future__ import annotations

from contextvars import ContextVar
import copy
from typing import Any, Callable

from budget_tools import deletion, purchases, transaction_workflow as transactions
from budget_tools.api import YnabClient
from budget_tools.errors import CliError
from budget_tools.state import StateStore
from budget_tools.utils import _selected_plan

_ACTIVE: ContextVar[tuple[str, str] | None] = ContextVar("purchase_batch_scope", default=None)


def child(parent: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    model = parent["proposal"]["model"]
    result = {k: copy.deepcopy(v) for k, v in step.items() if k != "delete_proposal"}
    result.update(
        workflow="purchase_step",
        status="pending",
        stage="drafted",
        created_at=parent["created_at"],
        plan_id=parent["plan_id"],
        summary={"purchase_id": model["id"], "parent_draft_id": parent["id"]},
        changes={"purchase_step": True},
        proposal={
            "parent_draft_id": parent["id"],
            "purchase_id": model["id"],
            **copy.deepcopy(step.get("delete_proposal", {})),
        },
    )
    return result


def ensure_child(store: StateStore, parent: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    expected = child(parent, step)
    path = store.draft_path(step["id"])
    # StateStore checks storage/link/file safety; existence only decides creation.
    from budget_tools.state import _directory, _private_file
    import os

    with _directory(store.drafts_dir, private=True) as directory:
        try:
            _private_file(os.stat(path.name, dir_fd=directory, follow_symlinks=False))
        except FileNotFoundError:
            store.save_draft(expected)
    actual = store.load_draft(step["id"])
    for key, value in expected.items():
        if key not in {"status", "stage"} and actual.get(key) != value:
            raise CliError("Purchase child intent differs from the reviewed batch")
    return actual


def require_step_authority(store: StateStore, draft: dict[str, Any]) -> dict[str, Any]:
    parent_id = draft["proposal"]["parent_draft_id"]
    if _ACTIVE.get() != (parent_id, draft["id"]):
        raise CliError(
            "Purchase child drafts cannot execute directly; apply/resume their reviewed parent batch"
        )
    parent = store.load_draft(parent_id)
    matching = [s for s in parent["payload"]["steps"] if s["id"] == draft["id"]]
    if len(matching) != 1 or parent.get("operation") not in {
        "purchase_create",
        "purchase_reconcile",
    }:
        raise CliError("Invalid sealed purchase parent/step relationship")
    expected = child(parent, matching[0])
    for key in (
        "operation",
        "payload",
        "proposal",
        "before_fingerprint",
        "before_invariants",
        "transaction_id",
    ):
        # Creation learns an ID; it is progress, not reviewed intent.
        if key == "transaction_id" and draft["operation"] == "create":
            continue
        if draft.get(key) != expected.get(key):
            raise CliError("Purchase child authorization does not match sealed intent")
    return parent


def active_record(store: StateStore, parent: dict[str, Any]) -> dict[str, Any]:
    model = parent["proposal"]["model"]
    record = store.load_purchase(model["id"])
    if record.get("model") != model:
        raise CliError("Purchase ledger evidence changed; fresh review required")
    if record.get("inflight_draft_id") != parent["id"]:
        if record.get("last_batch_id") == parent["id"] and not record.get("inflight_draft_id"):
            return record  # Crash after ledger commit but before parent finalization.
        raise CliError("Another batch owns the purchase; do not overwrite its progress")
    if parent.get("before_detail") is not None:
        baseline = copy.deepcopy(record)
        baseline.pop("inflight_draft_id", None)
        expected = copy.deepcopy(parent["before_detail"])
        expected.pop("inflight_draft_id", None)
        if purchases.snapshot_hash(baseline) != purchases.snapshot_hash(expected):
            raise CliError("Purchase ledger changed after review")
    return record


def verify_previous(client: YnabClient, store: StateStore, parent: dict[str, Any]) -> None:
    for step in parent["payload"]["steps"]:
        state = parent.get("progress", {}).get(step["id"])
        if not state or state.get("status") != "verified":
            continue
        saved = store.load_draft(step["id"])
        if saved["operation"] == "delete":
            if not deletion.readback(client, saved)["verified"]:
                raise CliError("Verified old-estimate deletion no longer holds")
        else:
            current = client.get_transaction(parent["plan_id"], saved["transaction_id"])
            if (
                not transactions._expected_verification(saved, current)["verified"]
                or deletion.fingerprint(current) != state["fingerprint"]
            ):
                raise CliError(
                    "Verified purchase step changed or matched; fresh reconciliation required"
                )


def verify_adoption(client: YnabClient, parent: dict[str, Any]) -> None:
    """Read current adoption evidence, including during GET-only recovery."""
    model = parent["proposal"]["model"]
    recovered = parent["proposal"]["recovered"]
    old = recovered["pending"]
    current = client.get_transaction(parent["plan_id"], old["id"])
    deletion.require_eligible(current, old["id"], model["account_id"])
    if deletion.fingerprint(current) != old["fingerprint"]:
        raise CliError("Adoption target changed; fresh review required")
    for actual in recovered["actuals"]:
        current = client.get_transaction(parent["plan_id"], actual["id"])
        if deletion.fingerprint(current) != deletion.fingerprint(actual["snapshot"]):
            raise CliError("Adoption coverage changed; fresh review required")


def revalidate_batch(
    client: YnabClient, store: StateStore, parent: dict[str, Any], *, verify_completed: bool = True
) -> None:
    transactions._check_draft_age(parent)
    if _selected_plan(store) != parent["plan_id"]:
        raise CliError("Selected plan changed since purchase batch review")
    active_record(store, parent)
    model = parent["proposal"]["model"]
    _, currency = purchases.context(client, parent["plan_id"], model["account_id"])
    if (
        currency["iso_code"] != model["currency"]
        or currency["decimal_digits"] != model["decimal_digits"]
    ):
        raise CliError("Purchase currency changed after review")
    payees = client.get_payees(parent["plan_id"])
    if not any(
        p.get("id") == model["payee_id"]
        and not p.get("deleted")
        and not p.get("transfer_account_id")
        for p in payees
    ):
        raise CliError("Purchase payee is no longer eligible")
    categories = client.get_categories(parent["plan_id"])
    writable = {
        c["id"]
        for g in categories
        if not any(g.get(k) for k in ("hidden", "deleted", "internal"))
        and str(g.get("name", "")).casefold() != "credit card payments"
        for c in g.get("categories", [])
        if not any(c.get(k) for k in ("hidden", "deleted", "internal"))
    }
    if any(u["category_id"] not in writable for u in model["units"].values()):
        raise CliError("A purchase category is no longer eligible")
    for excluded in parent["proposal"].get("excluded_charges", []):
        current = client.get_transaction(parent["plan_id"], excluded["transaction_id"])
        if deletion.fingerprint(current) != deletion.fingerprint(excluded["snapshot"]):
            raise CliError("Competing-order exclusion changed after review")
    for previous in parent["proposal"].get("prior_actual_snapshots", []):
        current = client.get_transaction(parent["plan_id"], previous["id"])
        if deletion.fingerprint(current) != deletion.fingerprint(previous):
            raise CliError("Previously covered charge changed after review")
    if verify_completed:
        verify_previous(client, store, parent)
    if parent["operation"] == "purchase_adopt":
        verify_adoption(client, parent)
    old = parent["proposal"].get("old_pending")
    deleted = any(
        s["operation"] == "delete"
        and parent.get("progress", {}).get(s["id"], {}).get("status") == "verified"
        for s in parent["payload"]["steps"]
    )
    if old and not parent["proposal"].get("native") and not deleted:
        current = client.get_transaction(parent["plan_id"], old["id"])
        deletion.require_eligible(current, old["id"], model["account_id"])
        if deletion.fingerprint(current) != old["fingerprint"]:
            raise CliError("Pending estimate changed or matched; stop before further writes")
    if parent["operation"] == "purchase_create":
        exempt = {
            v["transaction_id"]
            for v in parent.get("progress", {}).values()
            if v.get("transaction_id")
        }
        exempt.update(
            s["transaction_id"] for s in parent["payload"]["steps"] if s.get("transaction_id")
        )
        exempt.update(x["transaction_id"] for x in parent["proposal"].get("excluded_charges", []))
        purchases.duplicate_check(client, model, exempt=exempt, possible_full_charge=True)
    if parent["operation"] == "purchase_reconcile":
        exemptions = {a["id"] for a in parent.get("before_detail", {}).get("actuals", [])}
        exemptions.update(a["id"] for a in parent["proposal"].get("new_actuals", []))
        if parent["proposal"].get("old_pending"):
            exemptions.add(parent["proposal"]["old_pending"]["id"])
        exemptions.update(
            s["transaction_id"] for s in parent["payload"]["steps"] if s.get("transaction_id")
        )
        exemptions.update(
            v["transaction_id"]
            for v in parent.get("progress", {}).values()
            if v.get("transaction_id")
        )
        purchases.duplicate_check(client, model, exempt=exemptions)
    if parent["proposal"].get("retired_old_delete_id"):
        retired = store.load_draft(parent["proposal"]["retired_old_delete_id"])
        if not deletion.readback(client, retired)["verified"]:
            raise CliError("Previously retired old estimate reappeared; fresh review required")


def revalidate_step(client: YnabClient, store: StateStore, draft: dict[str, Any]) -> None:
    revalidate_batch(
        client,
        store,
        require_step_authority(store, draft),
        verify_completed=draft["operation"] != "update",
    )


def save_progress(
    client: YnabClient, store: StateStore, parent: dict[str, Any], saved: dict[str, Any]
) -> None:
    state: dict[str, Any] = {"status": "verified", "transaction_id": saved["transaction_id"]}
    if saved["operation"] != "delete":
        current = client.get_transaction(parent["plan_id"], saved["transaction_id"])
        if not transactions._expected_verification(saved, current)["verified"]:
            raise CliError("Purchase step readback changed before journal update")
        state.update(snapshot=current, fingerprint=deletion.fingerprint(current))
    parent.setdefault("progress", {})[saved["id"]] = state
    parent["status"] = "partially_applied"
    parent["stage"] = "step_verified"
    store.save_draft(parent)


def finalize(client: YnabClient, store: StateStore, parent: dict[str, Any]) -> dict[str, Any]:
    verify_previous(client, store, parent)
    if any(
        parent.get("progress", {}).get(s["id"], {}).get("status") != "verified"
        for s in parent["payload"]["steps"]
    ):
        raise CliError("Purchase batch still has unverified steps")
    record = active_record(store, parent)
    model = record["model"]
    if parent["operation"] == "purchase_adopt":
        verify_adoption(client, parent)
        recovered = parent["proposal"]["recovered"]
        record.update(
            pending=recovered["pending"],
            remaining=recovered["remaining"],
            actuals=recovered["actuals"],
            generation=recovered["generation"],
            inflight_draft_id=None,
            last_batch_id=parent["id"],
            status="partially_settled" if recovered["actuals"] else "awaiting_charges",
            ownership="explicitly_adopted",
        )
        record["history"].append(
            {
                "batch_id": parent["id"],
                "at": store.now(),
                "adoption_evidence": parent["proposal"]["adoption_evidence"],
            }
        )
        store.save_purchase(record)
        parent.update(status="applied", stage="verified_adoption", verification={"verified": True})
        store.save_draft(parent)
        return result(parent, record)
    actuals = copy.deepcopy(
        parent.get("before_detail", {}).get("actuals", []) if parent.get("before_detail") else []
    )
    for actual in parent["proposal"].get("new_actuals", []):
        item = copy.deepcopy(actual)
        step = next(
            s
            for s in parent["payload"]["steps"]
            if s.get("transaction_id") == item["id"] and s["role"] == "actual"
        )
        item["snapshot"] = parent["progress"][step["id"]]["snapshot"]
        actuals.append(item)
    pending_steps = [s for s in parent["payload"]["steps"] if s["role"] == "pending"]
    pending = None
    if pending_steps:
        progress = parent["progress"][pending_steps[-1]["id"]]
        pending = {
            "id": progress["transaction_id"],
            "snapshot": progress["snapshot"],
            "fingerprint": progress["fingerprint"],
        }
    remaining = parent["proposal"]["remaining"]
    purchases.require_total(model["units"], model["total"])
    covered = [k for a in actuals for k in a["units"]]
    if (
        len(covered) != len(set(covered))
        or set(covered) & set(remaining)
        or set(covered) | set(remaining) != set(model["units"])
    ):
        raise CliError("Purchase conservation failed; batch cannot be settled")
    record.update(
        pending=pending,
        remaining=remaining,
        actuals=actuals,
        generation=parent["proposal"]["generation"],
        inflight_draft_id=None,
        last_batch_id=parent["id"],
        status="partially_settled"
        if actuals and remaining
        else "awaiting_charges"
        if remaining
        else "settled",
    )
    if not any(h.get("batch_id") == parent["id"] for h in record["history"]):
        record["history"].append(
            {
                "batch_id": parent["id"],
                "at": store.now(),
                "pending_id": pending["id"] if pending else None,
            }
        )
    store.save_purchase(record)
    parent.update(
        status="applied",
        stage="verified_purchase",
        verification={"verified": True},
        apply_finished_at=store.now(),
    )
    store.save_draft(parent)
    return result(parent, record)


def result(parent: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    return {
        "draft_id": parent["id"],
        "purchase_id": record["id"],
        "status": parent["status"],
        "stage": parent["stage"],
        "purchase_status": record["status"],
        "pending_transaction_id": record["pending"]["id"] if record.get("pending") else None,
        "remaining_units": record["remaining"],
        "actual_transaction_ids": [a["id"] for a in record["actuals"]],
        "steps": parent.get("progress", {}),
        "verification": parent.get("verification", {"verified": False}),
        "temporary_duplicate_possible": parent["status"] not in {"applied", "pending"},
    }


def execute(
    store: StateStore,
    draft_id: str,
    *,
    client_factory: Callable[[], YnabClient],
    command: str = "apply",
    confirm: str | None = None,
    confirm_adopt: str | None = None,
) -> dict[str, Any]:
    if command not in {"apply", "resume", "reconcile"}:
        raise CliError("Unsupported purchase execution command")
    store.require_terms_acknowledged()
    with store.exclusive_lock("draft:" + draft_id):
        parent = store.load_draft(draft_id)
        if parent.get("operation") not in {
            "purchase_create",
            "purchase_reconcile",
            "purchase_adopt",
        }:
            raise CliError("Not an executable purchase batch")
        if command != "reconcile" and confirm != draft_id:
            raise CliError("--confirm must match the reviewed purchase batch id")
        if (
            parent["operation"] == "purchase_adopt"
            and command != "reconcile"
            and confirm_adopt != parent["proposal"]["recovered"]["pending"]["id"]
        ):
            raise CliError(
                "Adoption requires --confirm-adopt naming the exact provisional transaction after explicit review"
            )
        if command == "apply" and parent["status"] != "pending":
            raise CliError("Purchase batch already attempted; reconcile before resuming")
        if command == "resume" and parent["status"] not in {
            "partially_applied",
            "uncertain",
            "applying",
            "verification_pending",
        }:
            raise CliError("Purchase batch has no authorized remaining steps to resume")
        if command == "reconcile" and parent["status"] in {"pending", "canceled"}:
            raise CliError("Purchase batch has no recorded attempt to reconcile")
        with store.exclusive_lock("purchase:" + parent["proposal"]["model"]["id"]):
            active_record(store, parent)
            client = client_factory()
            if command != "reconcile":
                revalidate_batch(client, store, parent)
            if command != "reconcile":
                parent.update(status="applying", stage="batch_started")
                store.save_draft(parent)
            try:
                for step in parent["payload"]["steps"]:
                    saved = ensure_child(store, parent, step)
                    if step["operation"] == "observe":
                        current = client.get_transaction(parent["plan_id"], saved["transaction_id"])
                        if not transactions._expected_verification(saved, current)["verified"]:
                            raise CliError("Observed purchase step changed; fresh review required")
                        saved.update(
                            status="applied",
                            stage="verified_observation",
                            verification={"verified": True},
                        )
                        store.save_draft(saved)
                        save_progress(client, store, parent, saved)
                        continue
                    attempted = saved["status"] != "pending"
                    if command == "reconcile" and not attempted:
                        continue
                    token = _ACTIVE.set((parent["id"], step["id"]))
                    try:
                        if attempted:
                            transactions.execute(
                                store,
                                saved["id"],
                                client_factory=lambda: client,
                                command="reconcile",
                            )
                        elif command != "reconcile":
                            revalidate_batch(
                                client,
                                store,
                                parent,
                                verify_completed=step["operation"] != "update",
                            )
                            transactions.execute(
                                store,
                                saved["id"],
                                client_factory=lambda: client,
                                confirm=saved["id"],
                            )
                    finally:
                        _ACTIVE.reset(token)
                    saved = store.load_draft(step["id"])
                    if saved["status"] != "applied" or not saved.get("verification", {}).get(
                        "verified"
                    ):
                        raise CliError(
                            "Purchase step did not verify; stop and reconcile without replay"
                        )
                    save_progress(client, store, parent, saved)
                complete = all(
                    parent.get("progress", {}).get(s["id"], {}).get("status") == "verified"
                    for s in parent["payload"]["steps"]
                )
                if complete:
                    return finalize(client, store, parent)
                return result(parent, store.load_purchase(parent["proposal"]["model"]["id"]))
            except BaseException:
                parent["status"] = "uncertain"
                parent["stage"] = "reconciliation_required"
                store.save_draft(parent)
                raise
