# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Online purchase evidence, read-only analysis and sealed batch construction."""

from __future__ import annotations

import copy
from datetime import date, timedelta
import hashlib
import json
import uuid
from typing import Any

from budget_tools import deletion, purchase_memos as memos
from budget_tools.api import ApiError, YnabClient
from budget_tools.errors import CliError
from budget_tools.receipts import decimal_to_milliunits
from budget_tools.state import StateStore
from budget_tools.transactions import _transaction_detail, _transaction_summary, _verification
from budget_tools.utils import (
    _canonical_subtransactions,
    _currency_string,
    _fingerprint,
    _parse_iso_date,
    _resolve_account,
    _resolve_category,
    _resolve_payee,
    _selected_plan,
    _transaction_invariants,
)


def text(value: Any, field: str, *, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise CliError(f"{field} requires nonempty single-line evidence")
    return value.strip()


def context(
    client: YnabClient, plan_id: str, account_reference: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    account = _resolve_account(client, plan_id, account_reference)
    if account.get("on_budget") is not True:
        raise CliError("Purchase estimates require a known on-budget account")
    currency = client.get_plan_settings(plan_id).get("currency_format", {})
    if (
        type(currency.get("decimal_digits")) is not int
        or currency["decimal_digits"] not in range(4)
        or not isinstance(currency.get("iso_code"), str)
    ):
        raise CliError("Purchase currency/precision is unknown")
    return account, currency


def validate(proposal: dict[str, Any], client: YnabClient, store: StateStore) -> dict[str, Any]:
    plan = _selected_plan(store)
    if (
        type(proposal.get("schema_version")) is not int
        or proposal["schema_version"] != 1
        or proposal.get("plan_id") != plan
    ):
        raise CliError("Purchase proposal version/selected plan mismatch")
    if proposal.get("unresolved") != []:
        raise CliError("Resolve every purchase evidence gap before drafting")
    if any(
        proposal.get(k) not in (None, False, 0, "0", "0.00", [])
        for k in (
            "gift_balance",
            "reward_credit",
            "installments",
            "refunds",
            "cancellations",
            "cross_order",
        )
    ):
        raise CliError(
            "Known mixed tender, refund/cancellation or cross-order context requires manual evidence review"
        )
    if proposal.get("payment_type") != "single_card" or proposal.get("purchase_kind") != "physical":
        raise CliError(
            "Only physical one-card online orders are supported; mixed tenders/installments/refunds require review"
        )
    account, currency = context(client, plan, text(proposal.get("account"), "account"))
    if proposal.get("currency") != currency["iso_code"]:
        raise CliError("Purchase currency differs from the selected plan")
    merchant = text(proposal.get("merchant"), "merchant")
    eligibility = text(
        proposal.get("eligibility_evidence"), "eligible merchant split-charge evidence"
    )
    payee = _resolve_payee(client, plan, text(proposal.get("payee"), "existing payee"))
    if payee.get("transfer_account_id") or str(payee.get("name", "")).casefold().startswith(
        "transfer:"
    ):
        raise CliError("Purchase payee cannot be a transfer")
    ordered = _parse_iso_date(text(proposal.get("order_date"), "order date"))
    if date.fromisoformat(ordered) > date.today():
        raise CliError("Purchase order date cannot be future or inferred from delivery")
    digits = currency["decimal_digits"]

    def money(value: Any, field: str) -> int:
        return decimal_to_milliunits(value, field=field, decimal_digits=digits, positive=True)

    total = money(proposal.get("order_total"), "order total")
    if total != money(proposal.get("card_total"), "card total"):
        raise CliError("Order total differs from card-funded total; mixed tenders unsupported")
    if proposal.get("tenders") is not None:
        tenders = proposal["tenders"]
        if (
            not isinstance(tenders, list)
            or len(tenders) != 1
            or not isinstance(tenders[0], dict)
            or money(tenders[0].get("amount"), "card tender") != total
        ):
            raise CliError("Known payment tenders must be one exact card-funded total")
    order = text(proposal.get("order_reference"), "order reference")
    evidence = text(proposal.get("evidence"), "order evidence")
    stable = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL, json.dumps([plan, account["id"], merchant.casefold(), order])
        )
    )
    units = {}
    items = proposal.get("items")
    if not isinstance(items, list) or not items:
        raise CliError("Purchase requires complete item allocations")
    seen = set()
    category_cache = {}
    for item in items:
        if not isinstance(item, dict):
            raise CliError("Each purchase item must be an object")
        code = text(item.get("id"), "item id")
        if not code.isalnum() or len(code) > 12 or code in seen:
            raise CliError("Item IDs must be unique alphanumeric codes of at most 12 characters")
        seen.add(code)
        quantity = item.get("quantity")
        if type(quantity) is not int or not 1 <= quantity <= 1000:
            raise CliError("Item quantity must be a positive integer, at most 1000")
        amount = money(item.get("amount"), "item allocated total")
        parts = item.get("unit_amounts")
        if parts is None:
            each, remainder = divmod(amount, quantity)
            if remainder or each % (10 ** (3 - digits)):
                raise CliError(
                    "Partial-quantity allocation requires evidenced exact unit_amounts; no rounding guessed"
                )
            parts = [each] * quantity
        elif isinstance(parts, list) and len(parts) == quantity:
            parts = [money(v, "unit amount") for v in parts]
        else:
            raise CliError("unit_amounts must account for every quantity")
        if sum(parts) != amount:
            raise CliError("Item unit amounts do not reconcile")
        category_reference = text(item.get("category"), "category")
        if category_reference not in category_cache:
            category_cache[category_reference] = _resolve_category(client, plan, category_reference)
        category = category_cache[category_reference]
        basis = item.get("allocation_basis")
        if basis not in {"observed", "estimated"}:
            raise CliError(
                "Allocation must explicitly identify observed or estimated tax/discount attribution"
            )
        for position, unit_amount in enumerate(parts, 1):
            uid = f"{code}.{position}"
            units[uid] = {
                "id": uid,
                "item_id": code,
                "position": position,
                "amount": unit_amount,
                "category_id": category["id"],
                "description": text(item.get("description"), "item description"),
                "evidence": text(item.get("evidence"), "item evidence"),
                "allocation_basis": basis,
                **{
                    k: text(item.get(k), k, optional=True) for k in ("memo", "shipment", "delivery")
                },
            }
    require_total(units, total)
    components = proposal.get("components")
    if components is not None:
        if (
            not isinstance(components, list)
            or sum(
                decimal_to_milliunits(c.get("amount"), field="component", decimal_digits=digits)
                for c in components
            )
            != total
        ):
            raise CliError("Signed order components do not reconcile")
    model = {
        "schema_version": 1,
        "id": stable,
        "plan_id": plan,
        "account_id": account["id"],
        "payee_id": payee["id"],
        "payee_name": payee["name"],
        "merchant": merchant,
        "order_reference": order,
        "order_date": ordered,
        "currency": currency["iso_code"],
        "decimal_digits": digits,
        "total": total,
        "units": units,
        "evidence": evidence,
        "eligibility_evidence": eligibility,
        "automation_marker": text(
            proposal.get("automation_marker"), "automation marker", optional=True
        ),
    }
    memos.pending_payload(model, list(units), generation=1)  # Validate all exact memo bytes now.
    return model


def require_total(units: dict[str, Any], total: int) -> None:
    if len(units) > 1000 or sum(u["amount"] for u in units.values()) != total:
        raise CliError(
            "All item units must conserve the complete card-funded total (at most 1000 units)"
        )


def snapshot_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def scoped_transactions(
    client: YnabClient, model: dict[str, Any], *, since: str | None = None, until: str | None = None
) -> list[dict[str, Any]]:
    since = _parse_iso_date(
        since or (date.fromisoformat(model["order_date"]) - timedelta(days=10)).isoformat()
    )
    until = _parse_iso_date(until or date.today().isoformat())
    if since > until:
        raise CliError("Invalid inclusive purchase date range")
    rows = client.get_transactions_by_account(
        model["plan_id"], model["account_id"], since_date=since, until_date=until
    )
    return [
        r
        for r in rows
        if not r.get("deleted")
        and r.get("account_id") == model["account_id"]
        and isinstance(r.get("date"), str)
        and since <= r["date"] <= until
    ]


def duplicate_check(
    client: YnabClient,
    model: dict[str, Any],
    *,
    exempt: set[str] | None = None,
    possible_full_charge: bool = False,
) -> None:
    reference = "Purchase: " + model["id"]
    for row in scoped_transactions(client, model):
        if row.get("id") in (exempt or set()):
            continue
        if reference in (row.get("memo") or "") or (
            row.get("payee_id") == model["payee_id"]
            and model["order_reference"] in (row.get("memo") or "")
        ):
            raise CliError(
                "This purchase already has a YNAB representation; recover/reconcile instead of creating another"
            )
        if (
            possible_full_charge
            and posted(row)
            and row.get("payee_id") == model["payee_id"]
            and row.get("amount") == -model["total"]
        ):
            raise CliError(
                "Possible already-posted full order; provide existing_charges or evidence-backed exclude_charges before estimating"
            )


def summary(
    model: dict[str, Any], remaining: list[str], payload: dict[str, Any] | None
) -> dict[str, Any]:
    allocations: dict[str, int] = {}
    for key in remaining:
        unit = model["units"][key]
        category = unit["category_id"]
        allocations[category] = allocations.get(category, 0) - unit["amount"]
    return {
        "purchase_id": model["id"],
        "merchant": model["merchant"],
        "order_reference": model["order_reference"],
        "original_card_total": _currency_string(model["total"]),
        "remaining": _currency_string(sum(model["units"][k]["amount"] for k in remaining)),
        "category_activity_milliunits": allocations,
        "pending_transaction_payload": payload,
        "notice": "Estimate affects category availability and working balance; bank posting is unconfirmed. Delivery does not prove charge grouping.",
    }


def child_shape(
    operation: str,
    model: dict[str, Any],
    payload: dict[str, Any],
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    step = {
        "id": str(uuid.uuid4()),
        "operation": operation,
        "payload": payload,
        "role": "pending"
        if operation == "create"
        else "retire"
        if operation == "delete"
        else "actual",
    }
    if current is not None:
        step.update(
            transaction_id=current["id"],
            before_summary=_transaction_summary(current),
            before_detail=_transaction_detail(current),
            before_invariants=_transaction_invariants(current),
            before_fingerprint=deletion.fingerprint(current)
            if operation == "delete"
            else _fingerprint(current),
        )
    if operation == "delete":
        if current is None:
            raise CliError("Deletion step requires a reviewed original snapshot")
        deletion.require_eligible(current, current["id"], model["account_id"])
        step["delete_proposal"] = {
            "account_id": model["account_id"],
            "reason": "Retire superseded verified purchase estimate",
            "allow_protected": False,
            "protected_states": [],
            "original_transaction": current,
        }
    return step


def batch(
    store: StateStore,
    model: dict[str, Any],
    operation: str,
    steps: list[dict[str, Any]],
    proposal: dict[str, Any],
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    draft = {
        "id": str(uuid.uuid4()),
        "operation": operation,
        "workflow": "purchase_batch",
        "status": "pending",
        "stage": "drafted",
        "created_at": store.now(),
        "plan_id": model["plan_id"],
        "proposal": {"model": model, **proposal},
        "payload": {"steps": steps},
        "before_detail": copy.deepcopy(record),
        "before_fingerprint": snapshot_hash(record) if record else None,
        "summary": {
            "purchase_id": model["id"],
            "writes": [
                {"operation": s["operation"], "target": s.get("transaction_id")} for s in steps
            ],
            "temporary_duplicates": any(s["operation"] == "delete" for s in steps),
            "category_activity_changes_by_month": activity_changes(steps),
            "notice": "Activity deltas are not exact Available/Ready-to-Assign predictions; review month/payment effects separately.",
        },
    }
    store.save_draft(draft)
    if record is None:
        record = {
            "schema_version": 1,
            "id": model["id"],
            "model": model,
            "status": "drafted",
            "remaining": list(model["units"]),
            "actuals": [],
            "pending": None,
            "generation": 0,
            "history": [],
        }
    record["inflight_draft_id"] = draft["id"]
    store.save_purchase(record)
    return draft


def draft_create(
    client: YnabClient, store: StateStore, proposal: dict[str, Any], *, preview: bool = False
) -> dict[str, Any]:
    model = validate(proposal, client, store)
    with store.exclusive_lock("purchase:" + model["id"]):
        existing = store.load_purchase(model["id"]) if store.purchase_exists(model["id"]) else None
        if existing and (
            existing.get("status") != "uncreated"
            or existing.get("inflight_draft_id")
            or existing.get("model") != model
        ):
            raise CliError("Purchase is already tracked; inspect existing ledger/draft")
        actuals: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []
        selected: set[str] = set()
        targets: set[str] = set()
        existing_charges = proposal.get("existing_charges", [])
        if not isinstance(existing_charges, list):
            raise CliError("existing_charges must be an array of evidenced item mappings")
        for charge in existing_charges:
            if not isinstance(charge, dict):
                raise CliError("Each existing charge must be an object")
            txid = text(charge.get("transaction_id"), "existing charge id")
            keys = charge.get("units")
            evidence = text(charge.get("evidence"), "existing posted charge/item evidence")
            if (
                txid in targets
                or not isinstance(keys, list)
                or not keys
                or any(not isinstance(k, str) for k in keys)
                or len(keys) != len(set(keys))
                or any(k not in model["units"] or k in selected for k in keys)
            ):
                raise CliError("Existing charges must cover known units once")
            current = client.get_transaction(model["plan_id"], txid)
            require_actual(current, txid, model)
            if (
                current.get("deleted")
                or not posted(current)
                or current.get("account_id") != model["account_id"]
                or current.get("payee_id") != model["payee_id"]
                or current.get("transfer_account_id")
                or current.get("amount") != -sum(model["units"][k]["amount"] for k in keys)
            ):
                raise CliError(
                    "Existing charge identity/posting/amount does not match item evidence"
                )
            payload = {"memo": memos.actual_memo(model, keys, current.get("memo"), native=False)}
            if current.get("subtransactions"):
                if not allocation_matches(model, keys, current):
                    raise CliError(
                        "needs_native_correction: existing split allocations cannot be rewritten"
                    )
            else:
                payload.update(memos.allocation_payload(model, keys))
            selected.update(keys)
            targets.add(txid)
            actuals.append({"id": txid, "units": keys, "evidence": evidence})
            steps.append(child_shape("update", model, payload, current))
        excluded = proposal.get("exclude_charges", [])
        if not isinstance(excluded, list):
            raise CliError("exclude_charges must contain reviewed competing-order evidence")
        exclusions: list[dict[str, Any]] = []
        for entry in excluded:
            if not isinstance(entry, dict):
                raise CliError("Each competing-charge exclusion must be an object")
            txid = text(entry.get("transaction_id"), "excluded transaction id")
            evidence = text(entry.get("evidence"), "why this charge belongs to another order")
            current = client.get_transaction(model["plan_id"], txid)
            if current.get("account_id") != model["account_id"] or txid in targets:
                raise CliError("Invalid competing-charge exclusion")
            exclusions.append({"transaction_id": txid, "evidence": evidence, "snapshot": current})
        duplicate_check(
            client,
            model,
            exempt=targets | {x["transaction_id"] for x in exclusions},
            possible_full_charge=True,
        )
        remaining = [k for k in model["units"] if k not in selected]
        pending = memos.pending_payload(model, remaining, generation=1) if remaining else None
        if pending:
            steps.append(child_shape("create", model, pending))
        if preview:
            return {
                **summary(model, remaining, pending),
                "already_posted_charges": actuals,
                "proposed_steps": steps,
                "excluded_charges": exclusions,
                "charge_hypotheses": hypotheses(model),
            }
        return batch(
            store,
            model,
            "purchase_create",
            steps,
            {
                "remaining": remaining,
                "generation": 1,
                "new_actuals": actuals,
                "excluded_charges": exclusions,
            },
            existing,
        )


def allocation_matches(model: dict[str, Any], selected: list[str], current: dict[str, Any]) -> bool:
    expected = memos.allocation_payload(model, selected)
    if expected.get("subtransactions"):
        return _canonical_subtransactions(
            expected["subtransactions"]
        ) == _canonical_subtransactions(current.get("subtransactions"))
    return (
        not current.get("subtransactions") and current.get("category_id") == expected["category_id"]
    )


def require_actual(current: dict[str, Any], transaction_id: str, model: dict[str, Any]) -> None:
    if not isinstance(current, dict):
        raise CliError("Actual charge response is malformed")
    if (
        current.get("id") != transaction_id
        or current.get("account_id") != model["account_id"]
        or current.get("payee_id") != model["payee_id"]
        or current.get("deleted") is not False
        or type(current.get("amount")) is not int
        or type(current.get("approved")) is not bool
        or current.get("cleared") not in {"uncleared", "cleared", "reconciled"}
    ):
        raise CliError("Actual charge identity/account/merchant/state is unknown or mismatched")
    actual_date = _parse_iso_date(text(current.get("date"), "actual charge date"))
    if date.fromisoformat(actual_date) > date.today() or date.fromisoformat(
        actual_date
    ) < date.fromisoformat(model["order_date"]) - timedelta(days=10):
        raise CliError("Actual charge date is outside evidenced order bounds")
    rows = current.get("subtransactions")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise CliError("Actual charge split data is malformed")
    for record in (current, *rows):
        if (
            record.get("transfer_account_id") is not None
            or record.get("transfer_transaction_id") is not None
        ):
            raise CliError("Transfer charge allocations are unsupported")


def posted(current: dict[str, Any]) -> bool:
    return bool(
        current.get("import_id")
        or current.get("matched_transaction_id")
        or current.get("cleared") in {"cleared", "reconciled"}
    )


def draft_reconcile(
    client: YnabClient, store: StateStore, purchase_id: str, proposal: dict[str, Any]
) -> dict[str, Any]:
    with store.exclusive_lock("purchase:" + purchase_id):
        record = store.load_purchase(purchase_id)
        if record.get("inflight_draft_id"):
            raise CliError(
                "An existing purchase batch must be reconciled/resumed or canceled first"
            )
        model = record["model"]
        prior_snapshots = []
        for known in record["actuals"]:
            saved = client.get_transaction(model["plan_id"], known["id"])
            require_actual(saved, known["id"], model)
            if (
                not posted(saved)
                or saved.get("deleted")
                or saved.get("account_id") != model["account_id"]
                or saved.get("amount") != -sum(model["units"][k]["amount"] for k in known["units"])
                or not allocation_matches(model, known["units"], saved)
            ):
                raise CliError("Previously covered actual charge no longer matches item evidence")
            prior_snapshots.append(saved)
        if (
            _selected_plan(store) != model["plan_id"]
            or type(proposal.get("schema_version")) is not int
            or proposal.get("schema_version") != 1
            or proposal.get("purchase_id") != purchase_id
        ):
            raise CliError("Correction proposal purchase/plan mismatch")
        if proposal.get("unresolved") != []:
            raise CliError("Unresolved charge evidence cannot be applied")
        charges = proposal.get("charges")
        if not isinstance(charges, list) or not charges or not record.get("pending"):
            raise CliError(
                "Correction requires a tracked pending estimate and confirmed charge allocations"
            )
        previous = record["pending"]
        try:
            old = client.get_transaction(model["plan_id"], previous["id"])
        except ApiError as exc:
            if exc.status != 404 or exc.error_name != "resource_not_found":
                raise
            old = None
        selected = set()
        targets = set()
        steps = []
        actuals = []
        native = False
        for charge in charges:
            if not isinstance(charge, dict):
                raise CliError("Each posted charge must be an object")
            txid = text(charge.get("transaction_id"), "actual transaction id")
            evidence = text(charge.get("evidence"), "posted charge/item evidence")
            keys = charge.get("units")
            if (
                not isinstance(keys, list)
                or not keys
                or any(not isinstance(k, str) for k in keys)
                or len(keys) != len(set(keys))
                or any(k not in record["remaining"] or k in selected for k in keys)
            ):
                raise CliError(
                    "Every posted portion must cover outstanding known units exactly once"
                )
            if txid in targets or any(a["id"] == txid for a in record["actuals"]):
                raise CliError("A charge cannot be allocated or represented twice")
            targets.add(txid)
            selected.update(keys)
            current = client.get_transaction(model["plan_id"], txid)
            require_actual(current, txid, model)
            if (
                current.get("deleted")
                or current.get("account_id") != model["account_id"]
                or current.get("payee_id") != model["payee_id"]
                or current.get("transfer_account_id")
                or current.get("amount") != -sum(model["units"][k]["amount"] for k in keys)
            ):
                raise CliError(
                    "Actual charge identity/account/merchant/amount does not match reviewed item evidence"
                )
            if not posted(current):
                raise CliError(
                    "Charge is not identified as posted by current API state; bank authorizations are not settlement"
                )
            is_native = (
                txid == previous["id"]
                or current.get("matched_transaction_id") == previous["id"]
                or (old is not None and old.get("matched_transaction_id") == txid)
            )
            if is_native:
                if (
                    len(charges) != 1
                    or set(keys) != set(record["remaining"])
                    or not allocation_matches(model, keys, current)
                ):
                    raise CliError(
                        "needs_native_correction: matched estimate allocation must remain exactly correct"
                    )
                if current.get("memo") != previous["snapshot"].get("memo"):
                    raise CliError("Matched estimate memo was modified; review native correction")
                native = True
            payload = {
                "memo": memos.actual_memo(model, keys, current.get("memo"), native=is_native)
            }
            if current.get("subtransactions"):
                if not allocation_matches(model, keys, current):
                    raise CliError(
                        "needs_native_correction: existing actual split rows cannot be rewritten"
                    )
            elif not is_native:
                payload.update(memos.allocation_payload(model, keys))
            steps.append(child_shape("update", model, payload, current))
            actuals.append({"id": txid, "units": keys, "evidence": evidence})
        remaining = [k for k in record["remaining"] if k not in selected]
        generation = record["generation"] + (1 if remaining else 0)
        if not native:
            if old is None or deletion.fingerprint(old) != previous["fingerprint"]:
                raise CliError(
                    "Old pending estimate changed or disappeared; resolve matching before replacement"
                )
            deletion.require_eligible(old, previous["id"], model["account_id"])
            if remaining:
                payload = memos.pending_payload(
                    model, remaining, generation=generation, replaces=previous["id"]
                )
                steps.append(child_shape("create", model, payload))
            steps.append(child_shape("delete", model, {}, old))
        return batch(
            store,
            model,
            "purchase_reconcile",
            steps,
            {
                "remaining": remaining,
                "generation": generation,
                "new_actuals": actuals,
                "native": native,
                "old_pending": previous,
                "prior_actual_snapshots": prior_snapshots,
            },
            record,
        )


def subset_candidates(
    units: dict[str, dict[str, Any]], target: int, *, max_states: int = 4096
) -> dict[str, Any]:
    """Bounded exact sums suggest possibilities, never prove attribution."""
    states: dict[int, list[list[str]]] = {0: [[]]}
    truncated = False
    for uid, unit in units.items():
        additions: dict[int, list[list[str]]] = {}
        for subtotal, ways in list(states.items()):
            total = subtotal + unit["amount"]
            if total > target:
                continue
            combined = additions.setdefault(total, [])
            combined.extend(w + [uid] for w in ways)
        for total, ways in additions.items():
            if total not in states and len(states) >= max_states:
                truncated = True
                continue
            unique = {tuple(w) for w in states.get(total, []) + ways}
            if len(unique) > 3:
                truncated = True
            states[total] = [list(w) for w in sorted(unique)[:3]]
    ways = states.get(target, [])
    return {
        "allocations": ways,
        "ambiguous": len(ways) != 1 or truncated,
        "search_truncated": truncated,
        "evidence_required": True,
    }


def inspect(
    client: YnabClient,
    store: StateStore,
    purchase_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    record = store.load_purchase(purchase_id)
    model = record["model"]
    if _selected_plan(store) != model["plan_id"]:
        raise CliError("Select the tracked purchase plan")
    _, currency = context(client, model["plan_id"], model["account_id"])
    if (
        currency["iso_code"] != model["currency"]
        or currency["decimal_digits"] != model["decimal_digits"]
    ):
        raise CliError("Purchase currency changed; candidate amounts require fresh review")
    units = {k: model["units"][k] for k in record["remaining"]}
    known = {a["id"] for a in record["actuals"]}
    candidates = []
    for row in scoped_transactions(client, model, since=since, until=until):
        if (
            row["id"] in known
            or row.get("payee_id") != model["payee_id"]
            or not posted(row)
            or type(row.get("amount")) is not int
            or row["amount"] >= 0
        ):
            continue
        candidates.append(
            {"transaction": _transaction_detail(row), **subset_candidates(units, -row["amount"])}
        )
    return {
        "purchase_id": purchase_id,
        "pending": record.get("pending"),
        "remaining_units": list(units),
        "candidates": candidates,
        "notice": "Exact totals/delivery dates are suggestions only; confirm charge/item attribution with order/payment evidence.",
    }


def recovery_marker(memo: str) -> str:
    """Preserve an optional visible suffix as data, never an instruction."""
    known_fields = {
        "Purchase",
        "Merchant",
        "Order",
        "Ordered",
        "Currency",
        "Original",
        "Covered",
        "Remaining",
        "Generation",
        "Replaces",
        "Items",
        "Item",
        "Units",
        "Amounts",
        "Description",
        "Allocation",
        "Memo",
        "Shipment",
        "Delivery",
    }
    final = memo.splitlines()[-1]
    key, separator, _ = final.partition(": ")
    return "" if separator and key in known_fields else text(final, "recovered automation marker")


def recover(
    client: YnabClient,
    store: StateStore,
    transaction_id: str,
    account_reference: str,
    *,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Reconstruct readable current records; never infer ownership or approval."""
    plan = _selected_plan(store)
    account, currency = context(client, plan, account_reference)
    pending = client.get_transaction(plan, transaction_id)
    deletion.require_eligible(pending, transaction_id, account["id"], allow_protected=True)
    if (
        not (pending.get("memo") or "").splitlines()
        or (pending.get("memo") or "").splitlines()[0] != memos.PENDING_HEADER
    ):
        raise CliError(
            "Not a supported complete purchase estimate memo; supply original order evidence"
        )
    data = memos.fields(pending["memo"])
    try:
        purchase_id = str(uuid.UUID(data["Purchase"]))
        if data["Purchase"] != purchase_id or data["Currency"] != currency["iso_code"]:
            raise ValueError("Identity/currency mismatch")
        original = decimal_to_milliunits(
            data["Original"],
            field="original total",
            decimal_digits=currency["decimal_digits"],
            positive=True,
        )
        covered = decimal_to_milliunits(
            data["Covered"], field="covered total", decimal_digits=currency["decimal_digits"]
        )
        remaining = decimal_to_milliunits(
            data["Remaining"],
            field="remaining total",
            decimal_digits=currency["decimal_digits"],
            positive=True,
        )
        generation = int(data["Generation"])
        if (
            covered < 0
            or covered + remaining != original
            or remaining != -pending["amount"]
            or generation < 1
        ):
            raise ValueError("Totals/generation mismatch")
        model = {
            "schema_version": 1,
            "id": purchase_id,
            "plan_id": plan,
            "account_id": account["id"],
            "payee_id": pending["payee_id"],
            "payee_name": pending.get("payee_name"),
            "merchant": text(data["Merchant"], "merchant"),
            "order_reference": text(data["Order"], "order"),
            "order_date": _parse_iso_date(data["Ordered"]),
            "currency": currency["iso_code"],
            "decimal_digits": currency["decimal_digits"],
            "total": original,
            "units": {},
            "evidence": "Reconstructed from present records; original receipt/approval not available",
            "eligibility_evidence": "Requires fresh order/merchant evidence at adoption",
            "automation_marker": recovery_marker(pending["memo"]),
            "reconstructed": True,
        }
        expected_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                json.dumps(
                    [plan, account["id"], model["merchant"].casefold(), model["order_reference"]]
                ),
            )
        )
        if expected_id != purchase_id:
            raise ValueError("Reference does not match order/account")
    except (KeyError, TypeError, ValueError) as exc:
        raise CliError(
            "Incomplete or inconsistent purchase memo; original order evidence required"
        ) from exc

    def units_for(tx: dict[str, Any]) -> list[dict[str, Any]]:
        rows = tx.get("subtransactions")
        if rows:
            result = []
            for row in rows:
                if not row.get("deleted"):
                    result.extend(
                        memos.parse_row(
                            row.get("memo") or "",
                            row["amount"],
                            row["category_id"],
                            decimal_digits=model["decimal_digits"],
                        )
                    )
            return result
        return memos.parse_row(
            tx.get("memo") or "",
            tx["amount"],
            tx["category_id"],
            decimal_digits=model["decimal_digits"],
        )

    def add(units: list[dict[str, Any]]) -> list[str]:
        keys = []
        for unit in units:
            if unit["id"] in model["units"]:
                raise CliError(
                    "Duplicate item coverage/generations in recovery; no automatic deletion"
                )
            model["units"][unit["id"]] = unit
            keys.append(unit["id"])
        return keys

    require_actual(pending, transaction_id, model)
    outstanding = add(units_for(pending))
    actuals = []
    for tx in scoped_transactions(client, model, since=since, until=until):
        if tx["id"] == transaction_id or "Purchase: " + purchase_id not in (tx.get("memo") or ""):
            continue
        if not posted(tx) or tx.get("payee_id") != model["payee_id"]:
            raise CliError(
                "Competing pending generation or ambiguous related charge; order evidence required"
            )
        require_actual(tx, tx["id"], model)
        linked = memos.fields(tx.get("memo") or "")
        if any(
            linked.get(key) != data[key]
            for key in ("Purchase", "Merchant", "Order", "Ordered", "Currency", "Original")
        ):
            raise CliError("Related charge order identity/context is inconsistent")
        units = units_for(tx)
        if sum(u["amount"] for u in units) != -tx["amount"]:
            raise CliError("Actual recovery charge does not conserve item amounts")
        actuals.append(
            {
                "id": tx["id"],
                "units": add(units),
                "snapshot": tx,
                "evidence": "Current posted record; order attribution must be approved at adoption",
            }
        )
    if (
        sum(u["amount"] for u in model["units"].values()) != original
        or sum(model["units"][k]["amount"] for a in actuals for k in a["units"]) != covered
    ):
        raise CliError(
            "Covered item context is missing or inconsistent; supply order/payment evidence"
        )
    for item in {u["item_id"] for u in model["units"].values()}:
        item_units = [u for u in model["units"].values() if u["item_id"] == item]
        for field in (
            "category_id",
            "description",
            "allocation_basis",
            "memo",
            "shipment",
            "delivery",
        ):
            if len({u[field] for u in item_units}) != 1:
                raise CliError(
                    "Recovered item context differs across quantities; order evidence required"
                )
        positions = sorted(u["position"] for u in item_units)
        if positions != list(range(1, len(positions) + 1)):
            raise CliError("Recovered quantity history is incomplete; order evidence required")
    return {
        "model": model,
        "remaining": outstanding,
        "actuals": actuals,
        "generation": generation,
        "pending": {
            "id": transaction_id,
            "snapshot": pending,
            "fingerprint": deletion.fingerprint(pending),
        },
        "ownership": "unverified",
        "prior_approval": "unavailable",
        "protected_states": deletion.protected_states(pending),
        "already_posted_target": posted(pending),
        "adoption_required": not posted(pending),
    }


def draft_adopt(
    client: YnabClient,
    store: StateStore,
    transaction_id: str,
    account_reference: str,
    evidence: str,
    *,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    recovered = recover(client, store, transaction_id, account_reference, since=since, until=until)
    model = recovered["model"]
    deletion.require_eligible(recovered["pending"]["snapshot"], transaction_id, model["account_id"])
    evidence = text(evidence, "fresh order identity/merchant/covered-item adoption evidence")
    with store.exclusive_lock("purchase:" + model["id"]):
        if store.purchase_exists(model["id"]):
            raise CliError(
                "A purchase ledger already exists; do not overwrite its ownership/history"
            )
        return batch(
            store,
            model,
            "purchase_adopt",
            [],
            {
                "recovered": recovered,
                "adoption_evidence": evidence,
                "remaining": recovered["remaining"],
                "generation": recovered["generation"],
            },
        )


def cancel_draft(store: StateStore, draft_id: str, confirm: str) -> dict[str, Any]:
    from budget_tools.purchase_workflow import ensure_child

    if confirm != draft_id:
        raise CliError("Cancellation confirmation must match the draft id")
    with store.exclusive_lock("draft:" + draft_id):
        draft = store.load_draft(draft_id)
        if draft.get("workflow") != "purchase_batch" or draft.get("status") in {
            "applied",
            "canceled",
        }:
            raise CliError("Not an unapplied purchase batch")
        with store.exclusive_lock("purchase:" + draft["proposal"]["model"]["id"]):
            for step in draft["payload"]["steps"]:
                if ensure_child(store, draft, step)["status"] != "pending":
                    raise CliError("An attempted step cannot be canceled; reconcile the outcome")
            if draft.get("progress"):
                raise CliError("Verified steps require reconciliation rather than cancellation")
            record = store.load_purchase(draft["proposal"]["model"]["id"])
            if record.get("inflight_draft_id") != draft_id:
                raise CliError("Purchase in-flight owner changed")
            record["inflight_draft_id"] = None
            if draft["operation"] in {"purchase_create", "purchase_adopt"}:
                record["status"] = "uncreated"
            store.save_purchase(record)
            draft.update(status="canceled", stage="canceled_without_financial_writes")
            store.save_draft(draft)
            return {"draft_id": draft_id, "status": "canceled", "financial_writes": 0}


def recover_step(
    client: YnabClient,
    store: StateStore,
    draft_id: str,
    step_index: int,
    transaction_id: str,
    evidence: str,
) -> dict[str, Any]:
    """Pin an exact independently identified creation; do not issue an API write."""
    from budget_tools import purchase_workflow as workflow, transaction_workflow

    text(evidence, "independent lost-response identity evidence")
    with store.exclusive_lock("draft:" + draft_id):
        parent = store.load_draft(draft_id)
        if parent.get("workflow") != "purchase_batch" or parent["status"] not in {
            "uncertain",
            "applying",
            "partially_applied",
            "verification_pending",
        }:
            raise CliError("Purchase batch has no unresolved write")
        with store.exclusive_lock("purchase:" + parent["proposal"]["model"]["id"]):
            workflow.active_record(store, parent)
            if type(step_index) is not int or not 0 <= step_index < len(parent["payload"]["steps"]):
                raise CliError("Invalid purchase step index")
            step = parent["payload"]["steps"][step_index]
            saved = workflow.ensure_child(store, parent, step)
            with store.exclusive_lock("draft:" + saved["id"]):
                saved = store.load_draft(saved["id"])
                if (
                    saved["operation"] != "create"
                    or saved.get("transaction_id")
                    or saved["status"] not in {"uncertain", "applying"}
                ):
                    raise CliError("Step does not require exact missing-creation-ID recovery")
                current = client.get_transaction(parent["plan_id"], transaction_id)
                if not transaction_workflow._expected_verification(saved, current)["verified"]:
                    raise CliError(
                        "Independent recovery target does not match every reviewed creation field"
                    )
                model = parent["proposal"]["model"]
                candidates = [
                    t
                    for t in scoped_transactions(client, model)
                    if transaction_workflow._expected_verification(saved, t)["verified"]
                ]
                if len(candidates) != 1 or candidates[0]["id"] != transaction_id:
                    raise CliError("Creation identity is ambiguous; do not pick by amount alone")
                saved["transaction_id"] = transaction_id
                saved["recovery_evidence"] = evidence
                store.save_draft(saved)
                return {
                    "draft_id": draft_id,
                    "child_id": saved["id"],
                    "transaction_id": transaction_id,
                    "financial_writes": 0,
                    "next": "drafts reconcile " + draft_id,
                }


def draft_resume(
    client: YnabClient, store: StateStore, draft_id: str, evidence: str
) -> dict[str, Any]:
    """Fresh review of resolved reality; never automatically replay an old write.

    Used after expiry/definite rejection or a known-ID uncertain operation. A
    creation with no independently recovered ID remains blocked.
    """
    from budget_tools import purchase_workflow as workflow, transaction_workflow

    evidence = text(evidence, "fresh batch resolution and charge/item evidence")
    with store.exclusive_lock("draft:" + draft_id):
        old_parent = store.load_draft(draft_id)
        if old_parent.get("operation") not in {
            "purchase_create",
            "purchase_reconcile",
        } or old_parent["status"] in {"applied", "canceled", "superseded"}:
            raise CliError("Not an unresolved purchase batch")
        model = old_parent["proposal"]["model"]
        with store.exclusive_lock("purchase:" + model["id"]):
            record = workflow.active_record(store, old_parent)
            if record.get("last_batch_id") == draft_id and not record.get("inflight_draft_id"):
                raise CliError(
                    "Ledger is already finalized; reconcile the original batch rather than redrafting"
                )
            if _selected_plan(store) != model["plan_id"]:
                raise CliError("Select the original purchase plan")
            new_proposal = copy.deepcopy(old_parent["proposal"])
            new_proposal.pop("model", None)
            new_proposal.update(resolves_batch=draft_id, resolution_evidence=evidence)
            new_steps = []
            for original in old_parent["payload"]["steps"]:
                saved = workflow.ensure_child(store, old_parent, original)
                txid = saved.get("transaction_id")
                if original["operation"] == "create" and saved["status"] != "pending" and not txid:
                    raise CliError(
                        "Missing creation ID must be independently recovered before fresh batch review"
                    )
                if saved["status"] == "pending" and original["operation"] == "create":
                    new_steps.append(child_shape("create", model, original["payload"]))
                    continue
                if not txid:
                    raise CliError("No known target for batch resolution")
                current = None
                if original["operation"] == "delete":
                    if deletion.readback(client, saved)["verified"]:
                        continue
                    current = client.get_transaction(model["plan_id"], txid)
                    deletion.require_eligible(current, txid, model["account_id"])
                    if deletion.fingerprint(current) != original["before_fingerprint"]:
                        raise CliError(
                            "Superseded estimate changed; native/manual resolution required"
                        )
                    new_steps.append(child_shape("delete", model, {}, current))
                    continue
                current = client.get_transaction(model["plan_id"], txid)
                if original["role"] == "pending" and posted(current):
                    require_actual(current, txid, model)
                    keys = list(new_proposal["remaining"])
                    if (
                        not keys
                        or current.get("account_id") != model["account_id"]
                        or current.get("payee_id") != model["payee_id"]
                        or current.get("amount") != -sum(model["units"][k]["amount"] for k in keys)
                        or not allocation_matches(model, keys, current)
                        or current.get("memo") != original["payload"]["memo"]
                    ):
                        raise CliError(
                            "New remainder matched ambiguously; native correction required"
                        )
                    new_steps.append(
                        child_shape(
                            "update",
                            model,
                            {
                                "memo": memos.actual_memo(
                                    model, keys, current.get("memo"), native=True
                                )
                            },
                            current,
                        )
                    )
                    new_proposal.setdefault("new_actuals", []).append(
                        {"id": txid, "units": keys, "evidence": evidence}
                    )
                    new_proposal["remaining"] = []
                    continue
                if transaction_workflow._expected_verification(saved, current)["verified"]:
                    step = child_shape("observe", model, _transaction_invariants(current), current)
                    step["role"] = original["role"]
                    new_steps.append(step)
                elif original["role"] == "actual":
                    require_actual(current, txid, model)
                    if not posted(current):
                        raise CliError("Previously reviewed actual is no longer posted")
                    mapping = next(a for a in new_proposal["new_actuals"] if a["id"] == txid)
                    if (
                        current.get("deleted")
                        or current.get("account_id") != model["account_id"]
                        or current.get("payee_id") != model["payee_id"]
                        or current.get("amount")
                        != -sum(model["units"][k]["amount"] for k in mapping["units"])
                    ):
                        raise CliError(
                            "Charge identity changed; new evidence/native resolution required"
                        )
                    native_memo = new_proposal.get("native", False) and (
                        current.get("memo") or ""
                    ).startswith(memos.PENDING_HEADER)
                    if native_memo and current.get("memo") != new_proposal["old_pending"][
                        "snapshot"
                    ].get("memo"):
                        raise CliError(
                            "Native-match memo changed; preserve new context through manual review"
                        )
                    payload = {
                        "memo": memos.actual_memo(
                            model, mapping["units"], current.get("memo"), native=native_memo
                        )
                    }
                    if current.get("subtransactions"):
                        if not allocation_matches(model, mapping["units"], current):
                            raise CliError(
                                "needs_native_correction: cannot replan immutable actual split rows"
                            )
                    else:
                        payload.update(memos.allocation_payload(model, mapping["units"]))
                    new_steps.append(child_shape("update", model, payload, current))
                else:
                    raise CliError(
                        "Pending creation state changed; independent/native resolution required"
                    )
            # A verified old deletion has already removed the duplicate; retain
            # its observed fact rather than sending DELETE again in the new batch.
            old = new_proposal.get("old_pending")
            if old and not new_proposal.get("native"):
                old_delete = next(
                    s for s in old_parent["payload"]["steps"] if s["operation"] == "delete"
                )
                saved = workflow.ensure_child(store, old_parent, old_delete)
                if deletion.readback(client, saved)["verified"]:
                    new_proposal.pop("old_pending", None)
                    new_proposal["retired_old_delete_id"] = saved["id"]
            updated = batch(store, model, old_parent["operation"], new_steps, new_proposal, record)
            old_parent.update(
                status="superseded", stage="fresh_review_required", superseded_by=updated["id"]
            )
            store.save_draft(old_parent)
            return updated


def hypotheses(model: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, list[str]] = {}
    for uid, unit in model["units"].items():
        hint = (
            "Shipment " + unit["shipment"]
            if unit.get("shipment")
            else "Delivery hint " + unit["delivery"]
            if unit.get("delivery")
            else "Unknown grouping"
        )
        groups.setdefault(hint, []).append(uid)
    return {
        "groups": [
            {
                "hint": hint,
                "units": keys,
                "amount": _currency_string(sum(model["units"][k]["amount"] for k in keys)),
            }
            for hint, keys in groups.items()
        ],
        "alternatives": [
            "Combined charge possible",
            "Same-delivery items may ship and charge separately",
        ],
        "certainty": "fulfillment hints only; no posted-charge proof",
        "creates_extra_transactions": False,
    }


def draft_finalize(
    client: YnabClient,
    store: StateStore,
    transaction_id: str,
    account_reference: str,
    evidence: str,
    *,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    recovered = recover(client, store, transaction_id, account_reference, since=since, until=until)
    if not recovered["already_posted_target"]:
        raise CliError(
            "Target is still provisional; recover/adopt it instead of claiming settlement"
        )
    model = recovered["model"]
    evidence = text(evidence, "independent native-charge and item evidence")
    with store.exclusive_lock("purchase:" + model["id"]):
        if store.purchase_exists(model["id"]):
            raise CliError("Purchase is already tracked; draft its ordinary reconciliation")
        steps = []
        actuals = copy.deepcopy(recovered["actuals"])
        for actual in actuals:
            observed = child_shape(
                "observe", model, _transaction_invariants(actual["snapshot"]), actual["snapshot"]
            )
            observed["role"] = "actual"
            steps.append(observed)
        current = recovered["pending"]["snapshot"]
        if not allocation_matches(model, recovered["remaining"], current):
            raise CliError(
                "needs_native_correction: posted split/category does not match recovered item evidence"
            )
        steps.append(
            child_shape(
                "update",
                model,
                {
                    "memo": memos.actual_memo(
                        model, recovered["remaining"], current["memo"], native=True
                    )
                },
                current,
            )
        )
        actuals.append(
            {"id": transaction_id, "units": recovered["remaining"], "evidence": evidence}
        )
        return batch(
            store,
            model,
            "purchase_create",
            steps,
            {
                "remaining": [],
                "generation": recovered["generation"],
                "new_actuals": actuals,
                "finalizes_recovered_native_match": True,
                "native_charge_evidence": evidence,
            },
        )


def activity_changes(steps: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Show category activity by transaction month, not a promised Available balance."""

    def allocations(record: dict[str, Any]) -> dict[str, int]:
        rows = [r for r in record.get("subtransactions", []) if not r.get("deleted")]
        if not rows:
            return {record.get("category_id") or "uncategorized": record["amount"]}
        values: dict[str, int] = {}
        for row in rows:
            category = row.get("category_id") or "uncategorized"
            values[category] = values.get(category, 0) + row["amount"]
        return values

    deltas: dict[str, dict[str, int]] = {}
    for step in steps:
        if step["operation"] == "observe":
            continue
        before = step.get("before_invariants")
        after = None if step["operation"] == "delete" else {**(before or {}), **step["payload"]}
        for record, sign in ((before, -1), (after, 1)):
            if record is None:
                continue
            month = record["date"][:7]
            amounts = deltas.setdefault(month, {})
            for category, amount in allocations(record).items():
                amounts[category] = amounts.get(category, 0) + sign * amount
    return {
        month: {c: a for c, a in changes.items() if a}
        for month, changes in deltas.items()
        if any(changes.values())
    }
