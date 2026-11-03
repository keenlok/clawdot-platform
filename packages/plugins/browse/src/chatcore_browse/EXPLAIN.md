+++
topic = "browse"
title = "Rendered pages"
summary = "A separate guarded browser renderer reads supported JavaScript pages."
aliases = ["browser", "rendered_pages"]
+++

## Overview
Rendered fetching uses a separate browser-renderer service when available. It can read pages whose
useful text requires JavaScript. This is a bounded read operation, not a general interactive browser
session and not permission to edit a website.

## Workflow
The tool validates a human/search-sourced URL and the current scope's rendered-reading policy,
then asks the configured renderer for bounded text. If ordinary fetch is available, rendered fetch
is used after an empty or JavaScript-only response; if ordinary fetch is unavailable, the rendered
tool can be called directly. The result goes back as untrusted evidence.

## Storage
Scope settings are stored through the plugin's configured repository. Rendering runs outside the
bot in its own service and returns a bounded response. This does not create an authoritative copy
of the website, synchronize with the expense ledger, or automatically store a long-term memory.
Jobs can separately use rendered reads for explicitly configured website watches.

## Safety
The service uses guarded network access and restricted browser behavior. App configuration,
group/DM policy and per-scope settings can deny rendering. A page cannot authorize another
destination or a write. Availability of ordinary web tools does not imply availability of the
renderer, and availability of reads does not grant authenticated editing access.

## Limitations
Sites can block automation, require login, render incomplete content or exceed resource limits.
The renderer can be down or unconfigured. This tool does not provide the user's signed-in browser,
arbitrary click workflows, or a workaround for blocked network destinations.

## Verification
Inspect the returned status and source URL, compare with the page when possible, and disclose
missing/truncated text. Check current renderer availability and scope policy rather than inferring
them from documentation. Use a separately reviewed tool for any authenticated edit workflow.
