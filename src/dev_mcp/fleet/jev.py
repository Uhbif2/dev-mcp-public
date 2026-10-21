"""Jev (TypeSafe System One) — быстрый оракул вероятностей для маршрутизации флота.

Зачем (27.09.2026). Регулярки по тексту отказа ревьюера путают «owner config» в дефекте кода с
«нужен владелец». Проверка на 80 исторических отказах: где Jev и регулярка расходились (9 из 80),
прав почти всегда был Jev; на 70 ТЗ «упрётся ли задача во владельца» — AUC 0,78. Ответ за ~0,4 с.

Используется только там, где ошибка обратима: отказ, который почти наверняка упирается во
владельца или физический мир, замораживает fix-задачу (владелец снимает `fleet.cmd reactivate`),
вместо круга fix → review → reject. Нет ключа, сеть, таймаут, мусор в ответе → None, и флот ведёт
себя как раньше.

Ключ — `TYPESAFE_API_KEY` в окружении dev-mcp, файл по пути `DEV_MCP_JEV_KEY_FILE`, иначе
env из `~/.claude/settings.json` (там он живёт для Claude Code; `DEV_MCP_JEV_FROM_CLAUDE=0` — не брать).

Запасной бэкенд (17.10.2026, владелец: «Jev — бери из OpenRouter»). На OpenRouter нет оракула
System One — есть `typesafe/jev-router`, обычный chat completion, который сам выбирает модель;
цена плавающая. Поэтому он ВЫКЛЮЧЕН, пока владелец не задал бюджет: файл
`data/fleet/jev-openrouter.json` → `{"enabled": true, "daily_usd": 0.10}`. Ключ —
`OPENROUTER_API_KEY` из окружения. Траты считаются по `usage.cost` ответа в
`data/fleet/jev-spend.json`; достигнут дневной лимит → None, флот ведёт себя как без Jev.

Бесплатный бэкенд (17.10.2026): локальный релей ai-relay `127.0.0.1:8790` (пул ключей Gemini,
денег не тратит) — те же вопросы промптом, ответ JSON. ВЫКЛЮЧЕН: замер 17.10 — flash-lite
отвечает 0.9 на всё (при пороге 0.85 это ложные заморозки), квота gemini-2.5-flash в пуле
кончается за пару вызовов, gemini-3.8-flash — 8–12 с. Включать только после проверки калибровки
на исторических отказах: `data/fleet/jev-gemini.json` → `{"enabled": true}`. Порядок: TypeSafe
(если есть кредиты) → Gemini → OpenRouter (только с бюджетом владельца).
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

URL = 'https://api.typesafe.ai/v1/systemone'
TIMEOUT_S = float(os.environ.get('DEV_MCP_JEV_TIMEOUT_S', 4))
OWNER_GATE_MIN = float(os.environ.get('DEV_MCP_JEV_OWNER_GATE_MIN', 0.85))

# Системный HTTP(S)_PROXY на машине ведёт во внешний прокси — к API ходим напрямую.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

OWNER_GATE_QUESTION = {
    'type': 'noul',
    'instructions': (
        'Ревьюер отклонил сдачу задачи. Можно ли устранить причину отказа ТОЛЬКО с участием '
        'владельца проекта или физического мира: визуальный вердикт владельца, реальный '
        'телефон/устройство/железо, покупка или трата денег, секрет/аккаунт, решение владельца? '
        'Если агент может исправить сам правкой кода, тестов или документов — ответ нет.'
    ),
    'criteria': {'true': 'нужен владелец или физический мир', 'false': 'агент может исправить сам'},
}


def _key() -> str:
    key = os.environ.get('TYPESAFE_API_KEY', '').strip()
    if key:
        return key
    path = os.environ.get('DEV_MCP_JEV_KEY_FILE', '').strip()
    if path:
        try:
            return Path(path).read_text(encoding='utf-8').strip()
        except OSError:
            return ''
    # Единственное место, где ключ живёт на машине, — env Claude Code (~/.claude/settings.json).
    # Читаем оттуда, а не копируем: ключ не размножается по конфигам (27.09.2026).
    if os.environ.get('DEV_MCP_JEV_FROM_CLAUDE', '1') != '0':
        try:
            settings = json.loads((Path.home() / '.claude' / 'settings.json').read_text(encoding='utf-8-sig'))
            return str((settings.get('env') or {}).get('TYPESAFE_API_KEY') or '').strip()
        except (OSError, ValueError):
            return ''
    return ''


OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
OPENROUTER_MODEL = os.environ.get('DEV_MCP_JEV_OPENROUTER_MODEL', 'typesafe/jev-router')
_DATA = Path(__file__).resolve().parents[3] / 'data' / 'fleet'
_SPEND_LOCK = threading.Lock()


def _openrouter_config(data: Path | None = None) -> dict[str, Any]:
    try:
        cfg = json.loads(((data or _DATA) / 'jev-openrouter.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _today() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def spent_today(data: Path | None = None) -> float:
    try:
        ledger = json.loads(((data or _DATA) / 'jev-spend.json').read_text(encoding='utf-8'))
        return float(ledger.get(_today(), 0.0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def _record_spend(usd: float, data: Path | None = None) -> None:
    path = (data or _DATA) / 'jev-spend.json'
    with _SPEND_LOCK:
        try:
            ledger = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(ledger, dict):
                ledger = {}
        except (OSError, ValueError):
            ledger = {}
        ledger[_today()] = round(float(ledger.get(_today(), 0.0)) + max(0.0, usd), 6)
        path.write_text(json.dumps(dict(sorted(ledger.items())[-60:]), indent=1), encoding='utf-8')


def _openrouter_budget(data: Path | None = None) -> float:
    """Остаток дневного бюджета, USD; 0 — бэкенд выключен или исчерпан."""
    cfg = _openrouter_config(data)
    if cfg.get('enabled') is not True or not os.environ.get('OPENROUTER_API_KEY', '').strip():
        return 0.0
    try:
        cap = float(cfg.get('daily_usd', 0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, cap - spent_today(data))


def _openrouter_prompt(state: Any, questions: dict[str, Any]) -> str:
    spec = {}
    for name, q in questions.items():
        kind = q.get('type')
        shape = ({'probabilities': {str(k): 'p' for k in (q.get('criteria') or {})}}
                 if kind == 'choice' else {'noul': 'p'})
        spec[name] = {'type': kind, 'instructions': q.get('instructions'),
                      'criteria': q.get('criteria'), 'answer_shape': shape}
    for name, q in questions.items():
        crit = q.get('criteria') or {}
        if q.get('type') != 'choice' and 'true' in crit:
            spec[name]['meaning'] = ('noul = вероятность, что верно: «' + str(crit['true']) +
                                     '»; 0 — верно обратное: «' + str(crit.get('false', '')) + '»')
    return ('Ты — откалиброванный оракул вероятностей. Для каждого вопроса оцени вероятность по '
            'данным STATE. noul — вероятность утверждения из поля meaning, число из [0,1]; '
            'choice — вероятности вариантов, сумма 1. Будь осторожен с крайними значениями. '
            'Ответь ТОЛЬКО JSON-объектом {имя_вопроса: answer_shape}, без пояснений.\n'
            'STATE: ' + json.dumps(state, ensure_ascii=False)[:6000] + '\nQUESTIONS: ' +
            json.dumps(spec, ensure_ascii=False))


def _ask_openrouter(state: Any, questions: dict[str, Any], timeout_s: float,
                    *, opener: Any = None, data: Path | None = None) -> dict[str, Any] | None:
    if _openrouter_budget(data) <= 0:
        return None
    body = json.dumps({
        'model': OPENROUTER_MODEL, 'max_tokens': 400, 'temperature': 0,
        'response_format': {'type': 'json_object'}, 'usage': {'include': True},
        'messages': [{'role': 'user', 'content': _openrouter_prompt(state, questions)}],
    }).encode('utf-8')
    req = urllib.request.Request(OPENROUTER_URL, data=body, method='POST', headers={
        'authorization': 'Bearer ' + os.environ.get('OPENROUTER_API_KEY', '').strip(),
        'content-type': 'application/json', 'x-title': 'dev-mcp fleet'})
    try:
        with (opener or _opener).open(req, timeout=max(timeout_s, 15.0)) as resp:
            reply = json.loads(resp.read())
    except Exception:  # noqa: BLE001 — оракул необязателен
        return None
    try:
        _record_spend(float((reply.get('usage') or {}).get('cost') or 0.0), data)
    except (TypeError, ValueError, OSError):
        pass
    try:
        text = reply['choices'][0]['message']['content'].strip()
        if text.startswith('```'):
            text = text.strip('`').split('\n', 1)[-1]
        answers = json.loads(text[text.index('{'):text.rindex('}') + 1])
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    return answers if isinstance(answers, dict) else None


# 17.10: TypeSafe отвечал 402 «no available API credits», а флот считал Jev настроенным и на
# каждом разборе ходил в мёртвый API. Отказ по оплате/ключу выключает TypeSafe на час.
_TYPESAFE_DEAD_S = 3600.0
_typesafe_dead_until = 0.0


def _typesafe_alive() -> bool:
    import time
    return bool(_key()) and time.monotonic() >= _typesafe_dead_until


GEMINI_URL = os.environ.get('DEV_MCP_JEV_GEMINI_URL', 'http://127.0.0.1:8790/v1beta')
GEMINI_MODEL = os.environ.get('DEV_MCP_JEV_GEMINI_MODEL', 'gemini-2.5-flash')
_gemini_dead_until = 0.0


def _gemini_enabled(data: Path | None = None) -> bool:
    import time
    try:
        cfg = json.loads(((data or _DATA) / 'jev-gemini.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cfg = {}
    return (isinstance(cfg, dict) and cfg.get('enabled') is True and
            time.monotonic() >= _gemini_dead_until)


def _parse_json_answer(text: str) -> dict[str, Any] | None:
    try:
        text = text.strip()
        answers = json.loads(text[text.index('{'):text.rindex('}') + 1])
    except (ValueError, AttributeError):
        return None
    return answers if isinstance(answers, dict) else None


def _ask_gemini(state: Any, questions: dict[str, Any], timeout_s: float,
                *, opener: Any = None, data: Path | None = None) -> dict[str, Any] | None:
    global _gemini_dead_until
    if not _gemini_enabled(data):
        return None
    body = json.dumps({
        'contents': [{'parts': [{'text': _openrouter_prompt(state, questions)}]}],
        # 17.10, замер на 6 размеченных отказах: flash-lite отвечает 0.9 на всё (ложные
        # заморозки при пороге 0.85); flash без рассуждений различает. thinkingBudget 0 —
        # иначе рассуждения съедают maxOutputTokens и JSON обрывается.
        'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0,
                             'maxOutputTokens': 400, 'thinkingConfig': {'thinkingBudget': 0}},
    }).encode('utf-8')
    req = urllib.request.Request(f'{GEMINI_URL}/models/{GEMINI_MODEL}:generateContent', data=body,
                                 method='POST', headers={'content-type': 'application/json'})
    try:
        with (opener or _opener).open(req, timeout=timeout_s) as resp:
            reply = json.loads(resp.read())
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, 'reason', None), ConnectionRefusedError):
            import time
            _gemini_dead_until = time.monotonic() + 300.0  # релей не поднят — не долбить
        return None
    except Exception:  # noqa: BLE001 — оракул необязателен
        return None
    try:
        return _parse_json_answer(reply['candidates'][0]['content']['parts'][0]['text'])
    except (KeyError, IndexError, TypeError):
        return None


def _fallback(state: Any, questions: dict[str, Any], timeout_s: float) -> dict[str, Any] | None:
    answers = _ask_gemini(state, questions, timeout_s)
    return answers if answers is not None else _ask_openrouter(state, questions, timeout_s)


def ask(state: Any, questions: dict[str, Any], *, timeout_s: float = TIMEOUT_S) -> dict[str, Any] | None:
    global _typesafe_dead_until
    if not _typesafe_alive():
        return _fallback(state, questions, timeout_s)
    key = _key()
    body = json.dumps({'model': 'jev-latest', 'state': state, 'questions': questions}).encode('utf-8')
    req = urllib.request.Request(
        URL, data=body, method='POST',
        headers={'authorization': f'Bearer {key}', 'content-type': 'application/json'},
    )
    try:
        with _opener.open(req, timeout=timeout_s) as resp:
            answers = json.loads(resp.read()).get('answers')
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 402, 403):
            import time
            _typesafe_dead_until = time.monotonic() + _TYPESAFE_DEAD_S
            return _fallback(state, questions, timeout_s)
        return None
    except Exception:  # noqa: BLE001 — оракул необязателен, любой сбой = «не знаю»
        return None
    return answers if isinstance(answers, dict) else None


def noul(answers: dict[str, Any] | None, name: str) -> float | None:
    try:
        value = float(answers[name]['noul'])  # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        return None
    return value if 0.0 <= value <= 1.0 else None


def configured() -> bool:
    """Есть ли ключ. Отличает «оракула нет совсем» (работаем без него) от «сеть моргнула» (повторим)."""
    return _typesafe_alive() or _gemini_enabled() or _openrouter_budget() > 0


def choice(answers: dict[str, Any] | None, name: str) -> tuple[str, float] | None:
    """(выбранный вариант, его вероятность) для вопроса типа choice; None — не знаем."""
    try:
        probs = {str(k): float(v) for k, v in answers[name]['probabilities'].items()}  # type: ignore[index]
        best = max(probs, key=probs.__getitem__)
    except (TypeError, KeyError, ValueError, AttributeError):
        return None
    return best, probs[best]


def owner_gate_probability(note: str) -> float | None:
    """P(причину отказа снимает только владелец/физический мир). None — не знаем."""
    text = (note or '').strip()
    if len(text) < 40:
        return None
    return noul(ask({'reject_note': text[:6000]}, {'o': OWNER_GATE_QUESTION}), 'o')
