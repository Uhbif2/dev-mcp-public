"""Registry identity is a canonical directory, independent of a task/source name.

Inactive rows keep their audit history. They are not aliases to reactivate, and
two active queues must never compete over the same physical project directory.
"""
from __future__ import annotations

from typing import Any

from .model import norm_path


class ProjectIdentityError(ValueError):
    pass


def projects_at(tx: Any, root: str) -> list[Any]:
    key = norm_path(root)
    return [row for row in tx.query("SELECT name,root,active FROM projects ORDER BY name")
            if norm_path(str(row['root'])) == key]


def require_available(tx: Any, name: str, root: str) -> None:
    """Call inside the registration transaction, before any registry mutation."""
    matches = projects_at(tx, root)
    active = [str(row['name']) for row in matches if row['active'] and row['name'] != name]
    if active:
        raise ProjectIdentityError('project_root_already_registered: ' + ', '.join(active))
    named = tx.one('SELECT root,active FROM projects WHERE name=?', (name,))
    if named and norm_path(str(named['root'])) != norm_path(root):
        raise ProjectIdentityError('project_name_bound_to_another_root: ' + name)
    if named and not named['active']:
        raise ProjectIdentityError('project_disabled_requires_explicit_owner_reactivation: ' + name)
