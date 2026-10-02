"""Deterministic cleaning for captured communication bodies.

Mail clients keep the whole quoted thread in the plain-text part of a message.
Trosa records the communication fact, not the raw mailbox, so the current
message body has to be separated from quoted history, forwarded headers and
signatures before it becomes timeline content.  The original bytes stay in the
source payload (``raw_text`` / ``communication_source_items``) for audit.

The cleaner is intentionally conservative and deterministic:

* it never invents content;
* it returns the input unchanged when there is nothing to strip;
* it never returns an empty string for a non-empty input.
"""

import re

_MAX_LENGTH = 50000

# A quoted line (Gmail ">", Outlook "|", or their HTML-escaped forms).
_QUOTE_LINE = re.compile(r'^\s*(?:>|&gt;|\||&vert;|[＞﹥])\s*\S')
# "-------- Original Message --------", Chinese variants, "---------- Forwarded message ----------".
_SEPARATOR = re.compile(
    r'^\s*[-_=]{2,}\s*'
    r'(?:original message|forwarded message|forward message|original email|'
    r'原始邮件|原始邮件内容|转发邮件|转发的邮件|原邮件|原邮件内容|邮箱转发)'
    r'\s*[-_=]{2,}\s*$',
    re.IGNORECASE,
)
# Outlook divider: a line of underscores.
_OUTLOOK_DIVIDER = re.compile(r'^\s*_{5,}\s*$')
# "On <date> ... wrote:" / "<date> 于...写道："
_ATTRIBUTION = re.compile(
    r'^\s*(?:'
    r'on\b.{0,320}?\bwrote\s*:\s*'
    r'|.{0,240}?wrote\s*:\s*'
    r'|在.{0,240}?写道\s*[:：]?\s*'
    r'|于.{0,240}?写道\s*[:：]?\s*'
    r'|.{0,240}?写道\s*[:：]\s*'
    r')$',
    re.IGNORECASE,
)
# RFC 3676 signature delimiter: a line of exactly two dashes.
_SIGNATURE = re.compile(r'^\s*--\s*$')
# Mobile / client auto-signatures.
_MOBILE_SIGNATURE = re.compile(
    r'^\s*(?:sent from|sent with|get outlook for|send from|发送自|发自我的|来自我的|由我的)',
    re.IGNORECASE,
)
# A forwarded-message header line ("From: ...", "发件人: ...").
_FORWARD_HEADER = re.compile(r'^\s*(?:from|发件人)\s*[:：]\s*\S', re.IGNORECASE)
# Another header that confirms a forward block rather than a stray "From:".
_HEADER_HINT = re.compile(
    r'^\s*(?:sent|date|to|cc|bcc|subject|reply-to|收件人|发送时间|发送日期|主题|抄送)\s*[:：]',
    re.IGNORECASE,
)


def _normalize(value, limit=_MAX_LENGTH):
    text = str(value or '').replace('\x00', '')
    text = re.sub(r'\r\n?', '\n', text)
    return text[:limit]


def _collapse(text):
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _dedupe_paragraphs(text):
    """Drop consecutive duplicate paragraphs (plain/HTML or repeated quote copies)."""
    paragraphs = re.split(r'\n\s*\n', text)
    kept = []
    for paragraph in paragraphs:
        stripped = paragraph.strip('\n')
        if kept and stripped and stripped == kept[-1]:
            continue
        kept.append(stripped)
    return '\n\n'.join(paragraph for paragraph in kept if paragraph.strip())


def _is_forward_block(lines, index):
    if not _FORWARD_HEADER.match(lines[index]):
        return False
    for hint in lines[index + 1:index + 12]:
        if _HEADER_HINT.match(hint):
            return True
    return False


def _is_boundary(lines, index):
    if index <= 0:
        # Never cut the very first line: it carries the captured time/sender
        # prefix and the beginning of the current message.
        return False
    line = lines[index]
    if not line.strip():
        return False
    if _QUOTE_LINE.match(line):
        return True
    if _SEPARATOR.match(line) or _OUTLOOK_DIVIDER.match(line):
        return True
    if _ATTRIBUTION.match(line):
        return True
    if _SIGNATURE.match(line) or _MOBILE_SIGNATURE.match(line):
        return True
    return _is_forward_block(lines, index)


def looks_like_quoted_email(value):
    """True when the captured body still carries quoted history or a signature."""
    normalized = _normalize(value)
    if not normalized.strip():
        return False
    lines = normalized.split('\n')
    return any(_is_boundary(lines, index) for index in range(1, len(lines)))


def strip_quoted_email_text(value, limit=_MAX_LENGTH):
    """Return the current message body with quoted history and signatures removed.

    Idempotent: cleaning an already-clean body returns it unchanged.  If the
    boundary heuristic would remove everything, the normalized original is kept
    so no message is ever silently lost.
    """
    original = _normalize(value, limit)
    if not original.strip():
        return ''
    lines = original.split('\n')
    cut = len(lines)
    for index in range(1, len(lines)):
        if _is_boundary(lines, index):
            cut = index
            break
    kept = _dedupe_paragraphs('\n'.join(lines[:cut]))
    cleaned = _collapse(kept)
    if not cleaned:
        return _collapse(_dedupe_paragraphs(original)) or original.strip()
    return cleaned[:limit]
