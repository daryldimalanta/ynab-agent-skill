# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Coordinate reviewed drafts, guarded API writes, and durable recovery.

Command handlers adapt arguments and output; reusable workflows own write policy.
Transaction execution lives in ``transaction_workflow.py``. Reads and draft
construction live in ``transactions.py``; shared
resolvers and API-readiness helpers live in ``utils.py``. See ARCHITECTURE.md
for the module map and receipt state transitions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from budget_tools import __version__, planning, utils, transaction_workflow
from budget_tools.api import ApiError, YnabClient
from budget_tools.errors import CliError
from budget_tools.keychain import KeychainError
from budget_tools.receipts import ReceiptProposalError
from budget_tools.state import StateError, StateStore, TERMS_VERSION
from budget_tools.transaction_workflow import _check_draft_age
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


def cmd_purchases(args: argparse.Namespace, store: StateStore) -> None:
    from budget_tools import purchases
    from budget_tools.state import read_private_json
    command = args.purchases_command
    if command == "list":
        utils._json_output([{k: r.get(k) for k in ("id", "status", "generation", "remaining", "inflight_draft_id")}
                           for r in store.list_purchases()])
        return
    if command == "get":
        record = store.load_purchase(args.purchase_id)
        utils._json_output({"id": record["id"], "model": record["model"], "status": record["status"],
            "generation": record["generation"], "remaining": record["remaining"],
            "pending_transaction_id": record["pending"]["id"] if record.get("pending") else None,
            "actuals": [{"id": a["id"], "units": a["units"]} for a in record["actuals"]],
            "inflight_draft_id": record.get("inflight_draft_id")})
        return
    if command == "cancel-draft":
        utils._json_output(purchases.cancel_draft(store, args.draft_id, args.confirm))
        return
    _selected_plan(store)
    client = utils._client()
    if command in {"preview", "draft-create"}:
        result = purchases.draft_create(client, store, read_private_json(Path(args.proposal)), preview=command == "preview")
    elif command == "draft-reconcile":
        result = purchases.draft_reconcile(client, store, args.purchase_id, read_private_json(Path(args.proposal)))
    elif command == "inspect":
        result = purchases.inspect(client, store, args.purchase_id, since=args.since, until=args.until)
    elif command == "recover":
        result = purchases.recover(client, store, args.transaction_id, args.account, since=args.since, until=args.until)
    elif command == "draft-finalize":
        result = purchases.draft_finalize(client, store, args.transaction_id, args.account, args.evidence, since=args.since, until=args.until)
    elif command == "draft-adopt":
        result = purchases.draft_adopt(client, store, args.transaction_id, args.account, args.evidence, since=args.since, until=args.until)
    elif command == "draft-resume":
        result = purchases.draft_resume(client, store, args.draft_id, args.evidence)
    elif command == "recover-step":
        result = purchases.recover_step(client, store, args.draft_id, args.step, args.transaction_id, args.evidence)
    else:
        raise CliError("Unsupported purchases command")
    utils._json_output(_draft_public(result) if result.get("workflow") == "purchase_batch" else result)


def cmd_drafts(args: argparse.Namespace, store: StateStore) -> None:
    """Adapt CLI requests to the shared guarded workflow interfaces."""
    command = args.drafts_command
    if command == "list":
        utils._json_output(store.list_drafts())
        return
    if command == "deletion-history":
        utils._json_output({"deletion_history_path": store.append_deletion_history()})
        return
    draft = store.load_draft(args.draft_id)
    if command == "show":
        utils._json_output(_draft_public(draft))
        return

    if draft.get("workflow") == "purchase_batch":
        from budget_tools import purchase_workflow
        if getattr(args, "confirm_protected", None) is not None:
            raise CliError("Purchase batches cannot authorize protected deletion")
        utils._json_output(purchase_workflow.execute(store, args.draft_id, client_factory=utils._client,
            command=command, confirm=getattr(args, "confirm", None), confirm_adopt=getattr(args, "confirm_adopt", None)))
        return

    if draft.get("operation") == "budget_assign":
        if command == "resume":
            raise CliError("Budget drafts cannot be resumed; reconcile an attempted assignment without replaying it")
        utils._json_output(planning.execute(store, args.draft_id, client_factory=utils._client,
                                     confirm=getattr(args, "confirm", None), reconcile=command == "reconcile"))
        return

    utils._json_output(transaction_workflow.execute(
        store, args.draft_id, client_factory=utils._client,
        command=command, confirm=getattr(args, "confirm", None),
        confirm_protected=getattr(args, "confirm_protected", None),
    ))


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

    delete_parser = transaction_commands.add_parser(
        "draft-delete", help="Review removal of one exact transaction; protected records require explicit exception")
    delete_parser.add_argument("transaction_id")
    delete_parser.add_argument("--account", required=True, help="Exact account name or id")
    delete_parser.add_argument("--reason", required=True, help="Explicit purpose of deleting this record")
    delete_parser.add_argument("--allow-protected", action="store_true",
        help="Draft an explicit exception for approved/posted/reconciled records; additional apply confirmation required")

    restore_parser = transaction_commands.add_parser("draft-restore-deletion")
    restore_parser.add_argument("deletion_draft_id")

    purchases_parser = commands.add_parser("purchases", help="Review online order estimates, posted charges and remaining items")
    purchase_commands = purchases_parser.add_subparsers(dest="purchases_command", required=True)
    purchase_commands.add_parser("list")
    purchase_get = purchase_commands.add_parser("get")
    purchase_get.add_argument("purchase_id")
    for name in ("preview", "draft-create"):
        option = purchase_commands.add_parser(name)
        option.add_argument("--proposal", required=True, help="Private extracted order evidence JSON")
    correction = purchase_commands.add_parser("draft-reconcile")
    correction.add_argument("purchase_id")
    correction.add_argument("--proposal", required=True)
    investigation = purchase_commands.add_parser("inspect")
    investigation.add_argument("purchase_id")
    investigation.add_argument("--since")
    investigation.add_argument("--until")
    for name in ("recover", "draft-adopt", "draft-finalize"):
        option = purchase_commands.add_parser(name)
        option.add_argument("transaction_id")
        option.add_argument("--account", required=True)
        option.add_argument("--since")
        option.add_argument("--until")
        if name in {"draft-adopt", "draft-finalize"}:
            option.add_argument("--evidence", required=True)
    fresh = purchase_commands.add_parser("draft-resume", help="Prepare fresh review of resolved partial/expired batch; API reads only")
    fresh.add_argument("draft_id")
    fresh.add_argument("--evidence", required=True)
    cancel = purchase_commands.add_parser("cancel-draft")
    cancel.add_argument("draft_id")
    cancel.add_argument("--confirm", required=True)
    recovery = purchase_commands.add_parser("recover-step")
    recovery.add_argument("draft_id")
    recovery.add_argument("--step", required=True, type=int, help="Zero-based creation step index")
    recovery.add_argument("--transaction-id", required=True)
    recovery.add_argument("--evidence", required=True)

    drafts = commands.add_parser("drafts", help="Review or explicitly apply immutable write drafts")
    draft_commands = drafts.add_subparsers(dest="drafts_command", required=True)
    draft_commands.add_parser("list")
    draft_commands.add_parser("deletion-history", help="Create or locate shared private deletion history; no API access")
    show = draft_commands.add_parser("show")
    show.add_argument("draft_id")
    apply = draft_commands.add_parser("apply")
    apply.add_argument("draft_id")
    apply.add_argument("--confirm", required=True, help="Must exactly match the draft id")
    apply.add_argument("--confirm-protected",
        help="Exact transaction id, only after explicit approval of a protected deletion")
    apply.add_argument("--confirm-adopt", help="Exact provisional transaction ID for explicit purchase adoption")
    reconcile = draft_commands.add_parser("reconcile")
    reconcile.add_argument("draft_id")
    resume = draft_commands.add_parser("resume")
    resume.add_argument("draft_id")
    resume.add_argument("--confirm", required=True, help="Must exactly match the draft id")
    resume.add_argument("--confirm-adopt", help="Exact provisional transaction ID for unfinished purchase adoption")
    return parser


def main(argv: Sequence[str] | None = None, *, store: StateStore | None = None) -> int:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Python 3.12 required")
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
        elif args.command == "purchases":
            cmd_purchases(args, state)
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


