# Long-running work and recovery

## Automatic rate-limit handling

The CLI reserves requests against a private rolling-hour ledger shared by local processes using the same access token. It incorporates YNAB's `X-Rate-Limit` usage when available. On HTTP 429 it honors a valid `Retry-After` value; without one it waits a full rolling hour plus a small margin. Repeated rate-limit rejections continue automatically across hours. Wait messages go to stderr; normal JSON output remains on stdout. Sleeps occur in interruptible intervals of at most 30 seconds.

Requests from other applications/devices may be unknown until a response reports their usage or rejects a call. Never promise that the first 429 can always be avoided. Keep the private ledger intact across runs; deleting it or rotating a token to bypass limits is not a recovery procedure.

Let the existing command run and poll its execution-session handle with bounded waits. Do not keep an agent reasoning or repeatedly calling the API just to pass time. Waiting in the CLI uses no model calls. A chat quota interruption does not itself cancel an already-running process, but process survival depends on the host and execution session; do not claim the CLI is a background scheduler or will survive a computer shutdown. If the process stops, its accounting and draft checkpoints remain on disk.

Only confirmed 429 rejections are retried automatically. A timeout, lost/invalid response, or server error on a write may follow a successful mutation and must not replay the write. Invalid/revoked/expired credentials stop with an authentication error; this Keychain-token tool does not perform OAuth token refresh. Resolve credentials through the user's normal secure setup, never by requesting a token in chat. ChatGPT usage quota and YNAB authentication are separate from the API rate limit.

## Checkpoint a batch before starting

For read-only allocation reports on CLI 0.6.0 or later, start with `transactions list --since YYYY-MM-DD --until YYYY-MM-DD --include-splits` and no display limit. Split lines are already in the list response; fetching every parent individually wastes requests. Preserve complete posted IDs, including underscore/date suffixes, and filter by each record's date. Record any failed reads as gaps in report completeness; do not discard records based on ID shape or reinterpret a lookup failure as scheduled status.

Keep a concise local handoff and worklist under a private, mode-700 directory in the supported local application directory or `/private/tmp`; make the files mode 600. Save their absolute paths in the chat. Use stable storage in the local application directory when work may span sessions or restarts. Never commit the handoff, financial records, proposals, or approvals to source control.

Record the exact task scope and constraints; paths to evidence/proposals and reviewed draft IDs; which drafts the user explicitly approved and the message/date establishing that approval; completed, pending, held, and uncertain items; the current command and its execution-session handle; and the next safe action. Prefer references and IDs over copying receipt or account contents. Update the checkpoint after every completed item and before a long wait or ending a turn. Reuse authoritative CLI draft state instead of treating the handoff as proof of a successful write.

For a dry run, checkpoint matches/proposals and continue read-only work automatically after rate waits. For writes, proceed only through the already-reviewed and authorized drafts. Work sequentially so recovery is clear. This skill does not authorize new drafts or new financial actions merely because earlier items in a batch were approved.

## Resume after an interrupted process or chat

1. Read the private handoff. If an execution session still exists, inspect its output and await completion; do not launch a duplicate command.
2. Inspect the current draft with `drafts show ID`. Accounting survives restart and the next command will honor remaining cooldowns.
3. An authorized `pending` draft can use `drafts apply ID --confirm ID`. A confirmed rejected write returns to `pending` with stage `write_rate_limited`; no successful write is claimed.
4. An authorized `approval_pending` draft can use `drafts resume ID --confirm ID`. Stage `approval_rate_limited` means only the remaining approval was rejected. Resume must not recreate or reconvert the transaction.
5. For `applying`, `uncertain`, verification-pending, or other interrupted-write states, use `drafts reconcile ID` first. A creation without a known ID requires independent evidence and must not be replayed. Reconciliation itself is read-only and handles rate waits automatically.
6. Continue the same authorized work without asking the user merely to retry a rate limit. A changed transaction, missing approval evidence, ambiguous receipt, or draft older than 24 hours remains a genuine stop for that item. Do not extend the draft's timestamp, regenerate it silently, or treat a checkpoint as new approval. Report the specific issue and continue independent authorized items where safe.

Older versions recorded definite rate rejections as `failed` / `write_rejected` with `error.status: 429`, or `approval_failed` / `approval_rejected` with `approval_error.status: 429`. Version 0.4.0 accepts the same authorized draft through `apply` or approval-only `resume`, respectively, with all seal, age and current-state checks. Other failures and uncertain outcomes are not made retryable by this compatibility path.

The 24-hour draft lifetime still applies immediately before every write attempt, including after long waits. Read-only verification/reconciliation can finish after expiry. Plan very large batches so drafts can be reviewed and applied within their existing lifetime; multi-hour rate handling does not grant indefinite financial-write authorization.

For budget assignment drafts, `drafts reconcile` reads the stored month/category target without writing. `drafts resume` is not supported, and an attempted assignment cannot be applied again. Monthly assignment, activity, availability, Ready to Assign, category metadata and currency changes can invalidate review. See [budget planning](budget-planning.md) before preparing a fresh assignment after resolving an uncertain result.
