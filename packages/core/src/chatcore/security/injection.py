"""Log possible prompt injection in untrusted content for visibility only."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from chatcore.store.base import Cache

MAX_SCAN_CHARS = 200_000
COUNTER_TTL_S = 3 * 86_400

_RULES = (
    (
        "override",
        re.compile(
            r"(?:\b(?:ignore|disregard|forget|override)\s+(?:(?:all|the|your|my)\s+)?"
            r"(?:previous|prior|above|earlier|preceding|existing|original|all)\s+"
            r"(?:instructions?|prompts?|rules|directions|context|messages)\b|"
            r"\b(?:ignore|disregard|forget)\s+your\s+(?:instructions|prompt|rules)\b|"
            r"\bdisregard\s+(?:(?:everything|all)\s+)?(?:the\s+)?(?:above|before)\b|"
            r"(?:忽略|无视|無視|忘记|忘記|不要理会|不要理會).{0,8}"
            r"(?:之前|以上|上面|前面|先前|所有|全部|原来|原來).{0,8}"
            r"(?:指令|指示|说明|說明|提示|规则|規則|要求))",
            re.IGNORECASE,
        ),
    ),
    (
        "persona",
        re.compile(
            r"(?:\byou\s+are\s+now\s+(?:(?:a|an|in)\s+)?(?:dan|unrestricted|jailbroken|"
            r"developer\s+mode|an?\s+(?:ai|assistant|chatbot|model)\s+(?:with\s+no|without|that))\b|"
            r"\bfrom\s+now\s+on,?\s+you\s+(?:are|will|must)\b|"
            r"\bnew\s+(?:system\s+)?instructions\s*[:：]|"
            r"\byour\s+new\s+(?:instructions|role|task)\s+(?:are|is)\b|"
            r"你现在是|你現在是|从现在开始你|從現在開始你|从现在起你|從現在起你|"
            r"新的?(?:系统|系統)?(?:指令|指示)\s*[:：])",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt_probe",
        re.compile(
            r"(?:\b(?:reveal|show|print|repeat|output|display|dump|leak|tell\s+me|give\s+me|"
            r"what\s+(?:is|are)|what's)(?:\s+\w+){0,3}\s+your\s+"
            r"(?:(?:system|initial|original|hidden)\s+)?(?:prompt|instructions?)\b|"
            r"\b(?:reveal|print|repeat|output|dump|leak)(?:\s+(?:the|your))?\s+system\s+prompt\b|"
            r"(?:显示|顯示|输出|輸出|告诉我|告訴我|泄露|洩露|打印|重复|重複|透露)"
            r".{0,6}(?:系统提示|系統提示|提示词|提示詞|你的指令|你的指示))",
            re.IGNORECASE,
        ),
    ),
    (
        "fake_tag",
        re.compile(
            r"(?:<(?:\s{0,4}/)?\s{0,4}(?:system|assistant|developer|user)\s{0,4}>|"
            r"<(?:\s{0,4}/)?\s{0,4}untrusted-|"
            r"\[/?INST\]|<<\s*/?SYS\s*>>|<\|im_(?:start|end|sep)\|>|"
            r"<\|(?:system|user|assistant|endoftext|eot_id|start_header_id|end_header_id)\|>|"
            r"<｜[^｜\n]{1,40}｜>|</?｜DSML｜)",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_order",
        re.compile(
            r"(?:\b(?:ai|assistant|bot|chatbot|llm|claude|chatgpt|gpt|deepseek)"
            r"\s{0,3}[,:]\s{0,3}(?:[a-z']+\s+){0,4}?"
            r"\b(?:call|invoke|execute|send\s+(?:a\s+)?message|post\s+(?:a\s+)?message|"
            r"transfer|pay|confirm|approve)\b|"
            r"\b(?:ai|assistant|bot|chatbot|llm|claude|chatgpt|gpt|deepseek)\s+"
            r"(?:must|should|needs?\s+to|please|now)\s+(?:[a-z']+\s+){0,2}?"
            r"\b(?:call|invoke|execute|send\s+(?:a\s+)?message|post\s+(?:a\s+)?message|"
            r"transfer|pay|confirm|approve)\b|"
            r"\b(?:call|invoke)\s+(?:(?:the|your)\s+)?[a-z_]{1,40}\s+(?:tool|function)\b|"
            r"(?:调用|調用).{0,10}(?:工具|函数|函數)|"
            r"(?:(?<![A-Za-z])AI(?![A-Za-z])|助手|机器人|機器人|模型|人工智能).{0,20}"
            r"(?:发送消息|發送訊息|发消息|转账|轉帳|付款|支付|确认|確認))",
            re.IGNORECASE,
        ),
    ),
    (
        "ai_must",
        re.compile(
            r"(?:\bas\s+an?\s+(?:ai|artificial\s+intelligence|language\s+model|"
            r"ai\s+language\s+model|assistant)(?:,\s{0,3}|\s{1,4})(?:you\s+)?"
            r"(?:must|should|are\s+required\s+to|have\s+to|will)\b|"
            r"(?:作为|作為).{0,4}(?:AI|人工智能|语言模型|語言模型|助手).{0,6}"
            r"你?(?:必须|必須|应该|應該|需要))",
            re.IGNORECASE,
        ),
    ),
)

log = logging.getLogger(__name__)


def scan(text: str) -> tuple[str, ...]:
    """Return matched rule IDs in fixed order, scanning at most MAX_SCAN_CHARS."""
    if not isinstance(text, str) or not text:
        return ()
    sample = text[:MAX_SCAN_CHARS]
    return tuple(rule_id for rule_id, pattern in _RULES if pattern.search(sample))


def counter_key(day: date | None = None) -> str:
    """Return the UTC daily counter key."""
    day = day or datetime.now(UTC).date()
    return f"injection_flags:{day.isoformat()}"


async def flag(
    text: str, *, chat_key: str, source: str, cache: Cache | None = None
) -> tuple[str, ...]:
    """Scan and log rule IDs only; failures never escape into the tool call path."""
    try:
        rules = scan(text)
        if rules:
            log.info(
                "untrusted content flagged chat=%s source=%s rules=%s",
                chat_key,
                source,
                ",".join(rules),
            )
            if cache is not None:
                await cache.incr(counter_key(), ttl_s=COUNTER_TTL_S)
        return rules
    except Exception as exc:
        log.info(
            "injection scan failed chat=%s source=%s error=%s",
            chat_key,
            source,
            type(exc).__name__,
        )
        return ()


def web_source(tool_name: str, arguments: dict) -> str:
    """Return a tool label with only the URL hostname, when available."""
    url = arguments.get("url")
    if not isinstance(url, str):
        return tool_name
    try:
        host = urlsplit(url.strip()).hostname
    except (TypeError, ValueError):
        return tool_name
    return f"{tool_name}:{host.lower()}" if host else tool_name
