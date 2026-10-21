"""Точка входа dev-mcp.

Одна политика — `policy.toml` (профиль read-only + точечные write_roots/exec_roots).
Переменных окружения для режима намеренно нет: mcp-proxy запускает дочерние серверы
с `--pass-environment`, окружение родителя протекает целиком — режим, зависящий от
env, рано или поздно включился бы не там, где задумано.

`--check` — проверить policy.toml и выйти (не поднимая сервер). Этим пользуется
`check.cmd` / `restart.cmd`, чтобы поймать битый TOML до перезапуска.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import threading
from pathlib import Path

from dev_mcp.audit import Audit
from dev_mcp.fleet.integrate import run_integrator
from dev_mcp.fleet.controller import (
    ControllerProvisioning, load_trusted_delivery_provisioning, owner_intent_project, run_controller,
)
from dev_mcp.fleet.judgment import run_judgment
from dev_mcp.fleet.location import fleet_db_path
from dev_mcp.fleet.supervisor import supervise as supervise_fleet_chrome
from dev_mcp.policy import Policy, PolicyError, watch_policy_file
from dev_mcp.server import build_server

# Как долго вотчер спит между проверками mtime policy.toml. Правка, оставленная
# через tools/review_requests.py (или руками), долетает до уже запущенного
# сервера за это время — restart.cmd для одной только правки policy.toml больше
# не нужен (см. Policy.watch_policy_file).
POLICY_WATCH_INTERVAL_S = 3.0

HERE = Path(__file__).resolve().parents[2]
# This fixed OS-protected path is managed by a restricted Operator launcher,
# not by agent text, policy exec_roots, a project manifest or inherited env.
TRUSTED_OPERATOR_DELIVERY = (
    Path("C:/ProgramData/dev-mcp/operator-delivery.toml") if os.name == "nt"
    else Path("/etc/dev-mcp/operator-delivery.toml")
)


def _describe_plain(policy: Policy, policy_path: str) -> str:
    d = policy.describe()
    lines = [
        f'policy.toml: {policy_path}',
        f'  корни ({len(d["roots"])}):',
        *[f'    {r}' for r in d['roots']],
    ]
    if d['allow_write']:
        lines.append('  запись: во ВСЕ корни (allow_write=true)')
    elif d['write_roots']:
        lines.append(f'  запись только в ({len(d["write_roots"])}):')
        lines += [f'    {r}' for r in d['write_roots']]
    else:
        lines.append('  запись: запрещена (read-only)')
    lines.append(f'  исполнение (run) в ({len(d["exec_roots"])}):')
    lines += [f'    {r}' for r in d['exec_roots']]
    lines.append(f'  разрешённые программы: {", ".join(d["exec_allowed_bin"])}')
    if d['exec_bin_missing']:
        lines.append(f'  НЕ найдены в PATH (run для них ответит отказом): {", ".join(d["exec_bin_missing"])}')
    if d.get('exec_args_unrestricted'):
        lines.append(f'  без проверки путей в аргументах: {", ".join(d["exec_args_unrestricted"])}')
    lines.append(f'  скачивание из сети (fetch_url): {"да" if d["allow_network_fetch"] else "нет"}')
    if d['tasks']:
        lines.append(f'  задачи (run_task) ({len(d["tasks"])}):')
        for name, spec in d['tasks'].items():
            tail = '' if spec['runnable'] else f'   [НЕ ЗАПУСТИТСЯ: {spec["blocked_reason"]}]'
            lines.append(f'    {name}{tail}')
    return '\n'.join(lines)


def main() -> int:
    from dev_mcp import winproc
    winproc.install()  # 13.10: без консолей у детей — нет окон 0xc0000142 у владельца
    try:
        priority=winproc.normalize_serving_priority()
        if priority:
            print(f'dev-mcp: serving priority {priority}; child CPU=BelowNormal',file=sys.stderr)
    except Exception as exc:
        print(f'dev-mcp: serving priority unavailable: {type(exc).__name__}',file=sys.stderr)
    from dev_mcp import stackdump
    stackdump.install(HERE / 'logs')  # 14.10: зависание — touch logs/dump-stacks.request
    parser = argparse.ArgumentParser(prog='dev-mcp', description='Узкий MCP-сервер для работы с кодом')
    parser.add_argument(
        '--policy',
        default=str(HERE / 'policy.toml'),
        help='файл политики (по умолчанию policy.toml)',
    )
    parser.add_argument(
        '--audit',
        default=str(HERE / 'logs' / 'audit.jsonl'),
        help='файл журнала вызовов',
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='проверить файл политики и выйти (сервер не поднимать)',
    )
    opts = parser.parse_args()

    try:
        policy = Policy.load(opts.policy)
    except (PolicyError, OSError) as exc:
        stream = sys.stdout if opts.check else sys.stderr
        print(f'[X] policy.toml НЕ загружена ({opts.policy}):\n    {exc}', file=stream)
        if opts.check:
            print('\n    Исправь файл и запусти проверку снова.', file=stream)
        return 1

    if opts.check:
        print('[OK] policy.toml корректна.\n')
        print(_describe_plain(policy, opts.policy))
        return 0

    audit = Audit(opts.audit)
    audit.record(
        'server_start',
        {
            'policy': opts.policy,
            'allow_write': policy.allow_write,
            'roots': [str(r) for r in policy.roots],
            'exec_roots': [str(r) for r in policy.exec_roots],
            'exec_args_unrestricted': list(policy.exec_args_unrestricted),
            'allow_network_fetch': policy.allow_network_fetch,
        },
        decision='allow',
    )
    print(
        f'dev-mcp: политика {opts.policy}; запись: {"да" if policy.allow_write else "нет"}; '
        f'корней: {len(policy.roots)}; exec_roots: {len(policy.exec_roots)}; '
        f'сеть: {"да" if policy.allow_network_fetch else "нет"}; журнал: {opts.audit}',
        file=sys.stderr,
    )
    if policy._exec_bin_missing:
        print(f'dev-mcp: бинари не в PATH (run для них ответит отказом): {", ".join(policy._exec_bin_missing)}',
              file=sys.stderr)

    def _on_policy_change(ok: bool, message: str) -> None:
        audit.record(
            'policy_autoreload', {'policy': opts.policy},
            decision='allow' if ok else 'deny', reason=message,
        )
        tag = 'перечитан' if ok else f'ОШИБКА, старая политика в силе: {message}'
        print(f'dev-mcp: policy.toml изменился — {tag}', file=sys.stderr)

    stop_watch = threading.Event()
    watcher = threading.Thread(
        target=watch_policy_file,
        args=(policy, opts.policy),
        kwargs={'interval_s': POLICY_WATCH_INTERVAL_S, 'on_change': _on_policy_change, 'stop_event': stop_watch},
        daemon=True,
        name='dev-mcp-policy-watch',
    )
    watcher.start()

    # FleetChrome is an execution dependency, but its extension cannot resurrect the
    # browser after Chrome has exited.  Keep that outer self-heal loop in dev-mcp:
    # this process already knows the durable queue and survives browser shutdown.
    fleet_stop = threading.Event()
    from .listener_guard import start as start_listener_guard
    start_listener_guard(HERE, opts.policy, audit, fleet_stop)
    fleet_supervisor = threading.Thread(
        target=supervise_fleet_chrome,
        args=(fleet_db_path(audit.path.parent.parent), audit, fleet_stop),
        daemon=True,
        name='dev-mcp-fleet-chrome-supervisor',
    )
    fleet_supervisor.start()

    # Autonomous reconciliation: maintenance and outcome checks run even when
    # no agent calls fleet_take or fleet_list.
    if os.environ.get('DEV_MCP_FLEET_CONTROLLER', '1') != '0':
        provisioning = None
        try:
            if TRUSTED_OPERATOR_DELIVERY.is_file():
                provisioning = load_trusted_delivery_provisioning(
                    TRUSTED_OPERATOR_DELIVERY,
                    fleet_db_path(audit.path.parent.parent),
                )
        except Exception:
            # The source path, protected keys and target observation MUST NOT
            # leak to stderr or the agent audit trail. Invalid => fail closed.
            print('dev-mcp: trusted Operator delivery unavailable (fail closed)',
                  file=sys.stderr)
        # Owner intent inbox is always on: the folder is closed to agents by
        # policy, so its files are the owner's own grant (canon 00-final §7).
        provisioning = dataclasses.replace(
            provisioning or ControllerProvisioning(),
            intent_project=owner_intent_project,
        )
        threading.Thread(
            target=run_controller,
            args=(fleet_db_path(audit.path.parent.parent), fleet_stop),
            kwargs={'policy': policy, 'provisioning': provisioning},
            daemon=True,
            name='dev-mcp-fleet-controller',
        ).start()

    # Приёмка задачи ≠ рабочий проект: после каждого accept коммит путей задачи и гейт проекта
    # (fleet/integrate.py). Выключатель — DEV_MCP_FLEET_INTEGRATE=0.
    if os.environ.get('DEV_MCP_FLEET_INTEGRATE', '1') != '0':
        threading.Thread(
            target=run_integrator,
            args=(fleet_db_path(audit.path.parent.parent), fleet_stop),
            daemon=True,
            name='dev-mcp-fleet-integrator',
        ).start()

    # Задачи-суждения и умолчания решений (fleet/judgment.py). Выключатель — DEV_MCP_FLEET_JUDGMENT=0.
    if os.environ.get('DEV_MCP_FLEET_JUDGMENT', '1') != '0':
        threading.Thread(
            target=run_judgment,
            args=(fleet_db_path(audit.path.parent.parent), fleet_stop),
            daemon=True,
            name='dev-mcp-fleet-judgment',
        ).start()

    # Мельница памяти (fleet/mill.py): сбор опыта и раздача партий флоту. Выключатель — DEV_MCP_FLEET_MILL=0.
    if os.environ.get('DEV_MCP_FLEET_MILL', '1') != '0':
        from .fleet.mill import run_mill
        threading.Thread(
            target=run_mill,
            args=(fleet_db_path(audit.path.parent.parent), fleet_stop),
            kwargs={'policy': policy},
            daemon=True,
            name='dev-mcp-fleet-mill',
        ).start()

    try:
        build_server(policy, audit, policy_path=opts.policy).run()
    finally:
        fleet_stop.set()
        stop_watch.set()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
