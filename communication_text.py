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

# --- Inline boundaries -------------------------------------------------------
# Gmail/Proton plain-text parts often collapse the whole thread onto one line,
# so the same markers also have to be found mid-text, not only at a line start.
# These are deliberately narrow to avoid cutting legitimate prose.
_INLINE_SEPARATOR = re.compile(
    r'[-_=]{2,}\s*'
    r'(?:original message|forwarded message|forward message|original email|'
    r'原始邮件|原始邮件内容|转发邮件|转发的邮件|原邮件|原邮件内容|邮箱转发)'
    r'\s*[-_=]{2,}',
    re.IGNORECASE,
)
_INLINE_ON_WROTE = re.compile(r'\bOn\b[^\n]{0,320}?\bwrote\s*:\s*', re.IGNORECASE)
_INLINE_MOBILE = re.compile(r'(?<!\S)(?:Sent from|Sent with|Get Outlook for)\b')
_INLINE_OUTLOOK_DIVIDER = re.compile(r'(?<!\S)_{5,}')
# Escaped quote markers ("&gt;") and their full-width forms; a bare ">" is
# intentionally skipped inline because it also occurs in ordinary prose.
_INLINE_QUOTE = re.compile(r'(?<!\S)(?:&gt;|＞|﹥)(?=\s)')
_INLINE_CN_WROTE = re.compile(
    r'(?<!\S)(?:&lt;|<)[^<>\n]{0,80}(?:&gt;|>)?[^\n]{0,80}?写道\s*[:：]'
)
_INLINE_CN_DATE = re.compile(r'\d{4}年\d{1,2}月\d{1,2}日[^\n]{0,40}?写道\s*[:：]')
_INLINE_PATTERNS = (
    _INLINE_SEPARATOR, _INLINE_ON_WROTE, _INLINE_MOBILE,
    _INLINE_OUTLOOK_DIVIDER, _INLINE_QUOTE, _INLINE_CN_WROTE, _INLINE_CN_DATE,
)

# --- Legacy Sela feedback envelope -------------------------------------------
# A retired Sela history import wrote an import/sync provenance envelope in
# front of the real reply text:
#
#     [Sela Feedback ID: <event_id>]
#     历史客户回复
#     事件：INTERESTED
#     时间：Tue, 25 Aug 2026 09:11:31 -0400
#     Gmail 同步（CODEX_REVIEW）：Re: ... <body>
#
# None of the envelope lines nor the leading ``Gmail 同步（…）`` label is the
# communication fact; they are provenance.  The cleaner drops them and keeps
# the reply itself.  It also normalizes a raw Gmail sync block (``主题：`` /
# ``发件人：`` / ``正文：``) into the same shape the live reply path stores.
_SELA_ENVELOPE_MARKER = re.compile(r'^\s*\[\s*Sela\s*Feedback\s*ID\s*:.*?\]\s*$', re.IGNORECASE)
_SELA_ENVELOPE_HEADING = re.compile(r'^\s*历史\s*客户回复\s*[:：]?\s*$')
_SELA_ENVELOPE_EVENT = re.compile(r'^\s*(?:事件|事件类型)\s*[:：]\s*\S')
_SELA_ENVELOPE_TIME = re.compile(r'^\s*(?:时间|发生时间)\s*[:：]\s*\S')
# Import/sync provenance labels that used to prefix the stored detail.  Kept in
# step with ``trosa_domain._RE_SYNC_LABEL``.
_SYNC_PROVENANCE = re.compile(
    r'^\s*(?:'
    r'Gmail\s*同步|本地\s*Gmail\s*API|Gmail\s*exact-?thread|Gmail\s*DSN|Gmail\s*connector|'
    r'SYSTEM_FALLBACK|CODEX_REVIEW|RULES_V1|历史\s*sela\s*外联'
    r')\s*(?:[（(][^）)]*[）)])?\s*[:：]?\s*',
    re.IGNORECASE,
)
# Structured labels of a raw Gmail sync block; only 主题/正文 carry the fact.
_SYNC_FIELD_TOKEN = re.compile(
    r'(?P<label>主题|发件人|收件人|Gmail\s+message_id|消息\s*ID|规则意图|路由|正文)\s*[:：]\s*'
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


def _inline_cut(text):
    """Earliest inline boundary offset, or None when the body is already clean."""
    cut = None
    for pattern in _INLINE_PATTERNS:
        match = pattern.search(text)
        if match and match.start() > 0:
            if cut is None or match.start() < cut:
                cut = match.start()
    return cut


def _first_nonempty_index(lines):
    for index, line in enumerate(lines):
        if line.strip():
            return index
    return None


def looks_like_sela_feedback_envelope(value):
    """True when the body still carries the retired Sela import envelope."""
    normalized = _normalize(value)
    if not normalized.strip():
        return False
    lines = normalized.split('\n')
    index = _first_nonempty_index(lines)
    return index is not None and bool(_SELA_ENVELOPE_MARKER.match(lines[index]))


def looks_like_sync_provenance(value):
    """True when the body starts with a raw Gmail/Sela sync provenance label."""
    normalized = _normalize(value)
    if not normalized.strip():
        return False
    lines = normalized.split('\n')
    index = _first_nonempty_index(lines)
    return index is not None and bool(_SYNC_PROVENANCE.match(lines[index]))


def looks_like_legacy_sela_content(value):
    """True when ``clean_legacy_sela_content`` would change this stored body."""
    return looks_like_sela_feedback_envelope(value) or looks_like_sync_provenance(value)


def _strip_sync_provenance_labels(text):
    """Drop every leading sync label; the reply itself is left untouched."""
    cleaned = text.strip()
    while True:
        match = _SYNC_PROVENANCE.match(cleaned)
        if not match:
            return cleaned
        remainder = cleaned[match.end():].lstrip()
        if remainder == cleaned:
            return cleaned
        cleaned = remainder


def _reformat_sync_detail(text):
    """Turn a raw ``主题：…正文：…`` sync block into the live reply shape.

    Returns ``None`` when the block has no structured fields, so the caller can
    keep the already-readable detail unchanged.
    """
    matches = list(_SYNC_FIELD_TOKEN.finditer(text))
    if not matches:
        return None
    fields = {}
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        fields.setdefault(match.group('label'), value)
    subject = (fields.get('主题') or '').strip()
    body = (fields.get('正文') or '').strip()
    if not subject and not body:
        return None
    parts = ['客户通过 Gmail 回复']
    if subject:
        parts.append(f'主题：{subject}')
    if body:
        parts.append(f'正文：\n{body}')
    return '\n'.join(parts)


def clean_legacy_sela_content(value, limit=_MAX_LENGTH):
    """Strip the retired Sela import envelope from a stored communication body.

    The raw row is left in the source store for audit; this only cleans the
    value handed to a read surface.  Idempotent, and it never returns an empty
    string for a non-empty input.
    """
    original = _normalize(value, limit)
    if not original.strip():
        return ''
    lines = original.split('\n')
    marker = next((index for index, line in enumerate(lines) if _SELA_ENVELOPE_MARKER.match(line)), None)
    if marker is None and not looks_like_sync_provenance(original):
        return original.strip()
    if marker is not None:
        # The retired writer always emitted heading/事件/时间 immediately after
        # the marker; consume only that header so a ``时间：`` line inside a
        # multi-line detail is never mistaken for the envelope.
        index = marker + 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index < len(lines) and _SELA_ENVELOPE_HEADING.match(lines[index]):
            index += 1
        for pattern in (_SELA_ENVELOPE_EVENT, _SELA_ENVELOPE_TIME):
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index < len(lines) and pattern.match(lines[index]):
                index += 1
        detail = _collapse('\n'.join(lines[:marker] + lines[index:]))
    else:
        detail = _collapse(original)
    detail = _strip_sync_provenance_labels(detail)
    reformatted = _reformat_sync_detail(detail)
    cleaned = _collapse(reformatted if reformatted else detail)
    if not cleaned:
        return _collapse(original) or original.strip()
    return cleaned[:limit]


def looks_like_quoted_email(value):
    """True when the captured body still carries quoted history or a signature."""
    normalized = _normalize(value)
    if not normalized.strip():
        return False
    if looks_like_legacy_sela_content(normalized):
        return True
    lines = normalized.split('\n')
    if any(_is_boundary(lines, index) for index in range(1, len(lines))):
        return True
    return _inline_cut(normalized) is not None


def strip_quoted_email_text(value, limit=_MAX_LENGTH):
    """Return the current message body with quoted history and signatures removed.

    The retired Sela import envelope is dropped first (see
    :func:`clean_legacy_sela_content`), then quoted history/signatures.
    Idempotent: cleaning an already-clean body returns it unchanged.  If the
    boundary heuristic would remove everything, the normalized original is kept
    so no message is ever silently lost.
    """
    original = _normalize(value, limit)
    if not original.strip():
        return ''
    original = clean_legacy_sela_content(original, limit)
    lines = original.split('\n')
    cut = len(original)
    for index in range(1, len(lines)):
        if _is_boundary(lines, index):
            cut = min(cut, sum(len(part) + 1 for part in lines[:index]))
            break
    inline = _inline_cut(original)
    if inline is not None:
        cut = min(cut, inline)
    kept = _dedupe_paragraphs(original[:cut])
    cleaned = _collapse(kept)
    if not cleaned:
        return _collapse(_dedupe_paragraphs(original)) or original.strip()
    return cleaned[:limit]


# --- Raw message detection ---------------------------------------------------
# A communication record states what happened; it is never the message itself.
# ``looks_like_raw_message`` is the write-side gate: anything that still reads
# like a captured mail/chat body is summarised before it becomes a record.
# Mirrors ``looksLikeRawMessageText`` in app/static/app.js (display safety net).
_RAW_PREFIX = re.compile(r'^\s*(?:客户通过 Gmail 回复|外联邮件退信)')
_RAW_RFC_DATE = re.compile(r'^\s*[A-Za-z]{3},\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{1,2}:\d{2}')
_RAW_HEADER_A = re.compile(r'(?:^|\n)\s*(?:subject|from|主题|发件人)\s*[:：]\s*\S', re.IGNORECASE)
_RAW_HEADER_B = re.compile(r'(?:^|\n)\s*(?:to|sent|date|收件人|发送时间|消息 ID)\s*[:：]', re.IGNORECASE)
_RAW_BOILERPLATE = re.compile(
    r'notice of confidentiality|sent from my |wrote\s*:|原始邮件|-{3,}\s*(?:original|forwarded) message',
    re.IGNORECASE,
)
_EMAIL_ADDRESS = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
_SUBJECT_LINE = re.compile(r'(?:^|\n)\s*(?:主题|subject)\s*[:：]\s*([^\n]{1,80})', re.IGNORECASE)


def looks_like_raw_message(value):
    text = str(value or '').strip()
    if not text:
        return False
    if _RAW_PREFIX.match(text) or _RAW_RFC_DATE.match(text):
        return True
    if _RAW_HEADER_A.search(text) and _RAW_HEADER_B.search(text):
        return True
    if _RAW_BOILERPLATE.search(text):
        return True
    cjk = len(re.findall(r'[一-鿿]', text))
    return len(text) > 160 and cjk < len(text) * 0.1 and bool(_EMAIL_ADDRESS.search(text))


def raw_message_subject(value):
    match = _SUBJECT_LINE.search(str(value or ''))
    if not match:
        return ''
    return re.sub(r'^(?:re|回复)\s*[:：]\s*', '', match.group(1).strip(), flags=re.IGNORECASE)


def message_body_for_summary(value):
    """The message body without the sync envelope or quoted history."""
    cleaned = strip_quoted_email_text(value)
    marker = re.search(r'(?:^|\n)\s*正文\s*[:：]\s*', cleaned)
    if marker:
        cleaned = cleaned[marker.end():].strip()
    return cleaned


def fallback_message_fact(value, direction='inbound', activity_type=''):
    """One-line fact used when no summary model is available; never the body."""
    text = str(value or '')
    if activity_type == 'outreach_bounced' or text.lstrip().startswith('外联邮件退信'):
        label = '外联邮件退信'
    elif direction == 'outbound':
        label = '我方发出邮件'
    else:
        label = '客户回复了邮件'
    subject = raw_message_subject(text)
    return label + ('：' + subject if subject else '')
