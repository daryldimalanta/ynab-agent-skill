---
name: ynab-agent-skill
license: MIT
description: "Use the local ynab-agent-skill command to read monthly budgets and remaining category balances, prepare approved budget assignments, and find, create, categorize, split, or update transactions in the user's own YNAB plan. Apply for budget planning, receipts, and transaction management."
---

# ynab-agent-skill

Copyright (c) 2026 Daryl Dimalanta. Licensed under the [MIT License](LICENSE.txt).

If this installation includes `references/local-preferences.md`, read it within the user's storage restrictions for their memo defaults and local executable location. Explicit current instructions take precedence. Local preferences do not authorize writes or supply missing receipt evidence; keep them private and preserve them when reinstalling.

Use the locally installed command:

```text
ynab-agent-skill
```

Use an explicitly configured local executable if supplied by the user or private preferences; otherwise locate it with `command -v ynab-agent-skill`. Check its `--version`. If it is absent and no checkout is known, ask for the local checkout location and use its quoted absolute `bin/ynab-agent-skill` path. Do not search unrelated storage or fall back to an older command. Prefix the command fragments below with this executable. The legacy `ynab-codex-skill` command is a compatibility alias when provided by the same reviewed checkout; do not infer that an unrelated older installation is current.

Use the CLI instead of direct API, SDK, or Keychain calls. Never request, reveal, print, export, or log the access token. Keep receipt images and proposal files local and disclose only the fields needed for review. Follow the user's storage and external-transmission restrictions; installation does not grant access to additional storage or services.

## Rate limits and long runs

Version 0.4.0 automatically accounts for the 200-request rolling hour across local commands sharing a token, waits for capacity, and retries confirmed HTTP 429 rejections. Let a waiting command continue, even across multiple hours; rate limiting alone needs no new human approval. Use the execution tool's running-session handle to check completion instead of starting another copy or repeatedly making API probes. Read [long-running work and recovery](references/long-runs.md) before a large batch or resuming an interrupted chat.

The CLI rechecks a delayed write against current transaction or budget state and draft age. Existing approval of an unchanged, unexpired draft remains valid during rate-limit waits. Authentication failure, changed evidence, expired drafts, and uncertain writes are separate conditions; automatic rate-limit retries do not authorize bypassing them.

## Establish context

1. Run `terms status`. If the notice is not acknowledged, tell the user to run `terms acknowledge` interactively. Do not acknowledge it for them.
2. Run `auth status` before relying on API access. If Keychain lookup fails in a restricted shell, follow the recovery guidance below.
3. A transaction or budget operation requires one selected plan. If none is selected, run `plans list`. If multiple plans exist, ask the user to identify the exact plan before `plans use PLAN_ID`.

### Restricted-shell Keychain recovery

An agent's restricted shell can fail to read an existing macOS Keychain item that is accessible in the user's normal session. The CLI's "could not be read" / "Store it ... and try again" error does not prove the credential is missing or invalid. Treat this as a possible execution-context limitation, not a reason to replace the token, migrate the item, or weaken Keychain access controls.

When the user's task authorizes YNAB access and the terms notice is acknowledged, retry the same resolved executable's `auth status` once outside the restricted shell through the execution tool's supported approval mechanism. In Codex, for example, with `exec_command`, use `sandbox_permissions: "require_escalated"` and explain that the check reads the stored credential internally, sends it only to `https://api.ynab.com/v1/user`, and returns authentication status without printing the token. Do not request blanket permission for other commands. Other hosts, including Antigravity, must use their own supported approval mechanism; Codex tool arguments are not portable APIs. Honor any tool approval decision; skill instructions cannot grant execution permission.

Success is exit status 0 with `authenticated: true`. Only a successful retry establishes that authentication works in that execution context. For subsequent authorized commands that need Keychain access, use the same working context through the supported approval mechanism, with each command's scope intact. This does not approve a financial write: concrete draft review, explicit user approval, and all apply/resume checks still apply.

If escalation is unavailable or denied, stop dependent API work and give the user the quoted absolute executable path followed by `auth status` to run in their normal local terminal. Ask only for the success/failure result, never the token or a Keychain dump. If the retry still fails to read Keychain, report unresolved credential access and direct the user to verify the existing service/account item in Keychain Access. An actual YNAB HTTP authentication rejection is a separate credential problem; do not keep retrying it as a sandbox failure.

Never use raw `security` commands, token exports, temporary token files, or direct API calls as a workaround. After an interrupted or uncertain write, restore access and use `drafts reconcile DRAFT_ID`; a successful auth check is not permission to replay the write.

## Monthly budget planning

Requires CLI 0.7.0 or later. Read [budget planning](references/budget-planning.md) for monthly balances, assignment drafts, review fields, and recovery. Use `budgets months` to discover months and `budgets get YYYY-MM` to read a particular month. Show Assigned, Activity, and Available separately; use the API's Available value, which can include rollover. Ready to Assign is separate from category availability.

Budget changes use `budgets draft-assign`, then the same explicit draft-review and `drafts apply` approval boundary. A request to inspect budgets or suggest a plan does not authorize assigning money. Ask for the month or intended amount when ambiguous. Do not infer personal category defaults or spending habits from repository examples.

## Read-only allocation reports

For category or reimbursement reports, use `transactions list --since YYYY-MM-DD --until YYYY-MM-DD --include-splits` (requires CLI 0.6.0 or later). It returns the same detail fields as `get`, directly from the bulk list response; do not fetch every split parent individually. Default list output only summarizes splits. Omit `--limit` for complete reports; it limits parents, not split lines. Check the command's final success before claiming completeness. A read error must remain an explicit gap, not a silently omitted transaction.

Preserve full parent and split memos, exact categories and signed amounts. For a split allocation, count only relevant child amounts, never the full bill plus its children. Keep parent context distinct from each line's evidence; an unrelated line's memo does not classify the whole bill. Confirm the intended category/person scope if ambiguous. Use `categories` to resolve category IDs/groups when needed.

Interpret reimbursement wording in context, including clear spelling variants. Distinguish explicit reimbursement, explicit gift/non-reimbursement, and no mention/uncertain wording; no mention does not prove a gift. A gift-card purchase is not automatically a gift, and a merchant refund is not automatically a family reimbursement. Preserve refunds/inflows separately from gross spending and explain any net totals. Memos alone do not prove whether a reimbursement remains outstanding. Show the supporting memo text and disclose unresolved classification rather than forcing it.

## Match the transaction

Preserve the complete transaction ID returned by YNAB. Posted transactions originating from a schedule can have underscore/date suffixes; the suffix is part of their identity, not proof that they are future scheduled entries. Use the record's `date` for date checks. Never strip the suffix, skip such records, or substitute a recurring template ID after an error. Future scheduled templates are a separate resource not exposed by this CLI.

Use explicit inclusive date bounds, an exact account, and an exact amount:

```text
transactions match --since YYYY-MM-DD --until YYYY-MM-DD --account ACCOUNT --outflow AMOUNT
```

Use `--inflow` for a refund or other inflow. Require `match_status: unique`; `ambiguous` and `not_found` are not writable matches. A display limit never changes `candidate_count`. Merchant resemblance alone is insufficient. Then run `transactions get TRANSACTION_ID` and inspect the authoritative full record, including split lines.

Resolve account, category, and payee names exactly. Use an ID or `Group: Category` if a name is ambiguous. Hidden, deleted, and internal categories/groups and Credit Card Payment categories cannot be selected for receipt allocations. Transfers are outside the receipt workflow.

## Receipt proposals

Before composing parent or category-line memos, read [memo guidance](references/memos.md).

Translate the receipt and the user's instructions into proposal version 1. Store it only in a private local JSON file under the local application directory or `/private/tmp`, owned by the current user with mode `600`. Do not put it in source control.

Required proposal content:

- selected `plan_id` and exact `transaction_id`;
- expected `account_id`, ISO date, and signed `amount_milliunits` from YNAB;
- a receipt reference, exact positive total, match evidence, and an empty `unresolved` array;
- optional signed receipt `components` and at most one positive `tender` entry in the `tenders` array, each reconciling to the total when supplied;
- one or more positive category allocations whose sum equals the total, each with an exact category, memo, and evidence;
- optional parent memo and `approve_after_verification`.

Do not invent missing receipt amounts. Preserve discounts and returns as negative components and tax or tips as positive components. Use the plan's currency precision; never use a rounding residual to conceal a discrepancy.

Multiple tenders are currently blocked: do not omit a known payment to make the proposal pass. Mixed purchase/return allocations requiring opposite category signs are also unsupported. Positive allocation magnitudes inherit the parent's sign, so pure refunds are supported. Item-level tax attribution still requires receipt evidence; separately balanced component and category totals do not establish correct attribution.

For the exact JSON field layout, read [the proposal reference](references/proposal.md) when preparing a proposal.

Run `transactions assess-receipt TRANSACTION_ID --proposal PATH` first. Follow its route:

- `draft_split_available`: create `transactions draft-receipt-split ...`.
- `existing_split_matches`: when approval is requested, create `transactions draft-receipt-approval ...`.
- `manual_split_correction_required`: show the exact requested versus current lines. Existing split lines must be corrected in YNAB, then re-read and reassessed.
- `already_approved`: report it. Do not silently unapprove or recategorize it.

Pending bank transactions excluded from the API cannot be processed. A receipt with no unique transaction match must not create a substitute transaction.

## Explicitly requested new transactions

Only if the user specifically requests creation, use `transactions draft-receipt-create --account ACCOUNT --payee PAYEE --date YYYY-MM-DD --outflow AMOUNT --proposal PATH` (or `--inflow` for a refund). Resolve the existing non-transfer payee exactly.

If the requested payee does not exist, ask whether the user wants to create it, showing the exact proposed name. Wait for their answer; a request to add a transaction alone does not authorize a new payee. If they already explicitly agreed to create that exact payee in this conversation, use that answer without asking again. If they decline, leave it uncreated and ask for an existing payee or stop the entry. Do not substitute a similar merchant or treat ambiguous matches as missing.

After the user agrees, replace `--payee PAYEE` with `--new-payee "EXACT CONFIRMED NAME"`. This prepares a local draft only. Review the new-payee intent together with the full transaction draft, and obtain approval before applying. YNAB creates or resolves the payee in the transaction request; no standalone payee is written during drafting. New names must be 1-500 printable characters without surrounding whitespace; transfer payees are unsupported. If a matching payee appears before application or during a rate-limit wait, resolve it and review a fresh draft using `--payee`.

In the proposal, set `transaction_id` explicitly to null and set expected account/date/signed amount to the requested new entry. Record the explicit creation request, duplicate-check evidence, and any confirmed new-payee name in `receipt.match_evidence`. This is not a fallback from an ambiguous or missing transaction match. The new-payee option is supported for explicitly requested new receipt transactions; do not create a replacement transaction to change an existing transaction's payee.

Application verifies the exact new-payee name and its API-assigned ID before approval, and retains that ID for recovery. A mismatch blocks approval. After an uncertain creation, reconcile the existing draft; never recreate either the payee or transaction speculatively.

## Generic transaction updates and creation

When you need to make a simple update or create a simple transaction without dealing with receipt components, proposals, or splits, use the generic commands:

```text
transactions draft-create --account ACCOUNT --date YYYY-MM-DD --outflow AMOUNT ...
transactions draft-update TRANSACTION_ID --category CATEGORY --memo "Updated memo"
```

These generic commands skip the receipt-assessment conversion path. You can edit specific fields (such as `--memo`, `--clear-memo`, `--category`, or `--uncategorized`) for an existing transaction without altering its other properties.

If an existing transaction has the wrong payee and you need to assign a new, non-existent merchant, you can use `--new-payee "CONFIRMED NAME"` with `draft-update` to explicitly rename the payee. Always review the drafted changes with the user before applying.

## Draft review and application

All writes remain draft-first. Present the concrete draft's transaction, receipt evidence, parent memo, split categories, amounts, per-category memos, and approval intent. Apply only after the user explicitly approves that draft:

```text
drafts apply DRAFT_ID --confirm DRAFT_ID
```

Receipt split application saves categorization while unapproved, reads it back, verifies transaction identity, merchant, account, date, total, cleared/deletion/transfer/import state, parent memo, and every split, then separately approves and verifies the final state. A mismatch blocks approval. An approval failure does not repeat conversion or creation.

Use `drafts reconcile DRAFT_ID` after an uncertain or interrupted write. It only reads current API state. Recovery applies to conversions, new creations, and approvals of existing splits. If categorization is correct but approval is pending, present that state and use `drafts resume DRAFT_ID --confirm DRAFT_ID` only after the existing draft remains authorized. Never blindly retry an uncertain creation or conversion. A lost creation response without a known ID requires independent evidence; never guess the ID from the amount alone.

Apply and resume both enforce the 24-hour expiry. Reviewed content is sealed; changes require a new reviewed draft. Older unsealed drafts cannot be applied, but do not automatically replace an uncertain creation. Local locks coordinate ynab-agent-skill callers, but another YNAB client can still change a transaction after its final read. Do not expose the private audit wholesale.

The CLI does not expose deletion. Do not bypass that boundary with a raw API call.
