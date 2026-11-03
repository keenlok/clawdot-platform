# Publishing the storage architecture wiki page

The repository guide is canonical. [Storage-architecture.md](Storage-architecture.md)
is a concise, copy-ready wiki entry that shares its image and links to the full guide.
Its absolute URLs work from a GitHub wiki and target public `main` **after this PR
merges**. Until then, review the guide and image on the PR branch.

The public repository's wiki was disabled when checked on 2026-11-03. Adding files
here does not enable or publish it. GitHub stores wiki pages separately from the main
repository, so merging this PR alone does not create a wiki page.

After the PR merges, an owner or maintainer can publish it:

1. In the repository's **Settings → General → Features**, enable **Wikis** if needed.
2. Open the repository's **Wiki** tab. If it is empty, create the initial **Home** page
   first, with a short introduction to Clawdot's documentation.
3. Create a page titled **Storage architecture**, use Markdown mode, and paste the
   contents of [Storage-architecture.md](Storage-architecture.md). If that page already
   exists, review its content before updating it rather than overwriting unrelated work.
4. Preview the page. Confirm that the overview image loads and the full-guide and
   editable-image links open the files on public `main`, then save it.
5. On the wiki Home page, add `[Storage architecture](Storage-architecture)` to its
   navigation. Preserve existing Home and sidebar content.

For later updates, edit the guide and editable SVG in the main repository through
a PR, regenerate the PNG from the SVG, and visually inspect the export before
committing both files. Update the copy-ready wiki entry if its summary needs to
change. After merge, copy changed summary text to the live wiki and preview it again.
The image URL stays the same and follows `main`; there is no automatic wiki sync.

The SVG uses native text and shapes and can be edited in an SVG editor. One rendering
option is the `resvg` CLI: `resvg docs/images/storage-architecture.svg
docs/images/storage-architecture.png`. Rendering is an authoring step, not a runtime
dependency or a required installation for readers.
