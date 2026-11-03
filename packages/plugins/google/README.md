# chatcore-google

Optional owner-account Google Docs/Sheets and read-only Drive DOCX reads, confirmed native-file creation/appends, and deterministic
itinerary handlers. Export providers and CSV attachment readers are app-injected; this package
does not import another plugin. App composites combine its tools with search/research/expenses.

Public API: `GooglePlugin`, `GoogleSettings`. Configure `GOOGLE_OWNER_EMAIL` and a private
`GOOGLE_OAUTH_CREDENTIALS_FILE` after running `chatcore-google-auth`. Unconfigured apps register
no tools. Configured allowed chats default on, with confirmed per-chat opt-out.

See [operator guide](../../../docs/google-workspace.md) for OAuth setup, scopes, schema,
confirmation, quota, privacy and uncertainty behavior. Service-account impersonation, sharing
changes, arbitrary updates, existing-row edits, Word writes, uploaded DOCX and Excel extraction
are not implemented. Drive DOCX needs an explicitly renewed `drive.readonly` grant; existing
native Docs/Sheets profiles remain compatible. Main-body text/tables are extracted with bounded
ZIP handling and `defusedxml`; images and exact visual layout are not read.
