+++
topic = "cards"
title = "Summary cards"
summary = "Turns structured answers into fixed-template images and sends them to the chat."
aliases = ["summary_card"]
+++

## Overview
The cards plugin turns validated structured content into a clean summary image with a short text
reply beside it. It is disabled by default.

## Workflow
The skill and tool are offered only when cards are enabled, a renderer URL is configured, the
hourly cap is positive, and the platform can deliver photos or files. Rendering uses the local
card renderer; the image is sent to the chat.

## Storage
Card content and rendered PNGs are held in memory during the request. The plugin keeps no card
content or image history. Per-user hourly counters are process local and reset on restart.

## Safety
Card data is validated and rendered through a fixed escaped template. Access is checked before
rendering and again before delivery. Logs include only status, sizes and duration.

## Limitations
Cards have bounded text, block, page and image sizes. Links are shown as hostnames and are not
clickable. Platforms without photo or file delivery cannot use cards.

## Verification
Enable cards with a local renderer endpoint and request a short structured card. Confirm the
photo or file arrives before the concise text reply.

### Settings
| Setting | Default |
| --- | --- |
| `CARDS_ENABLED` | `false` |
| `CARDS_RENDER_URL` | empty |
| `CARDS_THEME` | `light` |
| `CARDS_TIMEOUT_SECONDS` | `20` |
| `CARDS_MAX_TEXT_CHARS` | `8000` |
| `CARDS_MAX_PER_USER_HOUR` | `20` |
| `CARDS_FOOTER` | empty |
