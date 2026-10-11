# ynab-agent-skill

An unofficial YNAB agent skill backed by a local API wrapper for reviewing monthly budgets and remaining balances, adjusting approved category assignments, reviewing receipts, and managing transactions. Shared instructions are intended for Codex and Antigravity. The project, canonical command, and skill are named `ynab-agent-skill`. The legacy command `ynab-codex-skill` forwards to the same implementation.

## ⚠️ Disclaimer

**Use at your own risk.** This project is not affiliated with, endorsed, or officially supported by YNAB or OpenAI. Use of this OAuth App could introduce problems with your budget that YNAB official support channels will not be able to troubleshoot or fix. The software is provided "as is", without warranty of any kind. Please review all automated proposals carefully before authorizing any writes to your budget.

The current version supports exact transaction discovery, full single-record and bulk split reads (including posted recurring entries), receipt allocation validation, conversion of an unapproved unsplit transaction into category splits, explicitly requested new split transactions with confirmed missing payees, readback verification, guarded approval, and automatic persistent rate-limit handling. `--version` derives from `budget_tools/__init__.py`.

Offline regression results are recorded in [VALIDATION.md](VALIDATION.md). Receipt interpretation acceptance and a controlled live pilot remain pending. Python tests of authored proposals do not evaluate receipt images or an agent's interpretation.

For a codebase walkthrough, read [Project structure and architecture](ARCHITECTURE.md). It maps the modules, command flow, draft/recovery records, private storage, test coverage, and where to make changes.

## Local setup

This checkout uses only the Python standard library at runtime and supports Python 3.12 on macOS (package requirement: `>=3.12,<3.13`). The application, installer and quality runner prefer this checkout's `.venv`; otherwise they require a supported `python3` on PATH. `.python-version` alone does not configure a system interpreter. The verified environment uses Python 3.12.15 on Apple Silicon macOS. Other Python minor versions are not supported. Future 3.12 security patch releases are allowed; each validation report records the exact patch version exercised.

For a fresh checkout, install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```sh
uv sync --python 3.12 --locked --group dev
```

This creates a project-local environment and installs the locked development tools. It may download public Python/tool packages; it does not access YNAB. Subsequent application and test commands select that environment automatically, including when the system `python3` is too old. No system Python replacement is needed.

Validation targets macOS with local APFS/HFS storage; Linux and Windows are not supported or tested. The tested interpreter and macOS version are recorded in the validation report.

### Installing the skill

The distributable skill is located at `skills/ynab-agent-skill`. To install it natively in Codex and/or Antigravity IDE, use the provided installer script from the checkout root:

```sh
./install.sh codex
./install.sh antigravity
./install.sh both
```

- **Prerequisites**: macOS, `bash`, and the project environment above (or Python 3.12 on PATH).
- **Native installation locations**:
  - Codex: `~/.codex/skills/ynab-agent-skill`
  - Antigravity IDE: `~/.gemini/config/skills/ynab-agent-skill`
- **What it does**: The installer copies the reviewed skill files. It preserves your existing private preferences (`references/local-preferences.md`) byte-for-byte across updates. If no preferences exist, it generates a minimal file pointing to the checkout's executable. It also handles legacy `ynab-codex-skill` migration to avoid duplicate active skills and creates safe backups in `~/.ynab-agent-skill-backups/`.
- **Dry run**: Add `--dry-run` to preview changes without modifying the filesystem.

**Note**: Installing the skill does not install a standalone application. The default CLI and Python package (`budget_tools`) remain in this repository checkout; an optional CLI wheel is described below. The installed skill finds the local executable via the `local-preferences.md` file generated during installation, or by searching your PATH. Native skill discovery by the host app may require a fresh session or reload. The installer verifies each installed public file and private preference bytes. Each host update is staged and verified before replacement. The installer keeps separate unique backups per host and restores both prior installations on a caught update failure. A process kill or power loss can require manual restoration from those backups. Only the seven explicitly listed public skill files are distributed; extra source files are excluded. Unknown or linked storage paths are rejected. It does not run a live financial workflow test.

### Using the CLI directly

From the local checkout root, you can also invoke the CLI directly:

```sh
./bin/ynab-agent-skill --version
./bin/ynab-agent-skill --help
export PATH="$PWD/bin:$PATH"
ynab-agent-skill --version
```

The `export` applies to the current shell. To use another shell or agent environment, add this checkout's absolute `bin` directory to that environment's PATH, or invoke the quoted absolute executable path. Keep the launcher beside the package; copying the launcher alone will not install the application. Help and version do not read credentials or private application state.

Memo conventions are described in [memo guidance](skills/ynab-agent-skill/references/memos.md). Personal defaults may be kept in `references/local-preferences.md` inside the installed skill, owned by the user with mode `600`; keep the installed directory private. That optional file is not distributed, and its source-tree counterpart is ignored. Preference files never grant financial-write authorization.

## Credentials and private state

For credential compatibility, the macOS Keychain generic-password item retains service `budget-tools-for-ynab` and account `personal`. These are lookup labels, not a token. Existing users need no credential migration. For a new setup, the user should create the item in Keychain Access themselves; never put its password in a command, repository, proposal, or chat.

Private state remains under `~/Library/Application Support/Budget Tools for YNAB`. This includes notice acknowledgment, selected plan, drafts, recovery state, locks, and private audit records. The internal Python package remains `budget_tools`. Renaming the command does not reset or migrate either storage location.

State access uses pinned directory descriptors, no-follow checks, ownership checks, private modes, atomic writes, and flushes. Only recognized local APFS/HFS destinations within the user Library or `/private/tmp` are accepted. Unknown/nonlocal filesystems, symlinked directories, and unsafe files are rejected. User storage restrictions still apply.

Normal API use sends the requested fields and authentication to the fixed `https://api.ynab.com/v1` origin. Authenticated redirects are rejected. No telemetry, analytics, remote logging, or receipt-image upload is implemented. CLI results can contain private financial data: keep output, proposals, and logs out of source control. Review memos and other fields before authorizing their transmission to YNAB.

Before live access, the user must review `ynab-agent-skill terms status` and personally run `ynab-agent-skill terms acknowledge` if necessary. An agent must never acknowledge on their behalf. After that, `auth status` checks API access, and `plans list` / `plans use PLAN_ID` select an exact plan. These are live-use instructions, not packaging checks.

An agent's restricted shell may be unable to read a Keychain item that is accessible in a normal local session. A Keychain lookup error alone does not establish that the token is missing or invalid. For authorized YNAB access, retry the same executable's `auth status` once using the execution tool's approved escalation mechanism; if unavailable or denied, have the user run that command in their normal terminal and report only success/failure. The CLI sends the stored token only to YNAB and prints authentication status, not the token. Do not export credentials, change Keychain controls, or replay an uncertain write to resolve this limitation. See [restricted-shell Keychain recovery](skills/ynab-agent-skill/SKILL.md#restricted-shell-keychain-recovery) for success criteria, remaining failures, and subsequent command handling.

## Where categories and allocation decisions come from

Publishing the source does not publish a user's YNAB plan. Each installation uses its own local Keychain token and privately saved plan selection. `ynab-agent-skill categories` requests the selected plan's current category groups from YNAB and returns category IDs, group/name, and status flags. It does not read a category list embedded in the repository. Category names, IDs, dates, amounts and merchant/memo examples in tests are fictional; see [fixture rules](tests/FIXTURES.md).

The agent uses the retrieved categories, receipt evidence, and the user's explicit instructions to propose allocations. The Python wrapper does not contain a learned categorization model or a private merchant-to-category rules table. A proposal must identify an exact category; the wrapper fetches current categories again to resolve the name/ID and reject ambiguous or unusable choices. The user then reviews the concrete draft before any transaction write. Missing instructions or evidence require clarification, not an invented category assignment.

The repository's public source, local runtime data, and the agent conversation are separate. Category/transaction output and receipt facts provided to the agent can enter the conversation/model context; do not treat that workflow as entirely offline. The wrapper itself makes no model-service calls and does not upload receipt images. Private state and runtime output must not be attached to public issues or committed with the source.

## Read-only transaction reports

For a report that needs category allocations and full memos, request bulk details:

```ynab
ynab-agent-skill transactions list --since 2026-01-01 --until 2026-01-31 --include-splits
```

`--include-splits` uses the same detail format as `transactions get`, including parent identifiers, parent memo, and live split lines with exact signed milliunits, category/payee IDs and names, and line memos. It uses the existing list response, with no per-transaction fetch. A non-split transaction has an empty `subtransactions` array. Deleted lines are omitted; the existing `subtransaction_count` field counts raw returned lines, so use the array length for the number of displayed live lines. Default list output and `match` output are unchanged.

Date/account/type/amount filters still select parent transactions. `--limit` limits parents, not their split lines; omit it for a complete report. For category-level totals, use the matching split amounts rather than the entire parent amount, and never add both parent and child amounts. Retain both memo levels as evidence. Reimbursement/gift interpretation is the reporting agent's responsibility, not an automatic CLI classification or proof of an outstanding balance. See the skill's [reporting guidance](skills/ynab-agent-skill/SKILL.md#read-only-allocation-reports).

## Monthly budget planning

Version 0.7.0 adds monthly budget reads and reviewed category assignments. After completing the setup and selecting the exact plan, prefix these examples with `ynab-agent-skill`:

```ynab
budgets months
budgets get 2001-02
budgets get 2001-02 --category "Example Group A: Example Category A"
budgets draft-assign 2001-02 --category "Example Group A: Example Category A" --assigned 81.25
budgets draft-assign 2001-02 --category CATEGORY_ID --increase 17.25
budgets draft-assign 2001-02 --category CATEGORY_ID --decrease 17.25
```

Dates, names, and amounts above are fictional. Specify `YYYY-MM` or `YYYY-MM-01`, including for future months exposed by YNAB. Read output separates Assigned, signed Activity, Available, and whole-month Ready to Assign. Available comes from YNAB and includes rollover; it is not necessarily Assigned plus Activity. Negative availability is flagged as overspent. Amounts include integer milliunits and exact decimal strings for both programmatic use and readable agent reports. A category filter does not narrow the whole-month totals.

`--assigned` sets a new total, whereas `--increase` or `--decrease` calculates a total from the current assignment. Drafting makes no API write. Review the exact plan/month/category, before/after amounts, difference, estimated availability, and Ready to Assign impact, including negative estimates. Only after explicit approval use the returned `drafts apply ID --confirm ID` command. Application rechecks current state and reads back the final assignment and balances. Changed state requires a new review; uncertain writes must be reconciled without replay.

Budget drafts use the existing sealed private draft storage and 24-hour expiry. Budget operations have draft and plan locks, and rate-limit retries revalidate state and age. `drafts reconcile ID` only reads YNAB; `drafts resume` is unsupported for budget assignments. Single-category assignments are supported, including Credit Card Payment categories. Target editing, plan/category creation, automatic multi-category allocation and atomic transfers between categories are outside this release. For the full review and recovery contract, see [budget planning guidance](skills/ynab-agent-skill/references/budget-planning.md).

The API contracts are documented in YNAB's official [monthly endpoints](https://github.com/ynab/ynab-sdk-python/blob/main/docs/MonthsApi.md) and [category endpoints](https://github.com/ynab/ynab-sdk-python/blob/main/docs/CategoriesApi.md). No live financial operation is authorized by these examples.

## Receipt workflow

Transaction IDs are opaque identifiers. Preserve the complete ID returned by YNAB, including any underscore/date suffix on a posted recurring transaction. Do not strip a suffix, infer scheduled status from it, or substitute a recurring template ID after a failed lookup. The transaction's `date` field, which may have been edited, supplies its date. Future scheduled templates use a separate API resource that this CLI does not expose.

Prefix these command fragments with `ynab-agent-skill`:

1. Use `transactions match` with inclusive start/end dates, an exact account, and an exact signed amount. Continue only with a unique match; a display limit cannot establish uniqueness.
2. Use `transactions get ID` to read the complete transaction, including existing splits.
3. Prepare a version-1 proposal using [the proposal reference](skills/ynab-agent-skill/references/proposal.md). Keep it in private local storage with mode `600`.
4. Run `transactions assess-receipt ID --proposal PATH` and follow its route.
5. Create the routed split or approval draft. Review the account/date/amount, evidence, parent memo, categories, line memos, and approval intent with the user.
6. Apply only after explicit approval of that concrete draft, using its returned `drafts apply ID --confirm ID` command.
7. Require verified categorization or approval. Use `drafts reconcile ID` after an uncertain write; never blindly replay it.

Receipt categorization is saved while unapproved, read back, verified, then separately approved and read back again if requested. Verification covers identity, merchant, account, date, total, clearing/deletion/transfer/import state, parent memo, and all split contents. Generated split IDs and order may differ, but duplicate-line counts must match.

Existing split lines cannot be rewritten by this implementation. When assessment returns `manual_split_correction_required`, correct them in YNAB and reassess. An already-approved transaction is reported without silently unapproving or recategorizing it. Approval status differs from cleared/reconciled status and from the user's authorization of a local draft.

## Explicit new split creation

Only for an explicit request to create a new transaction:

```ynab
ynab-agent-skill transactions draft-receipt-create --account ACCOUNT --payee PAYEE \
  --date YYYY-MM-DD --outflow AMOUNT --proposal PRIVATE_JSON_PATH
```

Use `--inflow` for a pure refund. Account and a non-transfer payee are required. If the payee is missing, ask the user whether to create the exact proposed name. After they agree, replace `--payee PAYEE` with `--new-payee "CONFIRMED NAME"`. The options are mutually exclusive; names must be 1-500 printable characters without surrounding whitespace. Existing, ambiguous, inactive same-name, and transfer payees cannot be treated as new. A prior explicit agreement for the same name can be reused; declining creates nothing.

Set proposal `transaction_id` to null and expected account/date/signed amount to the requested facts. Record the creation request, duplicate-check evidence, and any confirmed new-payee name. Review the concrete draft, including its new-payee intent, before applying it. Agreement to create the payee alone does not apply the transaction. An absent or ambiguous imported transaction must never trigger substitute creation; `--new-payee` is limited to explicitly requested new receipt transactions.

Drafting performs no API writes. On application, the transaction request supplies a null `payee_id` and the confirmed `payee_name`, as supported by the [official API contract](https://github.com/ynab/ynab-sdk-python/blob/main/docs/NewTransaction.md). The CLI rechecks that the name is absent before sending and after rate-limit waits; if it appears, review a fresh draft using its existing ID. Another client can still race the final check: the API may resolve the same name instead of creating it. Exact name, returned ID, and transaction fields are verified before approval. The first acknowledged or verified payee ID is retained for subsequent readback and recovery.

A known created ID is saved before readback. If the creation response is lost without an ID, stop for independent reconciliation; amount alone cannot establish identity.

## Generic transaction creation and updates

For updates or creations without a full receipt or proposal, use the generic commands:

```ynab
ynab-agent-skill transactions draft-create --account ACCOUNT --date YYYY-MM-DD \
  --outflow AMOUNT --payee PAYEE --category CATEGORY --memo "Optional memo"
ynab-agent-skill transactions draft-update TRANSACTION_ID --category CATEGORY
```

These generic commands skip the receipt-assessment conversion path. You can edit specific fields (e.g., `--memo`, `--clear-memo`, `--category`, `--uncategorized`) for an existing transaction without affecting others. To fix an unknown merchant, use `--new-payee "CONFIRMED NAME"` to create the missing payee and assign that transaction to it (this does not rename a shared payee record). As always, changes must be reviewed as a local draft before being applied with `drafts apply`.

## Arithmetic, recovery, and limits

The CLI tracks YNAB's 200-request rolling hour in private, token-specific state shared across local processes. It reserves capacity before sending, incorporates response usage headers, and automatically waits/retries confirmed HTTP 429 rejections for as many hours as needed. A valid `Retry-After` is honored; otherwise the cooldown is one hour plus a small margin. Wait notices go to stderr and sleeps are interruptible. Other applications using the same token may consume unobserved capacity, so an initial 429 remains possible. See [long-running work and recovery](skills/ynab-agent-skill/references/long-runs.md) for batch checkpoints and restart instructions.

Delayed mutations revalidate the target and check draft expiry before sending. A definite 429 saves a retryable pending state; uncertain writes are never automatically replayed. Authentication errors stop normally: this tool does not refresh OAuth credentials. The waiting process requires no model calls or human retry, but the CLI is not a background scheduler; persisted accounting and draft state support manual resumption after the process exits.

- Exact decimal strings, currency precision, 500-character parent/line memos, category totals, and required evidence are checked locally. Omitted/null parent memo preserves it; an empty string clears it.
- Discounts, tax, tips, and returns may be signed components. Their net, supplied tender, and allocations must each match the receipt total. Arithmetic cannot prove category or tax attribution; unresolved evidence blocks drafting.
- Ordinary outflows and pure refunds are supported. Allocation magnitudes inherit the transaction's sign. Mixed-sign category allocations and multiple tenders are blocked. Do not omit known payments to pass validation.
- Hidden/deleted/internal categories and groups, Credit Card Payment transaction categorization, receipt transfers, and pending bank transactions absent from the API are unsupported. Active Credit Card Payment budget assignments are supported. Deletion is not exposed.
- Apply and resume enforce a 24-hour expiry. Reviewed fields are sealed against accidental changes, not a malicious same-user process replacing both data and checksum.
- `drafts reconcile ID` only reads API state. `drafts resume ID --confirm ID` can send remaining approval when the existing draft is valid and still authorized; it does not repeat conversion or creation.
- Old unsealed drafts need reassessment. Never automatically replace an uncertain creation or edit private JSON to bypass recovery checks.
- Local locks serialize both the same draft and different drafts targeting the same transaction. They cannot lock YNAB's UI or other clients; there is no cross-client atomicity guarantee.
- Unique-match and duplicate-check evidence, truthful free-text evidence, and human authorization partly depend on the operator/skill. Automatic waits/retries execute only the current authorized operation; arbitrary unattended financial decision-making is outside the validated scope.

## Offline verification

From the checkout root, use the same checked-in runner in either agent:

```sh
python3 -B -m tools.quality quick
python3 -B -m tools.quality full
```

`quick` runs source consistency and both ordinary suites. `full` also runs all four mutation diagnostics. The runner uses synthetic temporary state and a Python guard inherited by child processes; it blocks accidental network, Keychain, existing home-file and private application-state access. Installer subprocesses are restricted to synthetic homes. This is a guard for reviewed Python tests, not an operating-system sandbox against hostile or native code. Reports go only to ignored `tmp/quality/`, with source revision/digest, interpreter/platform, commands, exit status and structured results; raw test output and private values are excluded. Confirm a saved report still matches the source with `python3 -B -m tools.quality full --check-report tmp/quality/REPORT.json`. A quick report cannot satisfy a full-report check.

Maintain `tools/source_manifest.txt` when adding source or instruction files. Every public source directory is scanned without following links; only explicitly named generated reports and build/cache directories are excluded. Use `ynab` fenced blocks for parser-checked CLI examples in README, skill and references; placeholders are parsed as arguments, trailing `...` denotes optional additional flags, and unknown commands or malformed quoting fail the check. Examples are never executed. The checker verifies the installer allowlist, canonical version, standalone license, discovery metadata, selected CLI shapes and current-document file links. It does not validate every prose claim or replace model interpretation evaluations. Historical reports are excluded from the source digest.

For focused development, explicitly use `.venv/bin/python` with the underlying commands below; these alone do not activate the offline guard:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s validation -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m validation.audit_mutation_checks
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m validation.audit_new_payee_checks
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m validation.audit_transaction_read_checks
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m validation.audit_budget_planning_checks
```

Fixtures are synthetic. Both test directories must be run. The general mutation diagnostic injects seven faults in memory; the new-payee diagnostic injects four, the transaction-read diagnostic injects ten, and the budget-planning diagnostic injects seven. All require a passing unmodified control and exit nonzero for missed faults or test errors. Assertion failures from an exercised fault are detection evidence, not normal regressions; loader/fixture errors do not count. The [receipt acceptance checklist](validation/RECEIPT_ACCEPTANCE_CASES.md) and [budget acceptance checklist](validation/BUDGET_ACCEPTANCE_CASES.md) are separate from automated tests. No live integration test is run automatically.

## Developer checks and packaging

Run `python3 -B -m tools.dev_checks` after environment setup. It applies correctness lint to all Python sources, formatting checks to the new workflow/tool modules, and strict typing to the transaction execution boundary and shared errors. The scope is explicit in `pyproject.toml` and `tools/dev_checks.py`; this does not claim the older modules have been fully formatted or strictly typed.

The supported library execution interface is `budget_tools.transaction_workflow.execute(store, draft_id, client_factory=..., command="apply", confirm=draft_id)`. It owns terms, sealed intent, draft/target locks, status, expiry, stale-state checks, durable progress, readback and recovery. Use `command="reconcile"` for API reads only or `command="resume"` for remaining receipt approval only. The CLI delegates to it. Private helpers ending `_locked` are not supported entry points. Confirmation remains an operator assertion; it does not authenticate human approval. Monthly assignments use `budget_tools.planning.execute`.

An optional wheel packages the CLI and `budget_tools` only; the skill still uses the reviewed folder installer. Build and inspect it locally:

```sh
uv build --wheel --out-dir /private/tmp/ynab-public-wheel
.venv/bin/python -B -m tools.check_wheel /private/tmp/ynab-public-wheel/ynab_agent_skill-0.7.0-py3-none-any.whl
```

Replace the filename with the version reported by the build. `pyproject.toml` derives it from `budget_tools/__init__.py`. The checker rejects extra/missing files and requires exact package source bytes, license, metadata and both command aliases. Private state, preferences, tests, handoffs, receipts and host installations are excluded. Installing a wheel into a separate virtual environment does not migrate credentials, state, host preferences or executable locations. Inspect the exact artifact again after source changes; no build command publishes it. Only macOS behavior has been verified even though Python wheel metadata uses a generic platform tag.

## Distribution status

Source releases and local checkout changes are separate. A local passing report does not mean those bytes have been committed or published; inspect the checkout revision and diff before distributing it. See [PRIVACY_REVIEW.md](PRIVACY_REVIEW.md) for the reviewed scope and publication boundaries.

## License

Copyright (c) 2026 Daryl Dimalanta. This project's code, skill, and documentation are licensed under the [MIT License](LICENSE).

When redistributing copies or substantial portions, retain the copyright and license notices. The standalone skill includes its own identical [license copy](skills/ynab-agent-skill/LICENSE.txt) so those notices accompany it when installed separately.
