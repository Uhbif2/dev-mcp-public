"""Stage proof checker for intent projects. Standard library only.

The same file is copied into every intent project as ``.intent/check.py`` so the
agent can run its acceptance (``py -3 .intent/check.py <stage>``), while the
server imports THIS trusted copy and re-runs it before writing evidence. An
agent editing the project copy therefore cannot change what the server proves.
"""
from __future__ import annotations

import re
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RUN_TIMEOUT_S = 900
LENSES = ("визуал", "пустые и ошибочные состояния", "первый запуск", "ресурсы",
          "безопасность", "документация", "восстановление")

KINDS = frozenset(("build", "change", "investigate", "ops"))
TRIPLE_STAGES = frozenset(("understood", "approach_selected", "hypotheses_plan"))


def size(root: Path) -> str:
    """Размер из доказанного understood.md; строки нет — large (прежнее поведение)."""
    file = root / "intent" / "understood.md"
    if not file.is_file():
        return "large"
    values = re.findall(r"^size:\s*(small|medium|large)\s*$",
                        file.read_text(encoding="utf-8", errors="replace"), re.M | re.I)
    return values[0].lower() if len(values) == 1 else "large"


def routing(root: Path, *, allowed_roots: tuple[Path, ...] = ()) -> tuple[str, str]:
    """Read route only from the proved understood artifact, not the raw intent."""
    file = root / "intent" / "understood.md"
    if not file.is_file():
        raise ValueError("нет intent/understood.md")
    contents = file.read_text(encoding="utf-8", errors="replace")
    fields = {}
    for name in ("kind", "target"):
        values = re.findall(rf"^{name}:\s*(.+?)\s*$", contents, re.M | re.I)
        if len(values) != 1:
            raise ValueError(f"нужна одна строка {name}:")
        fields[name] = values[0].strip()
    kind, target = fields["kind"].lower(), fields["target"]
    if kind not in KINDS or not target:
        raise ValueError("неизвестный kind или пустой target")
    if (kind == "build") != (target == "new"):
        raise ValueError("build требует target: new; остальные — существующий путь")
    if kind != "build":
        path = Path(target)
        if (not path.is_absolute() or ".." in path.parts or not path.is_dir()
                or path.is_symlink() or
                os.path.normcase(str(path.absolute())) != os.path.normcase(
                    str(path.resolve(strict=True)))):
            raise ValueError("target должен быть существующей канонической папкой")
        if allowed_roots:
            canonical = path.resolve(strict=True)
            if not any(canonical != allowed.resolve(strict=True) and canonical.is_relative_to(
                    allowed.resolve(strict=True)) for allowed in allowed_roots
                    if allowed.is_dir() and not allowed.is_symlink()):
                raise ValueError("target находится вне разрешённых корней")
    return kind, target


def _triple(root: Path, stage: str) -> list[str]:
    problems: list[str] = []
    for index in range(1, 4):
        problems.extend(_doc(root, f"candidates/{stage}/{index}.md",
                             ("Анализ", "Доказательства", "Вывод")))
    problems.extend(_doc(root, f"consensus/{stage}.md",
                         ("Сравнение", "Расхождения", "Спайк", "Решение")))
    # Gemini is invoked by the trusted stage prover, not accepted as an
    # agent-written 'model: gemini' claim.
    return problems


def _experiments(root: Path) -> list[str]:
    files = sorted((root / "experiments").glob("*/README.md"))
    if not files:
        return ["нет experiments/<id>/README.md"]
    problems: list[str] = []
    for file in files:
        contents = file.read_text(encoding="utf-8", errors="replace")
        for heading in ("Гипотеза", "Команда", "Результат", "Среда"):
            if not re.search(rf"^##\s+{heading}\s*$", contents, re.M | re.I):
                problems.append(f"{file}: нет ## {heading}")
        command = re.findall(r"^command:\s*(\S.+)$", contents, re.M | re.I)
        if len(command) != 1:
            problems.append(f"{file}: нужна одна команда воспроизведения (command: ...)")
        artifacts = re.findall(r"^artifact:\s*(\S+)\s*$", contents, re.M | re.I)
        if not artifacts:
            problems.append(f"{file}: нет ссылок на сохранённые результаты (artifact: ...)")
        for relative in artifacts:
            path = file.parent / relative
            if not path.resolve(strict=False).is_relative_to(file.parent.resolve()) or (
                    not path.is_file() or path.is_symlink()):
                problems.append(f"{file}: нет безопасного результата {relative}")
        if len(contents.strip()) < 200:
            problems.append(f"{file}: эксперимент не документирован")
    return problems


def _doc(root: Path, name: str, headings: tuple[str, ...]) -> list[str]:
    path = root / "intent" / name
    if not path.is_file():
        return [f"нет intent/{name}"]
    text = path.read_text(encoding="utf-8", errors="replace")
    found = {h.strip().lower() for h in re.findall(r"^##\s+(.+)$", text, re.M)}
    problems = [f"intent/{name}: нет раздела «## {h}»" for h in headings if h.lower() not in found]
    if len(text.strip()) < 400:
        problems.append(f"intent/{name}: меньше 400 символов — это не разбор, а заглушка")
    return problems


def _compiles(root: Path, script: str) -> list[str]:
    path = root / "intent" / script
    if not path.is_file():
        return [f"нет intent/{script}"]
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
        return [f"intent/{script} не компилируется: {exc}"]
    return []


def _run(root: Path, script: str, *, expect_ok: bool) -> list[str]:
    path = root / "intent" / script
    if not path.is_file():
        return [f"нет intent/{script}"]
    try:
        # A script path puts intent/ alone on sys.path; module execution keeps
        # the project root available to the same real package imports as -m tests.
        # A private cache also prevents same-second/equal-size source edits
        # from reusing stale pyc during repeated proof of the current bytes.
        with tempfile.TemporaryDirectory(prefix='fleet-stage-pycache-') as cache:
            done = subprocess.run([sys.executable, '-X', f'pycache_prefix={cache}',
                                   '-m', 'intent.' + Path(script).stem], cwd=root, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return [f"intent/{script}: дольше {RUN_TIMEOUT_S} с"]
    if (done.returncode == 0) != expect_ok:
        tail = (done.stdout + done.stderr)[-1500:]
        want = "успех" if expect_ok else "провал (до кода сценарии красные)"
        return [f"intent/{script}: код {done.returncode}, ожидался {want}\n{tail}"]
    return []


def _receipt(root: Path, name: str) -> list[str]:
    path = root / "intent" / name
    if not path.is_file():
        return [f"нет intent/{name}"]
    text = path.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"^result:\s*pass\s*$", text, re.M | re.I):
        return [f"intent/{name}: нет строки «result: pass»"]
    return []


def _lenses(root: Path) -> list[str]:
    problems = _doc(root, "lenses.md", LENSES)
    path = root / "intent" / "lenses.md"
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.search(r"\bP[01]\b", line) and re.search(r"\bоткрыт", line, re.I):
                problems.append(f"intent/lenses.md: открытый дефект — {line.strip()[:160]}")
    return problems


def check(root: Path, stage: str, *, strict: bool = False,
          triple: bool = False) -> list[str]:
    """Return problems; an empty list means the stage is proven."""
    root = Path(root)
    if stage.startswith("candidate:"):
        _, subject, index = stage.split(":")
        if subject not in TRIPLE_STAGES or index not in ("1", "2", "3"):
            return ["неизвестная кандидатская стадия"]
        return _doc(root, f"candidates/{subject}/{index}.md",
                    ("Анализ", "Доказательства", "Вывод"))
    if stage == "owner_brief":
        return _owner_brief(root / "intent" / "owner-brief.md")
    if stage == "understood":
        errors = _doc(root, "understood.md", ("Интенция", "Готовые решения",
                                              "Что уже есть", "Ограничения",
                                              "Неясности", "Нужно от владельца"))
        if True:  # маршрут kind/target нужен и без тройки (14.10)
            try:
                routing(root)
            except ValueError as exc:
                errors.append(str(exc))
        return errors + (_triple(root, stage) if triple else [])
    if stage == "executable_goal":
        return (_doc(root, "goal.md", ("Результат", "Сценарии", "Вне рамок"))
                + _run(root, "scenarios.py", expect_ok=False))
    if stage == "executable_goal_published":
        # Перепроверка уже принятой стадии (integrate revalidate): «сценарии красные» было
        # правдой до кода и закономерно ложно после него — проверяем документ и что сценарии есть.
        return (_doc(root, "goal.md", ("Результат", "Сценарии", "Вне рамок"))
                + _compiles(root, "scenarios.py"))
    if stage == "approach_selected":
        return (_doc(root, "approaches.md", ("Варианты", "Замер", "Выбор", "Откат"))
                + (_triple(root, stage) if triple else []))
    if stage == "executable_plan":
        problems = _doc(root, "plan.md", ("Работы", "Зависимости", "Приёмка"))
        if not list((root / "ToDo" / "tz").glob("TZ-*.md")):
            problems.append("нет ToDo/tz/TZ-*.md — план не превращён в ТЗ флота")
        return problems
    if stage == "implemented":
        return _run(root, "scenarios.py", expect_ok=True)
    if stage == "clean_start":
        return _receipt(root, "clean-start.md")
    if stage == "quality":
        return _lenses(root)
    if stage == "delivered":
        return _doc(root, "delivery.md", ("Где", "Откат")) + _run(root, "smoke.py", expect_ok=True)
    if stage == "handed_over":
        return _doc(root, "handover.md", ("Готово", "От владельца"))
    if stage == "hypotheses_plan":
        return (_doc(root, "hypotheses.md", ("Гипотезы", "Эксперименты",
                                            "Метрики", "Воспроизведение", "Риски"))
                + (_triple(root, stage) if triple else []))
    if stage == "experiments":
        return _experiments(root)
    if stage == "verified":
        return _doc(root, "verification.md", ("Повторы", "Независимый агент",
                                              "Критика Gemini", "Расхождения"))
    if stage == "report":
        return _doc(root, "report.md", ("Вывод", "Эксперименты", "Уверенность", "Что дальше"))
    if stage == "rollback_plan":
        return _doc(root, "rollback-plan.md", ("План", "Риски", "Откат",
                                               "Проверка", "Нужно от владельца"))
    if stage == "owner_approved":
        return _receipt(root, "owner-approval.md")
    if stage == "executed":
        return (_doc(root, "execution.md", ("Команды", "Результат", "Откат", "Проверка"))
                + _receipt(root, "execution.md"))
    return [f"неизвестная стадия {stage}"]


# Text for the owner on the TickTick board (19.10): read weeks later, without context — it must
# explain itself. Lives here, not in C:/hub/_lib: the fleet's run refuses absolute paths (20.10).
_BRIEF_FORBIDDEN = [
    (re.compile(r"[A-Za-z]:\\"), "путь к файлу"),
    (re.compile(r"\b(?:TZ|ТЗ)-\d+|\brequest:|\bclaim\b|\boutcome\b|\b[0-9a-f]{8,}\b", re.I), "внутренний id"),
    (re.compile(r"`"), "код в обратных кавычках"),
    (re.compile(r"\.(?:md|py|json|toml|txt)\b"), "имя файла"),
    (re.compile(r"\b(?:ops|build|stage|gate|acceptance|owns|evidence|pipeline|rollback|stack)\b", re.I),
     "внутренний термин"),
]


def _owner_brief(path: Path) -> list[str]:
    if not path.is_file():
        return ["нет файла intent/owner-brief.md"]
    text = path.read_text(encoding="utf-8")
    parts: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().casefold()
            parts[current] = []
        elif current is not None:
            parts[current].append(line)
    short = "\n".join(parts.get("коротко", [])).strip()
    errors = []
    if not short:
        errors.append("нет раздела «## Коротко»")
    else:
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", short) if s.strip()]
        if not 2 <= len(sentences) <= 4:
            errors.append(f"«Коротко» — 2–4 предложения, сейчас {len(sentences)}")
        if len(short) > 600:
            errors.append("«Коротко» длиннее 600 знаков")
    if "вопросы" not in parts:
        errors.append("нет раздела «## Вопросы» (если вопросов нет — напиши «нет»)")
    for pattern, what in _BRIEF_FORBIDDEN:
        hit = pattern.search(text)
        if hit:
            errors.append(f"{what}: «{hit.group(0)}» — объясни словами")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check.py <stage>", file=sys.stderr)
        return 2
    try:  # «✗» в консоли cp1251 ронял проверку трейсбеком вместо списка проблем
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    root = Path(__file__).resolve().parent.parent
    settings_file = root / ".intent" / "options.json"
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.is_file() else {}
    except ValueError:
        settings = {}
    problems = check(root, argv[1], strict=bool(settings.get("triple")),
                     triple=bool(settings.get("triple")))
    for problem in problems:
        print("✗", problem)
    if not problems:
        print("✓", argv[1])
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
