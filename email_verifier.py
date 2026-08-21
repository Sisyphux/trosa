"""Persistent SMTP verification worker for Trade OS.

The worker never sends DATA.  It records protocol evidence and leaves policy
or temporary failures distinct from mailbox rejection.
"""
import hashlib
import hmac
import json
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone

from config import EMAIL_VERIFICATION_CONFIG
from db import get_db

_TZ = timezone(timedelta(hours=8))
_ENHANCED_STATUS = re.compile(r'\b([245]\.[0-9]\.[0-9]+)\b')


def _now():
    return datetime.now(_TZ)


def _now_text():
    return _now().strftime('%Y-%m-%d %H:%M:%S')


def is_configured():
    return bool(
        EMAIL_VERIFICATION_CONFIG.get('smtp_probe_enabled')
        and EMAIL_VERIFICATION_CONFIG.get('smtp_helo_host')
        and EMAIL_VERIFICATION_CONFIG.get('smtp_mail_from')
    )


def _decode_response(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value or '')


def _interpret_recipient(code, text):
    enhanced = (_ENHANCED_STATUS.search(text) or [None, ''])[1]
    if code in (250, 251):
        return 'accepted', enhanced
    if code == 252:
        return 'unknown', enhanced
    if 400 <= code < 500:
        return 'temporarily_unavailable', enhanced
    if enhanced.startswith('5.1.1') or enhanced.startswith('5.1.0'):
        return 'invalid_mailbox', enhanced
    if enhanced.startswith('5.7.'):
        return 'policy_blocked', enhanced
    if 500 <= code < 600:
        return 'unknown', enhanced
    return 'unknown', enhanced


def _probe_recipient(host, email):
    """Perform one MAIL/RCPT transaction and always stop before DATA."""
    timeout = max(3, int(EMAIL_VERIFICATION_CONFIG.get('smtp_timeout_seconds', 8)))
    helo = EMAIL_VERIFICATION_CONFIG['smtp_helo_host']
    mail_from = EMAIL_VERIFICATION_CONFIG['smtp_mail_from']
    smtp = None
    try:
        smtp = smtplib.SMTP(timeout=timeout)
        smtp.connect(host, 25)
        smtp.ehlo(helo)
        if smtp.has_extn('starttls'):
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo(helo)
        mail_options = []
        if not email.isascii():
            if not smtp.has_extn('smtputf8'):
                return {'outcome': 'unsupported_smtputf8', 'smtp_code': '', 'enhanced_status': '',
                        'diagnostic_text': '目标服务器未声明 SMTPUTF8', 'remote_mta': host}
            mail_options.append('SMTPUTF8')
        mail_code, mail_text = smtp.mail(mail_from, options=mail_options)
        if mail_code >= 400:
            detail = _decode_response(mail_text)
            return {'outcome': 'policy_blocked' if mail_code >= 500 else 'temporarily_unavailable',
                    'smtp_code': str(mail_code), 'enhanced_status': (_ENHANCED_STATUS.search(detail) or [None, ''])[1],
                    'diagnostic_text': detail, 'remote_mta': host}
        code, response = smtp.rcpt(email)
        detail = _decode_response(response)
        outcome, enhanced = _interpret_recipient(code, detail)
        return {'outcome': outcome, 'smtp_code': str(code), 'enhanced_status': enhanced,
                'diagnostic_text': detail, 'remote_mta': host}
    except (OSError, smtplib.SMTPException) as exc:
        return {'outcome': 'temporarily_unavailable', 'smtp_code': '', 'enhanced_status': '',
                'diagnostic_text': f'{type(exc).__name__}: {exc}', 'remote_mta': host}
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except (OSError, smtplib.SMTPException):
                pass


def _probe_mx(email, mx_records):
    hosts = []
    for record in mx_records:
        host = (record.get('host') or '').strip()
        if host and host not in hosts:
            hosts.append(host)
    limit = max(1, int(EMAIL_VERIFICATION_CONFIG.get('smtp_max_mx_attempts', 2)))
    last = None
    for host in hosts[:limit]:
        result = _probe_recipient(host, email)
        last = result
        if result['outcome'] != 'temporarily_unavailable':
            return result
    return last or {'outcome': 'temporarily_unavailable', 'smtp_code': '', 'enhanced_status': '',
                    'diagnostic_text': '没有可连接的 MX 主机', 'remote_mta': ''}


def _domain_catchall_probe(cursor, domain, mx_records):
    if not EMAIL_VERIFICATION_CONFIG.get('catchall_enabled') or not EMAIL_VERIFICATION_CONFIG.get('catchall_secret'):
        return None
    now = _now_text()
    cached = cursor.execute('''SELECT catchall_status, evidence FROM email_domain_probes
                               WHERE domain=? AND next_check_at > ?''', (domain, now)).fetchone()
    if cached:
        return {'status': cached['catchall_status'], 'cached': True,
                'evidence': json.loads(cached['evidence'] or '[]')}
    period = _now().strftime('%Y-%m-%d')
    digest = hmac.new(EMAIL_VERIFICATION_CONFIG['catchall_secret'].encode('utf-8'),
                      f'{domain}:{period}'.encode('utf-8'), hashlib.sha256).hexdigest()[:24]
    canary = f'tradeos-verify-{digest}@{domain}'
    result = _probe_mx(canary, mx_records)
    if result['outcome'] == 'accepted':
        status = 'accepts_unknown_recipients'
    elif result['outcome'] == 'invalid_mailbox':
        status = 'not_detected'
    else:
        status = 'unknown'
    expires = (_now() + timedelta(days=max(1, int(EMAIL_VERIFICATION_CONFIG['domain_probe_cache_days'])))).strftime('%Y-%m-%d %H:%M:%S')
    evidence = [{'canary': canary, **result, 'checked_at': now}]
    cursor.execute('''INSERT INTO email_domain_probes (domain, catchall_status, evidence, checked_at, next_check_at)
                      VALUES (?, ?, ?, ?, ?)
                      ON CONFLICT(domain) DO UPDATE SET catchall_status=excluded.catchall_status,
                          evidence=excluded.evidence, checked_at=excluded.checked_at, next_check_at=excluded.next_check_at''',
                   (domain, status, json.dumps(evidence, ensure_ascii=False), now, expires))
    return {'status': status, 'cached': False, 'evidence': evidence}


def process_pending_email_verification_jobs(max_jobs=5):
    """Run a bounded number of due jobs for the current database user."""
    if not is_configured():
        return {'processed': 0, 'reason': 'smtp_not_configured'}
    conn = get_db()
    conn.row_factory = __import__('sqlite3').Row
    cursor = conn.cursor()
    jobs = cursor.execute('''SELECT * FROM email_verification_jobs
                             WHERE status='queued' AND next_run_at <= ?
                             ORDER BY created_at ASC LIMIT ?''', (_now_text(), max_jobs)).fetchall()
    processed = 0
    for job in jobs:
        claimed = cursor.execute("UPDATE email_verification_jobs SET status='running', updated_at=? WHERE id=? AND status='queued'",
                                (_now_text(), job['id'])).rowcount
        if not claimed:
            continue
        verification = cursor.execute('SELECT * FROM email_verifications WHERE email=?', (job['email'],)).fetchone()
        if not verification:
            cursor.execute("UPDATE email_verification_jobs SET status='failed', last_error=?, updated_at=? WHERE id=?",
                           ('未找到基础验证结果', _now_text(), job['id']))
            continue
        mx_records = json.loads(verification['mx_records'] or '[]')
        result = _probe_mx(job['email'], mx_records)
        evidence = json.loads(verification['evidence'] or '[]')
        evidence.append({'type': 'smtp_rcpt', **result, 'checked_at': _now_text()})
        final_status = verification['deliverability_status']
        confidence = verification['confidence']
        if result['outcome'] == 'invalid_mailbox':
            final_status, confidence = 'invalid_mailbox', 'high'
        elif result['outcome'] == 'accepted':
            final_status, confidence = 'likely_deliverable', 'high'
            catchall = _domain_catchall_probe(cursor, verification['domain'], mx_records)
            if catchall:
                evidence.append({'type': 'catchall', **catchall, 'checked_at': _now_text()})
                if catchall['status'] == 'accepts_unknown_recipients':
                    final_status, confidence = 'accepts_unknown_recipients', 'medium'
        elif result['outcome'] == 'policy_blocked':
            final_status, confidence = 'policy_blocked', 'low'
        elif result['outcome'] == 'temporarily_unavailable':
            final_status, confidence = 'temporarily_unavailable', 'low'
        cursor.execute('''UPDATE email_verifications SET deliverability_status=?, confidence=?, evidence=?,
                          checked_at=?, expires_at=? WHERE email=?''',
                       (final_status, confidence, json.dumps(evidence, ensure_ascii=False), _now_text(),
                        (_now() + timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'), job['email']))
        cursor.execute('''INSERT INTO email_delivery_events
                          (email, event_type, smtp_code, enhanced_status, diagnostic_text, remote_mta, source, occurred_at)
                          VALUES (?, 'smtp_probe', ?, ?, ?, ?, 'smtp_worker', ?)''',
                       (job['email'], result['smtp_code'], result['enhanced_status'], result['diagnostic_text'],
                        result['remote_mta'], _now_text()))
        attempts = job['attempts'] + 1
        if result['outcome'] == 'temporarily_unavailable' and attempts < 2:
            next_run = (_now() + timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("UPDATE email_verification_jobs SET status='queued', attempts=?, next_run_at=?, last_error=?, updated_at=? WHERE id=?",
                           (attempts, next_run, result['diagnostic_text'], _now_text(), job['id']))
        else:
            cursor.execute("UPDATE email_verification_jobs SET status='completed', attempts=?, last_error=?, updated_at=? WHERE id=?",
                           (attempts, result['diagnostic_text'], _now_text(), job['id']))
        processed += 1
    conn.commit()
    conn.close()
    return {'processed': processed}
