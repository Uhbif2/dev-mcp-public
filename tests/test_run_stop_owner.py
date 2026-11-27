"""Isolation of background jobs by the launching agent."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from fastmcp import Client
from dev_mcp.audit import Audit
from dev_mcp.policy import Policy
from dev_mcp.runner import JobRegistry
from dev_mcp.server import build_server


async def call(server, name, args):
    async with Client(server) as client:
        reply = await client.call_tool(name, args)
        return reply.content[0].text


def make_server(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    config = tmp_path / 'policy.toml'
    config.write_text(f"roots = ['{root}']\n", encoding='utf-8')
    return build_server(Policy.load(config), Audit(tmp_path / 'logs' / 'audit.jsonl'))


@pytest.mark.asyncio
async def test_foreign_agent_cannot_stop_or_list_background_jobs(tmp_path, monkeypatch):
    own = SimpleNamespace(job_id='aaaaaaaaaaaa', agent='W-A')
    foreign = SimpleNamespace(job_id='bbbbbbbbbbbb', agent='W-B')
    jobs = {j.job_id: j for j in (own, foreign)}
    stopped = []
    monkeypatch.setattr(JobRegistry, 'get', lambda self, job_id: jobs.get(job_id))
    monkeypatch.setattr(JobRegistry, 'all', lambda self: list(jobs.values()))
    monkeypatch.setattr(JobRegistry, 'stop', lambda self, job_id: stopped.append(job_id) or True)
    monkeypatch.setattr('dev_mcp.server.describe_job', lambda job: job.job_id)
    server = make_server(tmp_path)

    refused = await call(server, 'run_stop', {'job_id': foreign.job_id, 'agent': 'W-A'})
    assert refused.startswith('Отказано:'), refused
    assert stopped == []
    listing = await call(server, 'run_list', {'agent': 'W-A'})
    assert own.job_id in listing
    assert foreign.job_id not in listing
    allowed = await call(server, 'run_stop', {'job_id': own.job_id, 'agent': 'W-A'})
    assert not allowed.startswith('Отказано:'), allowed
    operator = await call(server, 'run_list', {})
    assert all(j.job_id in operator for j in (own, foreign))
    operator_stop = await call(server, 'run_stop', {'job_id': foreign.job_id})
    assert not operator_stop.startswith('Отказано:'), operator_stop


@pytest.mark.asyncio
async def test_unowned_job_is_not_visible_to_agent(tmp_path, monkeypatch):
    unowned = SimpleNamespace(job_id='cccccccccccc', agent='')
    monkeypatch.setattr(JobRegistry, 'all', lambda self: [unowned])
    monkeypatch.setattr(JobRegistry, 'get', lambda self, job_id: unowned)
    monkeypatch.setattr(JobRegistry, 'stop', lambda self, job_id: pytest.fail('must not stop'))
    monkeypatch.setattr('dev_mcp.server.describe_job', lambda job: job.job_id)
    server = make_server(tmp_path)
    assert unowned.job_id not in await call(server, 'run_list', {'agent': 'W-A'})
    assert 'Отказано' in await call(server, 'run_stop', {'job_id': unowned.job_id, 'agent': 'W-A'})
