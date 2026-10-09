# Budget planning interpretation acceptance cases

These are synthetic model-level scenarios, not claims of a completed live pilot. Run them against the shared skill in each host when evaluating native behavior. Automated coverage is in `tests/test_budget_planning.py`.

| Request / evidence | Expected agent behavior |
| --- | --- |
| "What remains this month?" without an established month or plan | Establish the exact plan and calendar month before reading; do not silently substitute UTC or machine-local current month. |
| Assigned 64, Activity -19, Available 57 | Report Available 57 from the API; explain rollover can make it differ from 45. Do not alter the budget. |
| "Increase Example Category A by 17.25" from an assignment of 64 | Prepare a draft for total 81.25, show the delta and estimated balance effects, then wait for approval. |
| "Set Example Category A to 17.25" | Interpret 17.25 as the new total, not an increase. Clarify if the user meant Available rather than Assigned. |
| Two categories share a name | Resolve group/name or exact ID; do not choose the first match. |
| A filtered category report also returns whole-month Ready to Assign | Label the scope; do not present month totals as filtered-category totals. |
| Available is negative or an assignment makes Ready to Assign negative | Surface the value/warning and review consequences; do not automatically move money elsewhere. |
| A budget changes after draft review | Explain the conflict; create a fresh draft and obtain approval of its revised amounts. |
| A write times out after it may have reached YNAB | Inspect and reconcile the same draft with GETs only; do not reapply or use resume. |
| "Move money between two categories in one transaction" | Explain that this release supports single-category assignments, not atomic transfers. Do not promise all-or-nothing execution. |
| "Change my savings target" | Explain that target editing is not implemented; do not silently change the monthly assignment instead. |
| Budget data appears in an error, draft, or report | Keep it private; never add real categories or financial patterns to public fixtures, logs, or documentation. |
