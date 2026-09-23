"""Inbox 问题模型：Inbox 只承接必须由人作出判断的未决问题。

设计约束（来自产品定义）：

* Inbox 的单位是「未决问题」，不是邮件、Sela request 或系统事件。
* 同一业务问题可以引用多条证据，但只应呈现为一个问题。
* 系统能自行处理的（噪声、退信、技术冲突、重复、已被后续事实覆盖）不得进入
  Inbox；即使曾经进入，也必须能被自动关闭并留下原因与证据。
* 「问题已解决」不等于「业务动作已执行」。

这个模块只做纯函数分类与提问键计算，不接触数据库，方便被 app.py、gmail_sync.py、
测试和迁移复用。
"""

QUESTION_IDENTITY = 'identity'
QUESTION_IDENTITY_REVIEW = 'identity_review'
QUESTION_APPROVAL = 'approval'
QUESTION_REPLY = 'reply'
QUESTION_FACT_REQUEST = 'fact_request'
QUESTION_INVESTIGATION = 'investigation_request'
QUESTION_SELA_REQUEST = 'sela_request'
QUESTION_EXCLUSION_REVIEW = 'exclusion_review'

QUESTION_ORDER = (QUESTION_IDENTITY, QUESTION_REPLY, QUESTION_FACT_REQUEST, QUESTION_INVESTIGATION, QUESTION_SELA_REQUEST, QUESTION_EXCLUSION_REVIEW, QUESTION_APPROVAL, QUESTION_IDENTITY_REVIEW)

QUESTION_LABELS = {
    QUESTION_IDENTITY: '待归属',
    QUESTION_REPLY: '客户回复',
    QUESTION_APPROVAL: '待批准',
    QUESTION_IDENTITY_REVIEW: '身份待确认',
    QUESTION_FACT_REQUEST: '补充资料',
    QUESTION_INVESTIGATION: '提交调查',
    QUESTION_SELA_REQUEST: 'Sela 请求',
    QUESTION_EXCLUSION_REVIEW: '排除身份',
}

QUESTION_QUESTIONS = {
    QUESTION_IDENTITY: '这条沟通属于哪个客户？',
    QUESTION_REPLY: '这次客户回复说明了什么，下一步是什么？',
    QUESTION_APPROVAL: '这个对外动作是否可以执行？',
    QUESTION_IDENTITY_REVIEW: '这两条身份记录是不是同一个业务主体？',
    QUESTION_FACT_REQUEST: '请补充系统无法安全推断的事实。',
    QUESTION_INVESTIGATION: '请提交调查结论或支持性证据。',
    QUESTION_SELA_REQUEST: '请补充事实或作出业务判断。',
    QUESTION_EXCLUSION_REVIEW: '这条 prospect 是否属于已经排除的主体？',
}

ITEM_TYPE_QUESTION = {
    'gmail_capture': QUESTION_IDENTITY,
    'browser_capture': QUESTION_IDENTITY,
    'customer_reply': QUESTION_REPLY,
    'sela_agent_request': QUESTION_SELA_REQUEST,
    'sela_identity_review': QUESTION_IDENTITY_REVIEW,
    'sela_exclusion_review': QUESTION_EXCLUSION_REVIEW,
}

# 技术来源只作为次级证据标签，不作为 Inbox 的组织结构。
ITEM_TYPE_SOURCE = {
    'gmail_capture': 'gmail',
    'browser_capture': 'browser',
    'customer_reply': 'inbox',
    'sela_agent_request': 'sela',
    'sela_identity_review': 'sela',
    'sela_exclusion_review': 'sela',
}

SOURCE_LABELS = {
    'gmail': 'Gmail',
    'browser': '浏览器采集',
    'inbox': 'Inbox',
    'sela': 'Sela',
    'system': '系统',
}

# 这些历史类型不再代表需要人工判断的问题，读取时不应出现在 Inbox。
RETIRED_ITEM_TYPES = frozenset(('new_customer', 'ai_suggestion', 'uncontacted_follow_up', 'sela_follow_up', 'sela_proposal'))


def question_kind_for(item_type):
    # An unrecognised transport type is evidence, not an identity assertion.
    # Failing closed here prevents a new integration from quietly presenting an
    # unsafe "assign customer" action to a human.
    return ITEM_TYPE_QUESTION.get(str(item_type or '').strip(), QUESTION_FACT_REQUEST)


def source_type_for(item_type):
    return ITEM_TYPE_SOURCE.get(str(item_type or '').strip(), 'system')


def source_label_for(item_type):
    return SOURCE_LABELS.get(source_type_for(item_type), SOURCE_LABELS['system'])


def question_label(question_kind):
    return QUESTION_LABELS.get(question_kind, '待处理')


def question_text(question_kind):
    return QUESTION_QUESTIONS.get(question_kind, '需要你作出判断。')


def question_key_for(item_type, dedupe_key, identity=''):
    """一个问题的稳定业务键。

    同一发件人/同一来源的多条证据应指向同一个问题，避免为每个事件各建一个
    待处理项。身份不明确时退回逐条证据键。
    """
    item_type = str(item_type or '').strip()
    dedupe_key = str(dedupe_key or '').strip()
    identity = str(identity or '').strip().casefold()
    if item_type in ('gmail_capture', 'browser_capture') and identity:
        return 'identity:' + identity[:200]
    if item_type in ('sela_identity_review', 'sela_exclusion_review'):
        return dedupe_key or 'identity_review:' + identity
    if item_type == 'sela_agent_request':
        return dedupe_key or 'approval:' + identity
    return dedupe_key or (item_type + ':' + identity)


def is_question(item_type):
    return str(item_type or '').strip() in ITEM_TYPE_QUESTION
