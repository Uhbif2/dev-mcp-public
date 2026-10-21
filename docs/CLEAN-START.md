# Clean-start: CI / второй ПК (TZ-19)

## Граница

Координатор в dev_mcp.fleet.clean_start — **не** среда запуска. Он не
клонирует проекты на рабочем ноутбуке, не вызывает shell и не читает секреты.
Доверенный контроллер формирует CleanStartSpec с pinned commit_sha, source_sha,
lock_sha, env_sha, artifact_sha, outcome_sha, workflow, second_pc_plan_id,
target и минимальными RAM/disk.

Доверенный probe возвращает ResourceProfile: ci.run или second_pc.run,
абсолютный изолированный workspace_base, онлайн/занятость человеком,
свободные ресурсы и только **имена** доступных секретов. Значения секретов
остаются у отдельно защищённого Operator. При равных условиях приоритет
у CI; если CI не подходит, выбирается доступный и **не занятый человеком**
второй ПК. Если недостаточно ресурсов или оба заняты — waiting.
Если ресурсы доступны, но не хватает секрета — defect.
Если CI уже зарезервирован для другого задания, атомарная попытка его
резервирования отклоняется, после чего координатор пробует свободный
second_pc.run. Повтор того же задания не уходит на другой ресурс.

## Атомарность и доказательства

1. SQLite BEGIN IMMEDIATE сохраняет task_id, request_sha, nonce и lease
   resource_id → task_id **до** внешнего вызова. Два задания не получают
   одну бронь одновременно. execution_id=nonce обеспечивает идемпотентность
   в Operator. Повтор ожидающей попытки не перезапускает операцию.
2. Вызывается только валидированная операция каталога: ci.run с commit_sha и
   workflow либо second_pc.run с artifact_sha256 и plan_id. Workspace
   создаётся из workspace_base/clean-<nonce>. Удалённый адаптер обязан
   обнаруживать непустой каталог, заново клонировать и устанавливать
   зависимости; грязное состояние — отказ.
3. Operator возвращает ограниченное публичное доказательство с
   request_fingerprint, subject_version, env_version, ref и verdict.
   Координатор проверяет fingerprint всех параметров и pinned версии.
4. Независимый attestor возвращает аутентифицированное **целиком**
   наблюдение того же nonce, operator_ref, ресурса, workspace и всех pinned
   SHA. Три шага: clone(commit_sha), install(lock_sha), run(outcome_sha +
   user_visible=true). Обязательны пустой workspace до клонирования,
   отсутствие старых артефактов, реальный end-to-end outcome, timestamp;
   все стадии verdict=pass.
5. Только proof Operator **и** подтверждённый независимым verifier witness
   дают status=verified. Если live runner/attestor недоступен, выдаётся
   waiting или defect, **никогда успех по одному rc=0**.
6. Receipt хранится с SHA-256 канонического JSON. Повтор читает сохранённый
   результат без внешнего эффекта, даже если ресурс теперь offline.
   Несовпадение SHA, повреждённый JSON, изменение spec → defect.
   Незавершённую бронь можно разрешить только через trusted reconciliation;
   автоматический перехват запрещён. Все SQLite-соединения закрываются
   после транзакции, в том числе при ошибке.

## Интеграция и ограничения

В доверенном процессе внедряются fleet_op(operation, params),
fetch_attestation(resource_id, nonce, ref), verify_attestation(observation).
Последний callback **должен** криптографически проверять подпись, issuer и
все поля payload с закреплённым публичным ключом; флага signed=true
недостаточно. Operator и attestor изолированы от агента и обязаны проверить,
что workspace принадлежит выделенной чистой среде, а не рабочему каталогу
владельца. Этот модуль **не содержит** live-адаптера, хранилища секретов
или реализации pinned-key verifier, поэтому его наличие не подтверждает,
что проект реально прошёл clean-start на чужой машине.

Протокол synthetic acceptance не обращается к реальному CI или второму ПК:
все вызовы используют fake Operator и fake attestor. Команда:

    .venv/Scripts/python.exe -m pytest -q tests/fleet/test_clean_start.py

Враждебные сценарии: CI offline, второй ПК занят человеком, нехватка
RAM/disk/секретов, отказ Operator, повторное использование грязного
workspace, несовпадение SHA/nonce/версий и пользовательского outcome,
подмена SHA/JSON в журнале, повтор после исчезновения ресурса и
конкуренция задач за один lease.
