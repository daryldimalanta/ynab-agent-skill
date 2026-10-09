# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Monthly budget queries and sealed, single-category assignment workflows.

Only execute() is the supported write entry point. It owns confirmation, sealed
intent, local locks, expiry, stale-state checks and uncertain-write recovery.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from budget_tools.api import ApiError, YnabClient
from budget_tools.state import StateStore

MAX_MILLIUNITS = 2**53 - 1
IDENTITY = ("id", "category_group_id", "name", "group_name", "hidden", "deleted",
            "internal", "group_hidden", "group_deleted", "group_internal", "group_known")


def month_key(value: str) -> str:
    """Require an explicit month so a delayed write cannot switch calendar months."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}(?:-01)?", value):
        raise ValueError("Use an explicit month: YYYY-MM or YYYY-MM-01")
    return date.fromisoformat(value[:7] + "-01").isoformat()


def money(value: Any) -> dict[str, Any]:
    if type(value) is not int:
        raise ValueError("The API did not return an integer budget amount")
    whole, fraction = divmod(abs(value), 1000)
    amount = str(whole) + (f".{fraction:03d}".rstrip("0") if fraction else "")
    return {"milliunits": value, "currency": ("-" if value < 0 else "") + amount}


def month_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {"month": month_key(record["month"]),
            "income": money(record["income"]), "assigned": money(record["budgeted"]),
            "activity": money(record["activity"]),
            "ready_to_assign": money(record["to_be_budgeted"])}


def _records(client: YnabClient, plan_id: str, month: str) -> tuple[dict, list[dict]]:
    raw = client.get_month(plan_id, month)
    if raw.get("month") != month or raw.get("deleted"):
        raise ValueError("The API did not return the requested active budget month")
    summary = month_summary(raw)
    groups = client.get_categories(plan_id)
    by_group = {g["id"]: g for g in groups}
    records = []
    seen = set()
    for category in raw["categories"]:
        if category.get("deleted"):
            continue
        if category["id"] in seen:
            raise ValueError("The API returned a duplicate monthly category")
        seen.add(category["id"])
        group = by_group.get(category["category_group_id"], {})
        item = {key: category.get(key) for key in ("id", "category_group_id", "name")}
        item.update({key: bool(category.get(key)) for key in ("hidden", "deleted", "internal")})
        item.update(group_name=group.get("name", category.get("category_group_name")),
                    group_known=bool(group), group_hidden=bool(group.get("hidden")),
                    group_deleted=bool(group.get("deleted")),
                    group_internal=bool(group.get("internal")) or group.get("name") == "Internal Master Category")
        # Use monthly figures, never the current-month amounts in get_categories().
        item.update(assigned=money(category["budgeted"]), activity=money(category["activity"]),
                    available=money(category["balance"]))
        item["overspent"] = category["balance"] < 0
        item["goal"] = {"type": category.get("goal_type"),
                        "target": money(category["goal_target"]) if category.get("goal_target") is not None else None,
                        "underfunded": money(category["goal_under_funded"]) if category.get("goal_under_funded") is not None else None}
        records.append(item)
    return summary, records


def _resolve(records: list[dict], reference: str) -> dict:
    by_id = [r for r in records if r["id"] == reference]
    if by_id:
        return by_id[0]
    if ":" in reference:
        group, name = (part.strip().casefold() for part in reference.split(":", 1))
        matches = [r for r in records if str(r["group_name"]).casefold() == group
                   and str(r["name"]).casefold() == name]
    else:
        matches = [r for r in records if str(r["name"]).casefold() == reference.casefold()]
    if len(matches) != 1:
        raise ValueError("Budget category must match exactly once; use its ID or 'Group: Category'")
    return matches[0]


def _writable(category: dict) -> None:
    if not category["group_known"] or any(category[k] for k in (
            "hidden", "deleted", "internal", "group_hidden", "group_deleted", "group_internal")):
        raise ValueError("Hidden, deleted, internal or unclassified budget categories cannot be adjusted")


def read_budget(client: YnabClient, plan_id: str, month: str, reference: str | None = None) -> dict:
    month = month_key(month)
    summary, categories = _records(client, plan_id, month)
    if reference is not None:
        categories = [_resolve(categories, reference)]
    return {"plan_id": plan_id, **summary, "categories": categories,
            "currency_format": client.get_plan_settings(plan_id)["currency_format"],
            "category_count": len(categories), "category_filter": reference,
            "totals_scope": "whole_month", "amounts_source": "requested_month"}


def _snapshot(client: YnabClient, plan_id: str, month: str, reference: str) -> dict:
    summary, records = _records(client, plan_id, month)
    category = _resolve(records, reference)
    _writable(category)
    currency = client.get_plan_settings(plan_id)["currency_format"]
    digits = currency["decimal_digits"]
    if type(digits) is not int or not 0 <= digits <= 3:
        raise ValueError("Unsupported plan currency precision")
    return {"month": summary, "category": category, "currency_format": currency}


def create_draft(client: YnabClient, store: StateStore, month: str, reference: str,
                 amount: int, *, relative: bool = False) -> dict:
    store.require_terms_acknowledged()
    plan_id = store.configured_plan_id()
    month = month_key(month)
    before = _snapshot(client, plan_id, month, reference)
    category = before["category"]
    if type(amount) is not int or abs(amount) > MAX_MILLIUNITS:
        raise ValueError("Invalid assignment amount")
    assigned = category["assigned"]["milliunits"]
    target = assigned + amount if relative else amount
    quantum = 10 ** (3 - before["currency_format"]["decimal_digits"])
    if abs(target) > MAX_MILLIUNITS or target % quantum or amount % quantum:
        raise ValueError("Assignment exceeds the supported range or plan currency precision")
    delta = target - assigned
    if delta == 0:
        raise ValueError("The requested assignment would make no change")
    available = category["available"]["milliunits"] + delta
    ready = before["month"]["ready_to_assign"]["milliunits"] - delta
    draft = {
        "id": str(uuid.uuid4()), "operation": "budget_assign", "workflow": "budget_planning",
        "plan_id": plan_id, "created_at": store.now(), "status": "pending",
        "proposal": {"month": month, "category_id": category["id"]},
        "payload": {"budgeted": target}, "before_detail": before,
        "before_summary": before,
        "changes": {"assigned_before": money(assigned), "assigned_after": money(target),
                    "delta": money(delta), "estimated_available_after": money(available),
                    "estimated_ready_to_assign_after": money(ready),
                    "warnings": (["Estimated Ready to Assign is negative"] if ready < 0 else [])
                                + (["Estimated category availability is negative"] if available < 0 else [])},
        "summary": f"Set {month} assignment for {category['group_name']}: {category['name']} to {money(target)['currency']}",
    }
    store.save_draft(draft)
    return draft


def _check_age(draft: dict) -> None:
    created = datetime.fromisoformat(draft["created_at"])
    if created.tzinfo is None:
        raise ValueError("Budget draft timestamp must include a timezone")
    now = datetime.now(timezone.utc)
    if now - created > timedelta(hours=24) or created > now + timedelta(minutes=5):
        raise ValueError("Budget draft expired or has a future timestamp; create a fresh draft")


def _revalidate(client: YnabClient, store: StateStore, draft: dict) -> None:
    _check_age(draft)
    store.require_terms_acknowledged()
    if store.configured_plan_id() != draft["plan_id"]:
        raise ValueError("The selected plan changed; select the reviewed plan before applying")
    p = draft["proposal"]
    current = _snapshot(client, draft["plan_id"], p["month"], p["category_id"])
    if current != draft["before_detail"]:
        raise ValueError("The budget changed after review; create and approve a fresh draft")


def _readback(client: YnabClient, store: StateStore, draft: dict) -> dict:
    p = draft["proposal"]
    summary, records = _records(client, draft["plan_id"], p["month"])
    actual = _resolve(records, p["category_id"])
    expected = draft["before_detail"]["category"]
    mismatches = [k for k in IDENTITY if actual[k] != expected[k]]
    if actual["assigned"]["milliunits"] != draft["payload"]["budgeted"]:
        mismatches.append("assigned")
    draft["verification"] = {"verified": not mismatches, "mismatches": mismatches}
    draft["readback"] = {"plan_id": draft["plan_id"], **summary, "category": actual}
    draft["last_reconciled_at"] = store.now()
    draft["status"] = "verification_failed" if mismatches else "applied"
    draft["stage"] = "verification_failed" if mismatches else "verified"
    store.save_draft(draft)
    if mismatches:
        raise ValueError("Budget readback does not match the reviewed assignment; do not replay the write")
    store.append_audit({"draft_id": draft["id"], "operation": "budget_assign",
                        "plan_id": draft["plan_id"], "month": p["month"],
                        "category_id": p["category_id"], "at": store.now(),
                        "verified": True})
    return {"id": draft["id"], "status": draft["status"], "verification": draft["verification"],
            "readback": draft["readback"]}


def execute(store: StateStore, draft_id: str, *, client_factory: Callable[[], YnabClient],
            confirm: str | None = None, reconcile: bool = False) -> dict:
    """Apply once, or reconcile with GETs only. Never resume an uncertain write."""
    store.require_terms_acknowledged()
    with store.exclusive_lock(f"draft:{draft_id}"):
        draft = store.load_draft(draft_id)  # Reload and verify seal while locked.
        if draft["operation"] != "budget_assign":
            raise ValueError("Not a budget assignment draft")
        with store.exclusive_lock(f"budget-plan:{draft['plan_id']}"):
            if reconcile:
                if draft["status"] not in {"applying", "uncertain", "verification_pending", "verification_failed", "applied"}:
                    raise ValueError("Only attempted budget writes can be reconciled")
                return _readback(client_factory(), store, draft)
            if confirm != draft_id:
                raise ValueError("--confirm must exactly match the budget draft id")
            if draft["status"] != "pending":
                raise ValueError("Only pending budget drafts can be applied; reconcile attempted writes")
            _check_age(draft)
            client = client_factory()
            _revalidate(client, store, draft)

            def before_send():
                _check_age(draft)
                draft.update(status="applying", stage="write_started", apply_started_at=store.now())
                store.save_draft(draft)

            def rate_limited():
                draft.update(status="pending", stage="write_rate_limited")
                store.save_draft(draft)

            p = draft["proposal"]
            try:
                with client.write_guard(lambda: _revalidate(client, store, draft), before_send, rate_limited):
                    client.update_month_category(draft["plan_id"], p["month"], p["category_id"],
                                                 draft["payload"]["budgeted"])
            except ApiError as exc:
                rejected = exc.status is not None and 400 <= exc.status < 500
                draft.update(status="failed" if rejected else "uncertain",
                             stage="write_rejected" if rejected else "write_uncertain",
                             error={"status": exc.status, "name": exc.error_name})
                if exc.status == 429:
                    draft.update(status="pending", stage="write_rate_limited")
                store.save_draft(draft)
                raise
            draft.update(status="verification_pending", stage="write_acknowledged")
            store.save_draft(draft)
            return _readback(client, store, draft)
