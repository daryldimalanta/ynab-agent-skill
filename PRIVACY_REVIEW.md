# Privacy and distribution boundary

The distributable project contains implementation, synthetic tests, generic skill/contributor instructions, schemas, licenses, and verification evidence. Personal preferences, real receipts, budget exports, financial records, credentials, chat history, local coordination, installation records, and session reports do not belong in source, examples, Git objects, or release artifacts.

Fixture names, dates, amounts, categories, and memos are fictional; see [tests/FIXTURES.md](tests/FIXTURES.md). Runtime categories are retrieved from the selected plan. No personal category taxonomy, merchant-to-category mapping, or personal memo convention is distributed.

Credentials remain in macOS Keychain. Private workflow state uses the supported local application directory. The fixed YNAB API origin receives only the fields needed for an authorized operation. No telemetry, analytics, remote logging, or receipt-image upload is implemented. Conversation/model handling of information supplied by a user is separate from the wrapper's own I/O.

Before publishing, inspect selected file bytes, all included Git objects and commit identity, and the exact built artifact. Ignore patterns and successful tests alone cannot prove that arbitrary future content is private. Never publish local workspace directories or force-add excluded private artifacts.

Copyright attribution is retained intentionally; the project does not claim contributor anonymity. Current source verification and limits are recorded in [VALIDATION.md](VALIDATION.md).
