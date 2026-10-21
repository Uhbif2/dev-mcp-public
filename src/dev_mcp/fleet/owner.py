"""Роль владельца для вкладки ChatGPT: пароль → пульт флота.

Зачем. Надзор за флотом — ставить написанные ТЗ в очередь, одобрять draft, снимать задачи
с раздачи, править приоритеты, рассылать сообщения — до 22.09.2026 умел только Claude Code
с локальным `fleet.cmd`. Владелец отдаёт это агенту в своей вкладке ChatGPT, чтобы надзор
не зависел от лимитов Claude Code. Обычный агент флота этих прав не получает: их открывает
пароль.

Урезанный вариант (решение владельца 22.09.2026). Пароль открывает только пульт — закрытый
список подкоманд `fleet.cmd`. Записи и запуска в самом dev-mcp он не даёт, периметр
`policy.toml` не меняет: писать ТЗ в `ToDo/tz` проектов агенты могут и без пароля. Полный
вариант (запись и запуск в dev-mcp) отвергнут: агент, сломавший dev-mcp, не починит его
через сломанный dev-mcp, а удалённая правка собственного периметра — отдельный риск.

Что это за граница. Сессия живёт только в памяти процесса dev-mcp, а не в реестре: агент с
`run` может выполнить любой python и переписать sqlite, но выдать себе сессию так не
сможет. На диске — только scrypt-хеш пароля; имя файла подпадает под deny-маску `*secret*`,
поэтому инструменты dev-mcp его не читают и не пишут. От агента, который осознанно ломает
периметр через `run` (`python -c` в exec_root исполняет что угодно, в том числе может
подменить файл хеша), это не защищает — как и всё остальное в dev-mcp. Пароль разделяет
роли для честных агентов и не даёт обычной вкладке случайно стать владельцем.

Перезапуск dev-mcp сбрасывает все сессии: после restart владелец разблокируется снова.

Выдача без пароля (23.09.2026, решение владельца) — основной путь. Пароль в аргументе
инструмента не дошёл до сервера ни разу: система безопасности OpenAI отклоняла
`fleet_owner_unlock` на стороне ChatGPT, а затем и все действия в том же чате. Секрет в чате
не нужен: доверие даёт локальная машина владельца. Вкладка делает `fleet_join(label='owner…')`,
владелец локально — `fleet.cmd owner-grant [W]` — и роль выдана этому worker_id на срок.
Выдача лежит в `data/owner-secret-grants.json` (маска `*secret*`: инструменты dev-mcp файл не
читают и не пишут) и, в отличие от сессии по паролю, переживает перезапуск dev-mcp.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SESSION_TTL_S = 12 * 3600
MAX_FAILURES = 5  # столько неверных паролей подряд за FAILURE_WINDOW_S →
FAILURE_WINDOW_S = 15 * 60  # …разблокировка закрыта на LOCKOUT_S для всех
LOCKOUT_S = 15 * 60
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # без 0/o, 1/l/i — пароль вставляют руками


def grants_path(secret_file: Path) -> Path:
    """Выдачи роли без пароля — рядом с хешем: data/owner-secret-grants.json."""
    return Path(secret_file).with_name("owner-secret-grants.json")


def read_grants(path: Path) -> dict[str, float]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for worker, until in data.items():
        try:
            out[str(worker)] = float(until)
        except (TypeError, ValueError):
            continue
    return out


def _write_grants(path: Path, grants: dict[str, float]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(grants, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def grant(path: Path, worker_id: str, ttl_s: int = SESSION_TTL_S, *, now: float | None = None) -> float:
    """Выдать роль владельца worker_id до now+ttl_s (только локально); вернуть срок."""
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        raise ValueError("нужен worker_id")
    now = time.time() if now is None else now
    grants = {w: u for w, u in read_grants(path).items() if u > now}
    grants[worker_id] = now + ttl_s
    _write_grants(path, grants)
    return grants[worker_id]


def revoke_grant(path: Path, worker_id: str | None = None) -> list[str]:
    """Снять выдачу у worker_id (None — у всех); вернуть, у кого снята."""
    grants = read_grants(path)
    gone = sorted(grants) if worker_id is None else [w for w in grants if w == str(worker_id).strip()]
    if gone:
        _write_grants(path, {w: u for w, u in grants.items() if w not in gone})
    return gone


def secret_path(base_dir: Path) -> Path:
    """Файл хеша: data/owner-secret.json рядом с реестром флота (data/ не в git)."""
    return Path(base_dir) / "data" / "owner-secret.json"


def generate_password(groups: int = 5, size: int = 4) -> str:
    """`k7qm-2xpa-9rtd-vw4h-c8ne` — около 98 бит, читается и вставляется без ошибок."""
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(size)) for _ in range(groups))


def _digest(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)


def set_password(path: Path, password: str) -> None:
    password = str(password or "").strip()
    if len(password) < 12:
        raise ValueError("пароль короче 12 символов")
    salt = secrets.token_bytes(16)
    record = {
        "algo": "scrypt",
        "params": {k: v for k, v in _SCRYPT.items() if k != "dklen"},
        "salt": salt.hex(),
        "hash": _digest(password, salt).hex(),
        "set_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def verify_password(path: Path, password: str) -> bool:
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(_digest(str(password or "").strip(), salt), expected)


@dataclass
class OwnerSessions:
    """Сессии владельца в памяти процесса: worker_id → момент истечения."""

    secret_file: Path
    ttl_s: int = SESSION_TTL_S
    clock: Callable[[], float] = time.time
    _until: dict[str, float] = field(default_factory=dict)
    _failures: list[float] = field(default_factory=list)
    _locked_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def configured(self) -> bool:
        return Path(self.secret_file).is_file()

    def unlock(self, worker_id: str, password: str) -> tuple[bool, str]:
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            return False, "нужен worker_id: сначала fleet_join"
        if not self.configured():
            return False, "пароль владельца не задан: владелец задаёт его локально командой fleet.cmd owner-password"
        now = self.clock()
        with self._lock:
            if now < self._locked_until:
                left = int((self._locked_until - now) // 60) + 1
                return False, f"слишком много неверных паролей — разблокировка закрыта ещё {left} мин"
        ok = verify_password(self.secret_file, password)  # scrypt вне замка: он медленный
        with self._lock:
            if not ok:
                self._failures = [t for t in self._failures if now - t < FAILURE_WINDOW_S] + [now]
                if len(self._failures) >= MAX_FAILURES:
                    self._locked_until = now + LOCKOUT_S
                    self._failures.clear()
                    return False, f"неверный пароль; {MAX_FAILURES} ошибок подряд — разблокировка закрыта на {LOCKOUT_S // 60} мин"
                return False, "неверный пароль"
            self._failures.clear()
            self._until[worker_id] = now + self.ttl_s
        return True, f"сессия владельца открыта для {worker_id} на {self.ttl_s // 3600} ч (до перезапуска dev-mcp)"

    def is_owner(self, worker_id: str) -> bool:
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            return False
        now = self.clock()
        granted = read_grants(grants_path(self.secret_file)).get(worker_id)
        if granted is not None and now < granted:
            return True
        with self._lock:
            until = self._until.get(worker_id)
            if until is None:
                return False
            if now >= until:
                del self._until[worker_id]
                return False
            return True

    def lock(self, worker_id: str) -> None:
        worker_id = str(worker_id or "").strip()
        with self._lock:
            self._until.pop(worker_id, None)
        if worker_id:
            revoke_grant(grants_path(self.secret_file), worker_id)

    def active(self) -> list[str]:
        now = self.clock()
        granted = {w for w, until in read_grants(grants_path(self.secret_file)).items() if until > now}
        with self._lock:
            return sorted(granted | {w for w, until in self._until.items() if until > now})
