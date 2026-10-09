# Synthetic financial fixtures

All accounts, merchants, category/group labels, identifiers, receipt amounts, dates, and memos used by the tests are fictional examples. They are not a plan export or a record of any contributor's spending. Example names and fixed dates in 2001 make the main fixture families visibly illustrative; other numeric boundary values exercise precision and validation rules.

Category IDs and labels test exact lookup, ambiguity, blocked categories, signed allocations, and readback. They are not merchant rules or personal category defaults. `Credit Card Payments` and `Split` are service concepts exercised by safety tests, not a personal category list.

Use invented test data. Do not copy real receipts, merchant-specific purchases, personal memo conventions, account identifiers, or a household's category taxonomy into fixtures. Preserve arithmetic, ordering, precision, memo propagation, confirmation, concurrency, recovery, and no-write assertions when changing illustrative values.

The cases under `validation/` and the proposal reference use the same synthetic-data boundary. Interpretation evaluations and live pilots require separate evidence and authorization.
