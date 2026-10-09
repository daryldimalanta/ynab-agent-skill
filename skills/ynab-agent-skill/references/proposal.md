# Receipt proposal version 1

Keep actual proposals in private local storage, not this repository. The following is a fictional schema example with placeholders, not a usable financial instruction.

```json
{
  "version": 1,
  "plan_id": "EXACT_PLAN_ID",
  "transaction_id": "EXACT_EXISTING_TRANSACTION_ID",
  "expected": {
    "account_id": "EXACT_ACCOUNT_ID",
    "date": "2001-01-30",
    "amount_milliunits": -43250
  },
  "receipt": {
    "reference": "Local receipt reference",
    "match_evidence": "Verified account, date, merchant and exact total; unique candidate",
    "total": "43.25",
    "components": [
      {"label": "Example Item A", "amount": "30.00"},
      {"label": "Example Item B", "amount": "10.00"},
      {"label": "Tax on Example Item B", "amount": "3.25"}
    ],
    "tenders": [{"label": "Matched account payment", "amount": "43.25"}],
    "unresolved": []
  },
  "parent_memo": "Receipt reviewed",
  "allocations": [
    {"category": "EXAMPLE_GROUP: CATEGORY_A", "amount": "30.00", "memo": "Example Item A", "evidence": "Example Item A lines"},
    {"category": "EXAMPLE_GROUP: CATEGORY_B", "amount": "13.25", "memo": "Example Item B and tax", "evidence": "Example Item B lines plus their stated tax"}
  ],
  "approve_after_verification": true
}
```

Use exact decimal strings, not JSON floats. Amounts must respect the plan's currency precision. `expected.amount_milliunits` is a signed integer; allocation amounts and the receipt total are positive magnitudes. Components may be negative for discounts/returns. Each supplied components/tenders list and the allocations must reconcile exactly to the total. Multiple tenders and mixed-sign category allocations are not supported.

The reference, match evidence, allocation memo and allocation evidence are required nonempty strings. All parent/line memos are limited to 500 characters. Omit `parent_memo` or set it null to preserve the existing parent memo; an empty string explicitly clears its text. Set approval intent explicitly even though the schema defaults it to true.

For explicitly requested creation only, `transaction_id` must be null, and expected account/date/amount must match the creation arguments. Match evidence must describe the creation request and duplicate check rather than falsely claim an existing match. If a missing payee was explicitly confirmed, include its exact name and that confirmation in match evidence; pass the name with `--new-payee` instead of `--payee`. Payee intent is sealed in the generated draft, so no new proposal-schema field is needed. Agreement to create a payee does not replace approval of the concrete transaction draft.

The validator checks arithmetic and known API constraints. It cannot prove that receipt interpretation, the category guidance, or tax attribution is correct. Keep unresolved evidence in `receipt.unresolved`; any entry blocks application.
