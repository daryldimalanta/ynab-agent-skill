# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Readable recovery data for online purchase estimates; memos are never authority."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from budget_tools.errors import CliError
from budget_tools.receipts import decimal_to_milliunits
from budget_tools.utils import _currency_string

PENDING_HEADER = "PENDING estimate; May post as separate charges (AI Created)"
ACTUAL_HEADER = "Posted online order charge"


def _money(amount: int) -> str:
    value = _currency_string(amount)
    if value is None:
        raise CliError("Purchase amount is missing")
    return value


def bounded(value: str) -> str:
    if len(value) > 500:
        raise CliError(
            "Purchase context exceeds 500 memo characters; supply a shorter complete description or order evidence; nothing was truncated"
        )
    return value


def row_memo(units: list[dict[str, Any]]) -> str:
    first = units[0]
    amounts = [u["amount"] for u in units]
    lines = [
        f"Item: {first['item_id']}",
        "Units: " + ",".join(str(u["position"]) for u in units),
        "Amounts: " + ",".join(_money(a) for a in amounts),
        f"Description: {first['description']}",
        f"Allocation: {first['allocation_basis']}",
    ]
    for key in ("memo", "shipment", "delivery"):
        if first.get(key):
            lines.append(f"{key.capitalize()}: {first[key]}")
    return bounded("\n".join(lines))


def parent_memo(
    model: dict[str, Any],
    remaining: list[str],
    *,
    generation: int,
    replaces: str | None = None,
    pending: bool = True,
) -> str:
    units = model["units"]
    amount = sum(units[k]["amount"] for k in remaining)
    lines = [
        PENDING_HEADER if pending else ACTUAL_HEADER,
        f"Purchase: {model['id']}",
        f"Merchant: {model['merchant']}",
        f"Order: {model['order_reference']}",
        f"Ordered: {model['order_date']}",
        f"Currency: {model['currency']}",
        f"Original: {_currency_string(model['total'])}",
    ]
    if pending:
        lines += [
            f"Covered: {_currency_string(model['total'] - amount)}",
            f"Remaining: {_currency_string(amount)}",
            f"Generation: {generation}",
        ]
        if replaces:
            lines.append(f"Replaces: {replaces}")
        if len({units[k]["item_id"] for k in remaining}) == 1:
            lines.append(row_memo([units[k] for k in remaining]))
        else:
            lines.append("Items: see splits; covered items: linked actual records")
    if model.get("automation_marker"):
        lines.append(model["automation_marker"])
    rendered = bounded("\n".join(lines))
    fields(rendered)
    return rendered


def allocation_payload(model: dict[str, Any], selected: list[str]) -> dict[str, Any]:
    if (
        not selected
        or len(selected) != len(set(selected))
        or any(k not in model["units"] for k in selected)
    ):
        raise CliError("Purchase allocation must identify known units exactly once")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key in selected:
        groups[model["units"][key]["item_id"]].append(model["units"][key])
    rows = []
    for group in groups.values():
        group.sort(key=lambda u: u["position"])
        rows.append(
            {
                "amount": -sum(u["amount"] for u in group),
                "category_id": group[0]["category_id"],
                "memo": row_memo(group),
            }
        )
    if len(rows) == 1:
        return {"category_id": rows[0]["category_id"]}
    return {"category_id": None, "subtransactions": rows}


def pending_payload(
    model: dict[str, Any], remaining: list[str], *, generation: int, replaces: str | None = None
) -> dict[str, Any]:
    return {
        "account_id": model["account_id"],
        "payee_id": model["payee_id"],
        "date": model["order_date"],
        "amount": -sum(model["units"][k]["amount"] for k in remaining),
        "approved": False,
        "cleared": "uncleared",
        "flag_color": None,
        "memo": parent_memo(model, remaining, generation=generation, replaces=replaces),
        **allocation_payload(model, remaining),
    }


def actual_memo(
    model: dict[str, Any], selected: list[str], current: str | None, *, native: bool
) -> str:
    marker = "Purchase: " + model["id"]
    if not native and current and marker in current:
        data = fields(current)
        if (
            data.get("Purchase") != model["id"]
            or data.get("Order") != model["order_reference"]
            or data.get("Currency") != model["currency"]
            or data.get("Original") != _currency_string(model["total"])
        ):
            raise CliError(
                "Existing purchase-linked memo is incomplete/inconsistent; review its exact replacement"
            )
        if (
            len({model["units"][k]["item_id"] for k in selected}) == 1
            and row_memo([model["units"][k] for k in selected]) not in current
        ):
            raise CliError(
                "Existing charge lacks complete covered-item memo context; order evidence/manual memo review required"
            )
        return bounded(current)
    model_without_marker = dict(model, automation_marker="")
    lines = [parent_memo(model_without_marker, [], generation=0, pending=False)]
    if len({model["units"][k]["item_id"] for k in selected}) == 1:
        lines.append(row_memo([model["units"][k] for k in selected]))
    if current and not native:
        marker_suffix = model.get("automation_marker")
        original_context = current
        if marker_suffix:
            while original_context.rstrip().endswith(marker_suffix):
                original_context = original_context.rstrip()[: -len(marker_suffix)].rstrip()
        if original_context:
            lines.insert(0, original_context)  # Preserve the complete meaningful existing memo.
    if model.get("automation_marker"):
        lines.append(model["automation_marker"])
    rendered = bounded("\n".join(lines))
    fields(rendered)
    return rendered


def fields(memo: str) -> dict[str, str]:
    result = {}
    for line in memo.splitlines():
        key, separator, value = line.partition(": ")
        if separator:
            if key in result:
                raise CliError("Duplicate recovery memo fields require order evidence")
            result[key] = value
    return result


def parse_row(
    memo: str, amount: int, category_id: str, *, decimal_digits: int
) -> list[dict[str, Any]]:
    data = fields(memo)
    try:
        positions = [int(v) for v in data["Units"].split(",")]
        amounts = [
            decimal_to_milliunits(
                v, field="memo amount", decimal_digits=decimal_digits, positive=True
            )
            for v in data["Amounts"].split(",")
        ]
        item = data["Item"]
        if (
            not item
            or not item.isalnum()
            or len(item) > 12
            or len(positions) != len(amounts)
            or len(set(positions)) != len(positions)
            or any(p < 1 for p in positions)
            or sum(amounts) != -amount
            or data["Allocation"] not in {"observed", "estimated"}
        ):
            raise ValueError("Invalid item recovery data")
        return [
            {
                "id": f"{item}.{p}",
                "item_id": item,
                "position": p,
                "amount": a,
                "category_id": category_id,
                "description": data["Description"],
                "allocation_basis": data["Allocation"],
                "evidence": "Reconstructed from current YNAB memo; original source unavailable",
                "memo": data.get("Memo", ""),
                "shipment": data.get("Shipment", ""),
                "delivery": data.get("Delivery", ""),
            }
            for p, a in zip(positions, amounts)
        ]
    except (KeyError, ValueError, TypeError) as exc:
        raise CliError(
            "Incomplete or malformed item memo; original order evidence required"
        ) from exc
