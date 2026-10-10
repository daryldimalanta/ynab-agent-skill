# Validation

## Current source: Python 3.12, transaction extraction, and host installation — 2026-10-10

Version `0.7.0`; Python `3.12.7` on Darwin `25.6.0` / `arm64`. Financial runtime code and persisted draft formats are unchanged. Both hosts use the same canonical skill. Installer fixes add explicit public artifacts, genuinely read-only previews, unique per-host private backups, current-preference priority, staged byte verification and rollback across hosts on caught failures. Hard kills/power loss still require recovery from retained backups.

`env python3 -B -m tools.quality full` completed with exit 0: **222 core tests, 50 safety tests, all 28 required financial faults detected**, and the bounded consistency check passed. Seven quality-runner cases and nine installer cases were added; existing safety assertions were retained. Earlier runner integration errors were repaired before this final run. No skipped cases, collection errors or mutation setup errors were accepted as passes.

Source SHA-256: `8f996908d8f72dd8513985ed08bfd53843d8524d96d8d09eda3c3e6937ce2b44`; base revision `d6806971d4e723a6aebd2a09a0f51c092c473068` plus the tested working changes. This digest uses `tools/source_manifest.txt` and `tools.consistency.digest`: sorted relative names, NUL, exact file bytes, NUL. It includes the manifest and new untracked source; excludes this historical/current report and the privacy review. The report records completed commands, controls, faults, totals, terminal statuses, runtime and matching source identity. This reporting-only update does not change that digest.

Canonical source and both installed copies pass Skill Creator validation. All seven public installed files match source for each host; private preference bytes and mode 600 were preserved, skill roots remain mode 700, and neither host retains an active legacy skill. Help/version launchers agree. Scoped source privacy scanning and whitespace checks passed; this is not a new audit of all published history.

The Python guard restricts accidental access during reviewed offline checks; it is not a hostile-code OS sandbox. File installation does not establish native UI discovery or interpretation quality. Model acceptance scenarios and a live financial pilot remain unrun. Packaging/runtime modernization and the larger transaction-workflow extraction were completed; this shared-host milestone does not claim the entire engineering plan is implemented. No publication or live financial operation was performed.

## Historical source: monthly budget planning — 2026-10-08

Version `0.7.0`; Python `3.9.6` on macOS `26.7.1` / arm64. Monthly reads report assigned, activity, authoritative available balances, Ready to Assign and currency metadata. Single-category assignment drafts use the existing seal format with confirmation, expiry, local draft/plan locks, stale-state and rate-wait revalidation, durable checkpoints and GET-only reconciliation. Existing transaction behavior and private-state identities are retained.

| Check | Observed result |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -q` | 205 tests passed; exit 0 |
| `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s validation -q` | 50 tests passed; exit 0 |
| `python3 -m validation.audit_mutation_checks` | Passing 118-test control; 7/7 faults detected; exit 0 |
| `python3 -m validation.audit_new_payee_checks` | Passing 33-test control; 4/4 faults detected; exit 0 |
| `python3 -m validation.audit_transaction_read_checks` | Passing 20-test control; 10/10 faults detected; exit 0 |
| `python3 -m validation.audit_budget_planning_checks` | Passing 31-test control; 7/7 faults exercised and detected by assertions, zero errors; exit 0 |
| Skill Creator `quick_validate.py skills/ynab-agent-skill` | Passed; validation-only PyYAML dependency |
| Canonical/legacy version, budget command help, Python syntax, relative documentation links and diff whitespace | Passed |

The 31 new budget tests use fictional API responses through the real HTTP serializer and CLI. Existing regression assertions were retained; the mutation-gate tests now also exercise the new runner. Budget tests cover monthly versus current-category figures, rollover, signs and precision, explicit confirmation, seals, changed state, locks/checkpoints, expiry, delayed writes, readback failures and recovery without replay. All 255 ordinary tests passed. These checks do not prove absence of every possible regression.

Tests used isolated local state with a Python guard blocking network, credential and unrelated private-file access. Installer tests ran the reviewed local script only against synthetic temporary homes. All four mutation diagnostics used the same guard. No live account or financial operation was performed.

Source SHA-256: `4023360937acf3e1b9b760665b9842318a329387006b8deae180fcc891281b2b`. This identifies the final distribution source using the procedure below, including new source files. Subsequent changes to this report are reporting only. Runtime smoke checks used help/version only. Native host discovery, model interpretation and live budget behavior were not tested; the [budget acceptance cases](validation/BUDGET_ACCEPTANCE_CASES.md) are an unexecuted evaluation plan. Atomic multi-category transfers and target editing are not implemented.

## Historical source: agent-neutral naming — 2026-10-06

Version `0.6.1`; Python `3.9.6` on macOS `26.7.1` / arm64. Project, canonical command and skill identity are `ynab-agent-skill`. The legacy command is a forwarding alias. Financial algorithms, credential labels, private-state paths, review seals and schemas are unchanged; runtime edits are naming strings and the package version.

| Check | Observed result |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -q` | 167 tests passed; exit 0 |
| `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s validation -q` | 50 tests passed; exit 0 |
| Skill Creator `quick_validate.py skills/ynab-agent-skill` | Valid frontmatter/naming; exit 0; PyYAML used only as a validation dependency |
| New and legacy commands through both configured launcher locations, from a different working directory | 12 checks passed: identical help/version and invalid-option handling; help/version exit 0, invalid option exit 2 |
| Synthetic draft command, API user-agent and compatibility identities | Canonical command generated; package version consistent; credential/state/terms identities preserved |
| Syntax, documentation links, standalone licenses and UI metadata | Passed; legacy skill folder absent; unchanged safety test source |

The test and launcher checks used local guards blocking network, credentials and private financial files. No live financial state was read. Source SHA-256: `dc39b0580583ba004d4683d73e8f218a3a3f9da2cb0cb21eb0722a42c46160fd`; use the distribution-file hashing procedure below. Only the reporting document changed after the checks.

The targeted mutation diagnostics were not rerun for naming-only edits; their results below apply to the earlier source snapshot. Model interpretation, native Antigravity discovery, an end-to-end Antigravity run and a live pilot remain unverified. This change does not install or activate a renamed skill.

## Verified source snapshot — 2026-10-06

Version `0.6.0`; Python `3.9.6` on macOS `26.7.1` / arm64. The checks below completed with exit status 0 against the same source snapshot. All data was synthetic; an additional local process guard blocked network access and private financial/credential paths, including in test child processes.

| Command (preceded by `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3`) | Observed result |
| --- | --- |
| `-m unittest discover -s tests -q` | 167 tests passed |
| `-m unittest discover -s validation -q` | 50 tests passed |
| `-m validation.audit_mutation_checks` | Unmodified control: 118 tests passed; all 7 faults detected by assertions, zero errors |
| `-m validation.audit_new_payee_checks` | Unmodified control: 33 tests passed; all 4 faults detected by assertions, zero errors |
| `-m validation.audit_transaction_read_checks` | Unmodified control: 20 tests passed; all 10 faults exercised and detected by assertions, zero errors |

The 217 ordinary test cases retain their safety assertions. Python syntax, relative documentation links, standalone license parity, and diff whitespace checks passed. Fixture rules are in [tests/FIXTURES.md](tests/FIXTURES.md).

Source identity: SHA-256 `6ac0cbd6feb149a5500211350aeecc7125d7d6bcdd562cdb4ce683a911992e9c`. To reproduce it, enumerate every distribution file except `VALIDATION.md` and `PRIVACY_REVIEW.md`; form sorted `[relative_posix_path, file_sha256]` pairs; encode their JSON with separators `(',', ':')` and no trailing newline; hash those UTF-8 bytes. These two report files are excluded to avoid circular evidence hashes. Their final changes are reporting only.

## Limits of historical results

These results validate the exercised offline behavior and targeted fault sensitivity. They do not prove model receipt interpretation, live API behavior, complete coverage, or production readiness. The [interpretation acceptance cases](validation/RECEIPT_ACCEPTANCE_CASES.md) and live pilot were not run. No installed-skill update was performed for those historical snapshots. Test controls must pass before mutation failures are counted as detection; setup/import errors do not qualify.

See [README.md](README.md#offline-verification) for reusable commands and [PRIVACY_REVIEW.md](PRIVACY_REVIEW.md) for distribution boundaries. Only synthetic source evidence belongs in this report.
