# Validation

## Current source: agent-neutral naming — 2026-10-06

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

## Limits

These results validate the exercised offline behavior and targeted fault sensitivity. They do not prove model receipt interpretation, live API behavior, complete coverage, or production readiness. The [interpretation acceptance cases](validation/RECEIPT_ACCEPTANCE_CASES.md) and live pilot were not run. No installed-skill update was performed. Test controls must pass before mutation failures are counted as detection; setup/import errors do not qualify.

See [README.md](README.md#offline-verification) for reusable commands and [PRIVACY_REVIEW.md](PRIVACY_REVIEW.md) for distribution boundaries. Only synthetic source evidence belongs in this report.
