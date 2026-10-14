"""Saved OpenRouter credit, alarms and fixed failure reasons."""
import json
import math
import os
from pathlib import Path
import urllib.error
import urllib.request

from .i18n import t

NUMBERS = ('limit', 'limit_remaining', 'usage', 'usage_daily')
RESET_DAYS = {None: 0, 'daily': 1, 'weekly': 7, 'monthly': 30}


def read_usage(data):
    try:
        value = json.loads((Path(data) / 'usage.json').read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def failure(s, error, now=None, *, counters=True):
    code = error.code if isinstance(error, urllib.error.HTTPError) else None
    if type(code) is not int:
        code = None
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    reason = ('skald_cap' if isinstance(error, s.BudgetExceeded) else
              'timeout' if isinstance(cause, TimeoutError) else
              {401: 'unauthorized', 402: 'out_of_credit', 403: 'forbidden', 429: 'rate_limited'}.get(code) or
              ('provider_error' if code is not None and 500 <= code <= 599 else
               'network_error' if isinstance(error, (urllib.error.URLError, OSError)) else 'invalid_response'))
    now = s.time.time() if now is None else now
    fields = {'ai_failure': {'reason': reason, 'at': now}}
    if reason == 'out_of_credit':
        fields['ai_exhausted_at'] = now
    s.save_usage(fields, counters=counters)
    warn(s, now)
    return reason


def status(usage, caps, monthly, config):
    saved = usage.get('ai_credit')
    provider = {k: saved.get(k) for k in (*NUMBERS, 'checked_at')} if saved else None
    if provider is not None:
        provider['limit_reset'] = next((k for k, v in RESET_DAYS.items() if v == saved.get('reset_days')), None)
    last = usage.get('ai_failure')
    exhausted = usage.get('ai_exhausted_at')
    if exhausted is None and last and last['reason'] == 'out_of_credit':
        exhausted = last['at']
    alarm = None
    if exhausted is not None and (not provider or exhausted > provider['checked_at']):
        alarm = 'out_of_credit'
    elif provider and provider['limit'] is not None and provider['limit_remaining'] is not None:
        prefs = config.get('ai', {})
        remaining = provider['limit_remaining']
        if remaining <= 0:
            alarm = 'out_of_credit'
        elif remaining < provider['limit'] * prefs.get('credit_warn_fraction', .2) or remaining < prefs.get('credit_warn_usd', 1):
            alarm = 'low_credit'
    return {'provider': provider, 'last_checked_at': usage.get('ai_check_at'), 'alarm': alarm,
            'spend_today': usage.get('cost', 0), 'spend_month': usage.get('month_cost', 0), 'monthly_cap': monthly,
            'daily_caps': {lane: {'used': sum(n for k, n in usage.get('calls', {}).items() if k.startswith(lane + ':')),
                                  'cap': cap} for lane, cap in caps.items()}, 'last_failure': last}


def text(data, lang='en', *, detailed=False):
    provider = data['provider']
    if data['alarm'] == 'out_of_credit':
        line = t('ai.out_of_credit', lang)
    elif not provider:
        line = t('ai.unchecked', lang)
    elif provider['limit'] is None:
        line = t('ai.unlimited', lang)
    else:
        reset = t('ai.reset.' + (provider['limit_reset'] or 'none'), lang)
        line = t('ai.credit', lang, remaining=f"{provider['limit_remaining']:.2f}", limit=f"{provider['limit']:g}", reset=reset)
    if data['alarm'] == 'low_credit':
        line += ' ' + t('ai.low_credit', lang)
    if not detailed:
        return line
    lines = [line, t('ai.spend', lang, today=f"{data['spend_today']:.2f}", month=f"{data['spend_month']:.2f}")]
    lines.append(t('ai.caps', lang) + ' ' + ', '.join(f"{lane} {n['used']}/{n['cap']}" for lane, n in data['daily_caps'].items()))
    if data['last_failure']:
        lines.append(t('ai.last_failure', lang, reason=t('ai.failure.' + data['last_failure']['reason'], lang)))
    return '\n'.join(lines)


def warn(s, now):
    result = s.ai_status()
    if result['alarm']:
        today = s.online_day(now)[0]
        with s._usage_lock:
            emit = s._usage.get('ai_warning_day') != today
            s._usage['ai_warning_day'] = today
        if emit:
            s.save_usage({'ai_warning_day': today}, counters=False)
            s._daily_log('WARNING: ' + text(result, s.LANG))


def check_credit(s, now, *, refresh=False):
    key = os.environ.get('OPENROUTER_API_KEY')
    if not key:
        return {'checked': 0}
    with s._ai_credit_lock:
        today = s.online_day(now)[0]
        with s._usage_lock:
            previous = max((value for value in (s._usage.get('ai_check_at'), read_usage(s.DATA).get('ai_check_at'))
                            if value is not None), default=None)
            if not refresh and previous is not None and s.online_day(previous)[0] == today:
                return {'checked': 0}
            s._usage['ai_check_at'] = now
        if not s.save_usage({'ai_check_at': now}, counters=False):
            s._daily_log('AI credit: check skipped; usage save failed')
            return {'checked': 0}
        try:
            req = urllib.request.Request('https://openrouter.ai/api/v1/key', headers={'Authorization': 'Bearer ' + key})
            with s._connector_open(req, timeout=8) as response:
                raw = response.read(16385)
                if len(raw) > 16384:
                    raise ValueError('credit response size')
                data = json.loads(raw)['data']
            numbers = {k: data[k] for k in NUMBERS}
            if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or v < 0)
                   for v in numbers.values()):
                raise ValueError('credit numbers')
            if numbers['limit'] is not None and numbers['limit_remaining'] is None:
                raise ValueError('credit remaining')
            saved = {**numbers, 'reset_days': RESET_DAYS[data['limit_reset']], 'checked_at': now}
            s.save_usage({'ai_credit': saved}, counters=False)
        except Exception as error:
            failure(s, error, now, counters=False)
        warn(s, now)
        return {'checked': 1}
