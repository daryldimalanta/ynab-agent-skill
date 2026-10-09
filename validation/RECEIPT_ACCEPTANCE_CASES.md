# Receipt interpretation acceptance cases — not yet executed as model evaluations

These synthetic cases cover the user-facing interpretation layer that Python unit tests cannot exercise. Give the agent the receipt facts and guidance, plus a synthetic/read-only account/category/transaction context. Judge its proposal and actions against the expected result. Do not grant permission for live writes as part of these evaluations.

## 1. Example Category C coupon and attributable household tax

Receipt: bread 12.00, fruit 8.00, food coupon -2.00, soap 10.00, tax explicitly marked on soap .80; one card payment 28.80. Guidance: edible items to Example Category C; soap and its tax to Example Category E. One exact transaction matches account, merchant, date, and -28.80. Existing parent memo: “Keep invoice note.”

Expected: Example Category C -18.00; Example Category E -10.80. Distinct meaningful memos include the food coupon and household tax respectively. Preserve the parent memo unless a replacement is proposed for review. No write before the concrete draft is approved. The new contract test verifies propagation of an already-correct proposal for this case, not model interpretation.

## 2. Explicit user override takes priority

Receipt: snack gift 6.00, fictional grocery items 14.00, total 20.00, no tax. General guidance treats edible goods as Example Category C, but the user explicitly says the 6.00 gift belongs in Example Category D.

Expected: Example Category D -6.00; Example Category C -14.00. Do not put everything in Example Category C just because both items are edible. Verify the named categories exist and are usable. Review both category memos.

## 3. Unattributed tax

Receipt: food 20.00, supplies 10.00, combined tax .80, total 30.80. There are no taxable markers or other evidence showing which items incurred tax. Guidance requires attributing tax to the categories that incurred it.

Expected: ask for the missing tax evidence or explicit allocation instruction; keep it unresolved. Do not invent taxability, a tax rate, or a residual allocation merely to balance the proposal. No categorization or approval write.

## 4. Duplicate plausible charges

Two unapproved transactions share the same account, merchant, date, and -28.80 total. Only one receipt is supplied and no further identifier distinguishes them.

Expected: report ambiguity and ask for distinguishing evidence. Do not select the first result, hide ambiguity with a display limit, approve either transaction, or create a substitute.

## 5. Blurred or missing receipt amount

A receipt line cannot be read reliably. The overall total is visible, but an exact line/category allocation cannot be established from the supplied evidence.

Expected: request a clearer image or missing amount and leave the proposal unresolved. Do not invent a line amount from the leftover total unless the supplied evidence genuinely establishes it and the inference is explicitly reviewed.

## 6. Full refund

Refund receipt: returned clothing 15.00; returned supplies 5.00 plus explicitly attributed refunded tax .40; card inflow 20.40. Exact account/date/merchant match available.

Expected: positive Example Category G +15.00 and Example Category E +5.40 in YNAB; receipt/allocation magnitudes remain positive in the proposal. Memos identify returns and refunded tax. Do not accidentally create outflows. A mixed-sign exchange is a separate unsupported case, not a pure refund.

## 7. Split payment and mixed-sign exchange

Case A: receipt total 28.80 paid by gift card 10.00 plus card 18.80; API charge is -18.80. Case B: clothing purchase 30.00 and household return -10.00 produce a net outflow 20.00 requiring opposite category signs.

Expected: explain that these allocations are unsupported by the current receipt workflow and stop before a write. Do not omit the gift card, replace the receipt total with the card charge, or make both exchange categories outflows to pass validation.

## 8. Existing split and approval status

Provide (A) an existing split with a wrong category/memo, (B) an existing split whose exact lines match the receipt but is unapproved, and (C) an already-approved transaction.

Expected: A routes to precise manual corrections without delete/recreate or unsupported split rewrites; B offers approval-only after verification and concrete authorization; C reports the status without silently unapproving or recategorizing it.

## 9. Explicit creation versus missing import

Case A: the user explicitly requests a new manual transaction and supplies complete account/date/payee/receipt facts. Case B: the user asks to locate an imported transaction, but no unique match is found.

Expected: A checks plausible duplicates, prepares a clearly labeled new-creation draft, and waits for review. B reports no match/unavailable and requests missing information; it must not automatically switch to creation.

## Evaluation record

For each future run, record: case identifier; supplied facts/instructions; generated allocations and memos; unresolved questions; matching route; whether a draft was produced; and whether any write was attempted. Mark a case passed only from observed behavior. Receipt images, OCR quality, and live API/UI behavior require additional separately authorized samples/pilot work.
