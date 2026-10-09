# Monthly budget planning

Use CLI 0.7.0 or later after the skill's terms, authentication, and exact-plan checks. These commands apply to the selected plan; `plans list` and `plans use` identify or select a whole YNAB plan. Budget commands address monthly allocations within it.

## Read and explain

Prefix these examples with the configured `ynab-agent-skill` executable:

```text
budgets months
budgets get 2001-02
budgets get 2001-02 --category "Example Group A: Example Category A"
```

The example month and categories are fictional. Resolve the user's requested month explicitly as `YYYY-MM` or `YYYY-MM-01`; do not silently use the machine's current month. Future months can be queried when YNAB exposes them. A missing month is an explicit limitation, not a zero budget.

Translate structured output into a readable table with Category, Assigned, Activity, and Available. Include the selected plan and month, Ready to Assign, and any filter or failed read. Use the returned plan `currency_format` and exact decimal `currency` strings; `milliunits` are integers representing thousandths of a currency unit. Do not assume the plan uses dollars.

- **Assigned** is the month's net assignment, not a spending target.
- **Activity** is signed net activity; refunds can increase it. It is not necessarily gross spending.
- **Available** is the API's category balance, including rollover and YNAB's rules. Never substitute Assigned plus Activity, which can omit rollover. A negative value is flagged as overspent.
- **Ready to Assign** is returned for the whole month and is separate from category availability. Month totals remain whole-month totals even when `--category` filters the rows. Do not sum category balances and label that sum Ready to Assign or a bank balance.
- Hidden categories remain visible in reads with flags. Deleted categories are omitted. A missing goal value means unavailable or not configured, not zero. Goal underfunding is the API's value and may differ from the app's future-month target presentation.

## Prepare and review assignments

```text
budgets draft-assign 2001-02 --category "Example Group A: Example Category A" --assigned 81.25
budgets draft-assign 2001-02 --category CATEGORY_ID --increase 17.25
budgets draft-assign 2001-02 --category CATEGORY_ID --decrease 17.25
```

Choose exactly one amount option. `--assigned` sets the month's new total; `--increase` and `--decrease` calculate a new total from the observed assignment. Negative totals use `--assigned=-AMOUNT`; zero is allowed. Currency precision is enforced. Drafting saves local private intent and makes no API write.

Present the draft ID, exact plan, month, category/group, current assignment, proposed assignment, difference, current Activity and Available, and estimated Available and Ready to Assign afterward. Surface negative estimates clearly. Estimates assume no intervening changes and are not authoritative readback. A decrease can return funds to Ready to Assign; an increase consumes it. A negative estimate is a review concern, not permission to rebalance other categories automatically.

Wait for explicit approval of that concrete draft. Then use its returned command:

```text
drafts apply DRAFT_ID --confirm DRAFT_ID
```

Application checks the review seal, 24-hour age, selected plan, current monthly figures, category identity/status, and currency configuration. A changed review snapshot requires a fresh draft and approval. The PATCH sets only the selected month's category assignment; it does not edit transactions, category names, or targets. Credit Card Payment assignments are supported, although those categories remain forbidden for transaction categorization. Hidden, deleted, internal, or unclassified categories cannot be adjusted.

## Verify and recover

Require `verification.verified: true` and show the authoritative readback assignment, Available, and Ready to Assign. A successful HTTP response alone is insufficient. The tool reads the exact month again; it does not trust the write response as the final balance.

After a timeout, interruption, or failed readback, use `drafts show DRAFT_ID` and `drafts reconcile DRAFT_ID`. Reconciliation makes GET requests only and can verify the current target state even after draft expiry. It does not prove which client caused that state. Do not reapply an attempted write, create a replacement draft merely to retry it, or use `drafts resume`; budget drafts have no resumable approval stage. Resolve uncertainty first, then obtain fresh approval for any new change.

Only confirmed rate-limit rejections may retry, with fresh state/expiry checks. Local draft and plan locks serialize cooperating budget commands. They do not lock the YNAB app, transaction writers, or external clients; there is no atomic compare-and-set guarantee across clients.

This release adjusts one category at a time. It does not implement an atomic transfer between categories, multi-category auto-assignment, target editing, or plan/category creation. Review a fresh snapshot after each completed change. Keep all budget output, drafts, and personal planning guidance out of public source and logs.
