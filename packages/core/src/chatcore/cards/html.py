"""Safe fixed-template HTML rendering for validated cards."""

from __future__ import annotations

import html

from .errors import CardError
from .models import Card

THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "raised": "#f5f5f4",
        "ink": "#0b0b0b",
        "ink-2": "#52514e",
        "rule": "#dededc",
        "rule-strong": "#8d8d89",
        "accent": "#2a78d6",
        "good": "#176b36",
        "warning": "#795000",
        "critical": "#b42318",
        "info": "#1558a6",
    },
    "dark": {
        "surface": "#1a1a19",
        "raised": "#252524",
        "ink": "#f0efec",
        "ink-2": "#c3c2b7",
        "rule": "#444442",
        "rule-strong": "#888883",
        "accent": "#3987e5",
        "good": "#72d994",
        "warning": "#ffca66",
        "critical": "#ff8177",
        "info": "#79b8ff",
    },
}
_TEMPLATE = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
    '<meta name="viewport" content="width=400"><style>{css}</style></head>'
    "<body><main>{content}{footer}</main></body></html>"
)
_CSS = (
    "*{box-sizing:border-box}"
    "html,body{margin:0;background:{surface};color:{ink};"
    'font:400 16px/1.5 "Noto Sans","Noto Sans CJK SC","Noto Color Emoji",'
    "system-ui,sans-serif;overflow-wrap:anywhere}"
    "main{width:400px;padding:20px}"
    "header{border-bottom:3px solid {accent};padding-bottom:10px}"
    ".title{font-size:22px;line-height:1.25;font-weight:650}"
    ".subtitle{margin-top:4px}"
    ".subtitle,.note,.meta,footer{font-size:13px;line-height:1.4;color:{ink-2}}"
    "section[data-block]{margin-top:16px}"
    ".heading{font-size:17px;line-height:1.3;font-weight:600}"
    ".stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}"
    ".tile{background:{raised};padding:12px;border-radius:8px}"
    ".value{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}"
    ".table{width:100%;border-collapse:collapse}"
    ".table th{background:{raised};font-size:13px;line-height:1.4;color:{ink-2};text-align:left}"
    ".table td,.table th{padding:7px 6px;border-bottom:1px solid {rule};vertical-align:top}"
    ".table .end{text-align:right;font-variant-numeric:tabular-nums}"
    ".total td{font-weight:650;border-top:2px solid {rule-strong}}"
    ".callout{border-left:4px solid var(--tone);padding:8px 12px}"
    ".tone-info{--tone:{info}}.tone-good{--tone:{good}}"
    ".tone-warning{--tone:{warning}}.tone-critical{--tone:{critical}}"
    ".tile.tone-info,.tile.tone-good,.tile.tone-warning,.tile.tone-critical{"
    "border-left:4px solid var(--tone)}"
    ".callout-label{font-weight:600;color:{ink}}ul{padding-left:22px;margin:0}"
    "footer{margin-top:16px}"
)
_TONES = {
    "info": ("ℹ", "Note"),
    "good": ("✓", "Good"),
    "warning": ("!", "Check"),
    "critical": ("✕", "Important"),
}
_TONE_CLASSES = {tone: f"tone-{tone}" for tone in _TONES}
_ALIGN_CLASSES = {"start": "start", "end": "end"}


def _fixed(mapping: dict[str, str], value: str, field: str) -> str:
    try:
        return mapping[value]
    except (KeyError, TypeError):
        raise CardError("invalid", f"{field}: unsupported value") from None


def contrast_ratio(a: str, b: str) -> float:
    def lum(value):
        channels = [int(value[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in channels]
        return sum(c * w for c, w in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    x, y = sorted((lum(a), lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def render_card_html(card: Card, *, theme: str = "light", footer: str | None = None) -> str:
    if theme not in THEMES:
        raise CardError("invalid", "theme: unsupported")

    def esc(value):
        return html.escape(str(value), quote=True)

    out = [f'<header><div class="title">{esc(card.title)}</div>']
    if card.subtitle:
        out.append(f'<div class="subtitle">{esc(card.subtitle)}</div>')
    out.append("</header>")
    for block in card.blocks:
        typ = block.type
        if typ == "heading":
            body = f'<div class="heading">{esc(block.text)}</div>'
        elif typ == "paragraph":
            body = f"<div>{esc(block.text).replace(chr(10), '<br>')}</div>"
        elif typ == "bullets":
            body = "<ul>" + "".join(f"<li>{esc(item)}</li>" for item in block.items) + "</ul>"
        elif typ == "stats":
            body = (
                '<div class="stats">'
                + "".join(
                    f'<div class="tile'
                    f"{' ' + _fixed(_TONE_CLASSES, t.tone, 'tone') if t.tone else ''}"
                    f'"><div class="meta">'
                    f"{esc(t.label)}</div>"
                    f'<div class="value">{esc(t.value)}</div>'
                    + (f'<div class="note">{esc(t.note)}</div>' if t.note else "")
                    + "</div>"
                    for t in block.tiles
                )
                + "</div>"
            )
        elif typ == "table":
            heads = "".join(
                f'<th class="{_fixed(_ALIGN_CLASSES, c.align, "align")}">{esc(c.header)}</th>'
                for c in block.columns
            )
            rows = "".join(
                "<tr data-row>"
                + "".join(
                    f'<td class="{_fixed(_ALIGN_CLASSES, block.columns[i].align, "align")}">'
                    f"{esc(v)}</td>"
                    for i, v in enumerate(row)
                )
                + "</tr>"
                for row in block.rows
            )
            total = (
                '<tr class="total">'
                + "".join(
                    f'<td class="{_fixed(_ALIGN_CLASSES, block.columns[i].align, "align")}">'
                    f"{esc(v)}</td>"
                    for i, v in enumerate(block.total)
                )
                + "</tr>"
                if block.total
                else ""
            )
            body = (
                f'<table class="table"><thead><tr>{heads}</tr></thead>'
                f"<tbody>{rows}{total}</tbody></table>"
            )
        elif typ == "callout":
            tone_class = _fixed(_TONE_CLASSES, block.tone, "tone")
            icon, label = _TONES[block.tone]
            body = (
                f'<div class="callout {tone_class}">'
                f'<div class="callout-label">{icon} {label}</div>'
                f"<div>{esc(block.text)}</div></div>"
            )
        elif typ == "sources":
            body = (
                '<div class="meta">Sources</div><ul>'
                + "".join(
                    f'<li>{esc(s.title)} <span class="meta">{esc(s.hostname)}</span></li>'
                    for s in block.items
                )
                + "</ul>"
            )
        else:
            continue
        out.append(f"<section data-block>{body}</section>")
    footer_html = f"<footer>{esc(footer)}</footer>" if footer else ""
    tokens = THEMES[theme]
    css = _CSS
    for key, value in tokens.items():
        css = css.replace("{" + key + "}", value)
    result = _TEMPLATE.format(css=css, content="".join(out), footer=footer_html)
    if len(result.encode("utf-8")) > 256 * 1024:
        raise CardError("too_large")
    return result
