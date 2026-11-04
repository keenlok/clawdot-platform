# chatcore-google

Optional owner-account Google Docs/Sheets and Drive DOCX reads, confirmed native-file creation,
appends, bounded content edits and deterministic
itinerary handlers. Export providers and CSV attachment readers are app-injected; this package
does not import another plugin. App composites combine its tools with search/research/expenses.

Public API: `GooglePlugin`, `GoogleSettings`. Configure `GOOGLE_OWNER_EMAIL` and a private
`GOOGLE_OAUTH_CREDENTIALS_FILE` after running `chatcore-google-auth`. Unconfigured apps register
no tools. Configured allowed chats default on, with confirmed per-chat opt-out.

See [operator guide](../../../docs/google-workspace.md) for OAuth setup, scopes, schema,
confirmation, quota, privacy and uncertainty behavior. Native edits use the original when
effective access permits. Qualified copy fallback and DOCX-to-new-native-Doc edits preserve
the source and require preview/confirmation before creation. `GOOGLE_COPY_QUALIFIED` defaults
false pending live owner-grant checks. Every DOCX edit imports the original package bytes;
there is no Word write-back. Service-account impersonation, arbitrary updates, row deletion,
uploaded DOCX and Excel extraction are not implemented. New Docs can be made non-discoverable
link-viewer outputs when the confirmed preview says so; original Docs retain their existing ACLs.
`GOOGLE_NEW_DOCS_LINK_VIEWER=false` keeps the configured default private.
Drive DOCX needs an explicitly renewed `drive.readonly` grant; existing
native Docs/Sheets profiles remain compatible. Main-body text/tables are extracted with bounded
ZIP handling and `defusedxml`; images and exact visual layout are not read.
