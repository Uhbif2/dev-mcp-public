# Контракт результатов — Fleet Platform TZ-02

Модель добавляется к существующей очереди, а не заменяет её. Источник истины —
одна SQLite Store: outcomes, evidence, obstacles, decisions, outcome_work.
Миграция install_schema(store) выполняется атомарно через Store.tx(), использует
собственную outcome_schema_version=1 и **не меняет** старые таблицы или
Store.SCHEMA_VERSION. Повторный вызов безопасен, Store.healthy() сохранён.

## Результат и дерево

outcomes.id — стабильный ID, parent_id — родитель в дереве, project —
проект, intent_id — интенция. title — короткое описание; spec — JSON цели
и версии требований; evidence_spec — JSON критерия доказательства;
authority — JSON разрешённых путей, операций, бюджетов. state —
open|done|dropped; rev начинается с 1 и увеличивается при закрытии;
created_ts и closed_ts — UTC ISO-8601.

Родитель должен существовать, быть открыт и принадлежать тому же проекту;
узел с открытыми детьми закрыть нельзя. Обязательные JSON — непустые объекты
до 64 КиБ, строго сериализуемые (NaN запрещён).

API: create_outcome, get_outcome, close_outcome. Связь работы с результатом
создаёт link_work(store, outcome_id, project, tz). Поле в tasks не добавляется:
используется outcome_work(outcome_id, project, tz). Связь может предшествовать
созданию задачи, но исполняема только существующая задача tasks.state=ready.

## Доказательство

evidence: id, outcome_id, kind=run|measure|screen|delivery|review|external,
ref (путь/id прогона), обязательная непустая subject_version (commit/хеш),
env_version, verdict=pass|fail, producer, ts.

add_evidence(..., writer=server_evidence_writer(store)) — **только для
доверенного серверного верификатора**, который сам проверил артефакт и
версии. Обычный конструктор ServerEvidenceWriter и writer чужого Store
отвергаются. **Нельзя публиковать server_evidence_writer как агентский
MCP-инструмент или создавать evidence по рапорту агента.** Python-модуль
не является криптографической границей; настоящая граница — серверный маршрут.

## Препятствие

obstacles: id, outcome_id, type, cause_code, predicate (JSON проверяемого
условия), change_source (датчик или ID результата-поставщика), resolver_work,
owner_action (одна фраза только для типа owner_action), state=open|resolved, ts.
Типы: external_fact, needs_result, unknown_method, defect, owner_action.

Начальный закрытый перечень cause_code:

| Код | Причина |
| --- | --- |
| path_forbidden | Запрещён путь |
| resource_down | Недоступен ресурс или устройство |
| gate_red_foreign | Красный чужой гейт |
| gate_red_own | Красный собственный гейт |
| needs_file_outside_owns | Нужен файл вне своей зоны |
| acceptance_impossible | Невозможно получить доказательство |
| procedure_unknown | Неизвестен проверяемый способ |
| apply_failed_schema | Ошибка структуры при применении |
| apply_failed_transient | Временный сбой применения |
| producer_failed | Исполнитель не произвёл результат |
| owner_physical | Физическое действие владельца |
| owner_taste | Решение вкуса владельца |
| owner_money | Деньги/финансовое разрешение |
| owner_private_data | Личные данные/доступ владельца |

**Новый cause_code добавляется только вместе с обработчиком в TZ-04.**
open_obstacle отвергает неизвестные причины, resolve_obstacle снимает только
открытое препятствие. Для needs_result поставщик задаётся
change_source=ID_результата и структурным predicate с requires_outcome.
Для датчиков задавайте ключ sensor/check/value, а не text/note.

Самоожидание и циклы не запрещены на этапе записи: их должен находить
инвариант и передавать на ремонт.

## Следующий шаг и инвариант

next_step(store, outcome) возвращает один NextStep(kind,id):

| kind | id / семантика |
| --- | --- |
| work | project/tz существующей готовой работы |
| wait | obstacle_id наблюдаемого ожидания |
| owner | obstacle_id одного конкретного действия владельца |
| done | закрытый узел либо диагностический fallback без планируемого шага |

Если родитель имеет открытых детей, возвращается первый доступный шаг
первого ребёнка в стабильном порядке. Для открытого невалидного листа
kind=done **не значит, что цель достигнута**: контроллер TZ-03 обязан
сначала проверить check_invariant(store) -> list[Violation].

| Код нарушения | Условие |
| --- | --- |
| no_next_step | Открытый лист без готовой работы, наблюдаемого ожидания или owner_action |
| self_wait | Ожидание доказательства самого результата без поставщика |
| wait_cycle | Цикл (в том числе 3+ узлов) в графе needs_result |
| prose_control | Пустой или только текстовый predicate вместо машинного условия |

У валидного листа приоритет: готовая работа, затем действие владельца или
наблюдаемое препятствие в порядке создания. API не запускает контроллер,
не меняет старые состояния задач, не считает отчёт агента доказательством.

decisions(id,scope,text,source,reversible,rollback,ts) — общая книга
решений для будущего контроллера; её запись/интеграция вне TZ-02.

## Приёмка и границы

Команда: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/fleet/test_outcome.py

Проверяются дерево, миграция в той же SQLite, все четыре шага, все четыре
нарушения, циклы 2/3 узлов, серверный evidence без пустой subject_version,
враждебный JSON, неизвестный cause_code, удалённый/чужой родитель,
FK и rollback. Эта библиотека ещё не является включённым контроллером,
оператором доставки или продовой интеграцией — это TZ-03/TZ-04.
