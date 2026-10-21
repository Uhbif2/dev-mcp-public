"""17.10: Jev через OpenRouter — только с бюджетом владельца; TypeSafe 402 не тормозит флот."""
from __future__ import annotations

import io
import json
import urllib.error

from dev_mcp.fleet import jev


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def open(self, req, timeout=0):
        self.calls.append(json.loads(req.data))
        return _Resp(json.dumps(self.reply).encode('utf-8'))


def _reply(content, cost=0.002):
    return {'choices': [{'message': {'content': content}}], 'usage': {'cost': cost}}


def test_disabled_without_owner_budget(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'k')
    opener = _Opener(_reply('{"o": {"noul": 0.9}}'))
    assert jev._ask_openrouter({'a': 1}, {'o': jev.OWNER_GATE_QUESTION}, 5, opener=opener, data=tmp_path) is None
    assert opener.calls == []


def test_answers_and_spend_until_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'k')
    (tmp_path / 'jev-openrouter.json').write_text('{"enabled": true, "daily_usd": 0.003}', encoding='utf-8')
    opener = _Opener(_reply('```json\n{"o": {"noul": 0.9}, "c": {"probabilities": {"a": 0.7, "none": 0.3}}}\n```'))
    questions = {'o': jev.OWNER_GATE_QUESTION, 'c': {'type': 'choice', 'instructions': 'x',
                                                     'criteria': {'a': 'A', 'none': 'нет'}}}
    answers = jev._ask_openrouter({'a': 1}, questions, 5, opener=opener, data=tmp_path)
    assert jev.noul(answers, 'o') == 0.9
    assert jev.choice(answers, 'c') == ('a', 0.7)
    assert opener.calls[0]['model'] == jev.OPENROUTER_MODEL
    assert jev.spent_today(tmp_path) == 0.002
    jev._ask_openrouter({'a': 1}, questions, 5, opener=opener, data=tmp_path)
    assert jev._ask_openrouter({'a': 1}, questions, 5, opener=opener, data=tmp_path) is None
    assert len(opener.calls) == 2, 'cap 0.003 reached after two calls of 0.002'


def test_garbage_reply_is_unknown_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'k')
    (tmp_path / 'jev-openrouter.json').write_text('{"enabled": true, "daily_usd": 1}', encoding='utf-8')
    assert jev._ask_openrouter({}, {'o': jev.OWNER_GATE_QUESTION}, 5,
                               opener=_Opener(_reply('не знаю')), data=tmp_path) is None


def test_typesafe_billing_error_disables_it(monkeypatch):
    monkeypatch.setattr(jev, '_key', lambda: 'ts-key')
    monkeypatch.setattr(jev, '_typesafe_dead_until', 0.0)
    monkeypatch.setattr(jev, '_openrouter_budget', lambda data=None: 0.0)

    class Dead:
        def open(self, req, timeout=0):
            raise urllib.error.HTTPError(jev.URL, 402, 'Payment Required', {}, io.BytesIO(b'{}'))

    monkeypatch.setattr(jev, '_opener', Dead())
    assert jev.configured() is True
    assert jev.ask({'reject_note': 'x' * 60}, {'o': jev.OWNER_GATE_QUESTION}) is None
    assert jev.configured() is False


def test_gemini_backend_off_unless_enabled_and_parses_reply(tmp_path):
    reply = {'candidates': [{'content': {'parts': [{'text': '{"o": {"noul": 0.2}}'}]}}]}
    opener = _Opener(reply)
    assert jev._ask_gemini({}, {'o': jev.OWNER_GATE_QUESTION}, 5, opener=opener, data=tmp_path) is None
    (tmp_path / 'jev-gemini.json').write_text('{"enabled": true}', encoding='utf-8')
    answers = jev._ask_gemini({}, {'o': jev.OWNER_GATE_QUESTION}, 5, opener=opener, data=tmp_path)
    assert jev.noul(answers, 'o') == 0.2
    assert opener.calls[0]['generationConfig']['thinkingConfig'] == {'thinkingBudget': 0}
