"""Sela 续跑（continuation）状态机：纯函数，不接触数据库。

一条 continuation 代表「一个人工回答已经改变了业务事实，Sela 还需要据此
从原阻塞点继续」这件事。它不是新的业务实体，而是 Inbox 问题的执行侧状态，
存放在 ``inbox_items.request_json`` 的 ``resume_run`` 里，因此沿用现有数据
模型，不引入新表。

状态词汇（对外契约）：

* ``waiting_for_human``  问题仍开放，等人回答（Inbox item 状态 open）。
* ``answered``           人已提交回答（``human_response.status``）。
* ``queued`` / ``waiting_for_sela``  回答已落库，续跑已排入队列，等待 Sela 消费。
* ``running``            兼容旧状态：Sela 正在执行。
* ``resumed``            Sela 已从原阻塞点继续（人工回答已被消费）。
* ``completed``          续跑完成。
* ``failed`` / ``needs_review``  失败或需人工复核；保留原因，可重试。

一个回答只创建一条 continuation：稳定键是 ``inbox:<item_id>:<answer_sha256>``，
重复提交同一回答或重复消费都返回同一条，不重复执行。
"""

WAITING_FOR_HUMAN = 'waiting_for_human'
ANSWERED = 'answered'
QUEUED = 'queued'
WAITING_FOR_SELA = 'waiting_for_sela'
RUNNING = 'running'
RESUMED = 'resumed'
COMPLETED = 'completed'
FAILED = 'failed'
NEEDS_REVIEW = 'needs_review'

# 开放状态：Sela 仍应消费。
OPEN_STATUSES = frozenset({QUEUED, RUNNING, RESUMED})
# 终态：不再自动消费；failed / needs_review 保留原因，可人工重排。
TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, NEEDS_REVIEW})
ALL_STATUSES = frozenset({
    WAITING_FOR_HUMAN, ANSWERED, QUEUED, WAITING_FOR_SELA,
    RUNNING, RESUMED, COMPLETED, FAILED, NEEDS_REVIEW,
})

# 历史/别名状态收敛到规范状态。
STATUS_ALIASES = {
    WAITING_FOR_SELA: QUEUED,
    'awaiting_agent': QUEUED,
    'pending': QUEUED,
    'succeeded': COMPLETED,
    'error': FAILED,
}

# 允许的状态迁移。``completed`` 是唯一不可逆的终态，避免 Sela 重试把已完成的
# 续跑改回去；``failed`` / ``needs_review`` 保留原因且可以人工重排继续处理。
TRANSITIONS = {
    QUEUED: {QUEUED, RUNNING, RESUMED, COMPLETED, FAILED, NEEDS_REVIEW},
    RUNNING: {RUNNING, RESUMED, QUEUED, COMPLETED, FAILED, NEEDS_REVIEW},
    RESUMED: {RESUMED, RUNNING, QUEUED, COMPLETED, FAILED, NEEDS_REVIEW},
    COMPLETED: {COMPLETED},
    FAILED: {FAILED, QUEUED, RUNNING, RESUMED, NEEDS_REVIEW},
    NEEDS_REVIEW: {NEEDS_REVIEW, QUEUED, RUNNING, RESUMED, COMPLETED, FAILED},
}

# Sela 可请求的续跑动作。动作是「从原阻塞点继续」的语义标签，不是发送授权。
ACTION_VERIFY_EMAIL = 'verify_email'
ACTION_RESOLVE_EXCLUSION = 'resolve_exclusion'
ACTION_RESOLVE_NEED = 'resolve_need'
ACTION_CONTINUE_DEVELOPMENT = 'continue_development'
ACTIONS = (
    ACTION_VERIFY_EMAIL, ACTION_RESOLVE_EXCLUSION, ACTION_RESOLVE_NEED,
    ACTION_CONTINUE_DEVELOPMENT,
)
DEFAULT_ACTION = ACTION_CONTINUE_DEVELOPMENT


def normalize_status(value):
    """Return the canonical continuation status, or '' when unknown."""
    status = str(value or '').strip().lower()
    status = STATUS_ALIASES.get(status, status)
    return status if status in ALL_STATUSES else ''


def is_open(value):
    return normalize_status(value) in OPEN_STATUSES


def is_terminal(value):
    return normalize_status(value) in TERMINAL_STATUSES


def can_transition(current, wanted):
    """Whether ``wanted`` may follow ``current`` (idempotent on equality)."""
    current = normalize_status(current)
    wanted = normalize_status(wanted)
    if not wanted:
        return False
    if not current:
        return True
    return wanted in TRANSITIONS.get(current, set())


def normalize_action(value):
    action = str(value or '').strip().lower()
    return action if action in ACTIONS else DEFAULT_ACTION


def continuation_key(inbox_item_id, answer_sha256):
    """Stable identity of one continuation: one per question + answer."""
    try:
        item_id = int(inbox_item_id)
    except (TypeError, ValueError):
        return ''
    digest = str(answer_sha256 or '').strip().lower()
    if not digest:
        return ''
    return f'inbox:{item_id}:{digest}'


def build_resume_run(
    *, status=QUEUED, answer_sha256='', inbox_item_id=None, action='',
    source_id='', session_id='', summary='', error='', error_code='',
    reason='', facts_applied=None, run_session_id='', attempt=0,
    updated_at='',
):
    """Assemble the bounded continuation record stored with an Inbox item."""
    digest = str(answer_sha256 or '').strip().lower()
    status = normalize_status(status) or QUEUED
    return {
        'status': status,
        'continuation_key': continuation_key(inbox_item_id, digest) if inbox_item_id else '',
        'answer_sha256': digest,
        'action': normalize_action(action),
        'source_id': str(source_id or '')[:200],
        'session_id': str(session_id or '')[:200],
        'summary': str(summary or '')[:2000],
        'error': str(error or '')[:500],
        'error_code': str(error_code or '')[:80],
        'reason': str(reason or '')[:80],
        'facts_applied': list(facts_applied or [])[:40],
        'run_session_id': str(run_session_id or '')[:200],
        'attempt': max(0, int(attempt or 0)),
        'updated_at': str(updated_at or ''),
    }


def view(resume_run):
    """Project a stored continuation into the bounded API shape.

    Accepts the stored dict and always returns the canonical keys, so old rows
    (which only had status/answer_sha256/summary) still read cleanly.
    """
    value = resume_run if isinstance(resume_run, dict) else {}
    status = normalize_status(value.get('status')) or QUEUED
    return {
        'status': status,
        'waiting_for_sela': status == QUEUED,
        'resumed': status == RESUMED,
        'continuation_key': str(value.get('continuation_key') or ''),
        'answer_sha256': str(value.get('answer_sha256') or ''),
        'action': normalize_action(value.get('action')),
        'source_id': str(value.get('source_id') or ''),
        'session_id': str(value.get('session_id') or ''),
        'summary': str(value.get('summary') or ''),
        'error': str(value.get('error') or ''),
        'error_code': str(value.get('error_code') or ''),
        'reason': str(value.get('reason') or ''),
        'facts_applied': value.get('facts_applied') if isinstance(value.get('facts_applied'), list) else [],
        'run_session_id': str(value.get('run_session_id') or ''),
        'attempt': int(value.get('attempt') or 0),
        'updated_at': str(value.get('updated_at') or ''),
    }
