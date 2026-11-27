"""Recheck the complete public suite in balanced, process-isolated module shards."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dev_mcp.json_book import write_json

root = Path(sys.argv[1]).resolve()
out = root.parent
weights = {}
for case in ET.parse(out / 'pytest-public.xml').iter('testcase'):
    module = case.attrib['classname'].replace('.', '/') + '.py'
    weights[module] = weights.get(module, 0) + float(case.get('time', '0'))
modules = sorted(p.relative_to(root).as_posix() for p in (root / 'tests').rglob('test_*.py'))
bins = [[], []]
totals = [0.0, 0.0]
for module in sorted(modules, key=lambda p: (-weights.get(p, 1), p)):
    index = min(range(2), key=totals.__getitem__)
    bins[index].append(module)
    totals[index] += weights.get(module, 1)
write_json(out / 'pytest-shards.json', bins, indent=2)
assert sorted(bins[0] + bins[1]) == modules
processes = []
streams = []
try:
    for index, batch in enumerate(bins):
        stream = (out / f'pytest-final-{index}.log').open('w', encoding='utf-8')
        streams.append(stream)
        args = [str(root / '.venv/Scripts/python.exe'), '-m', 'pytest', *sorted(batch),
                '-q', '--tb=short', '-o', f'cache_dir=../pytest-cache-{index}',
                f'--junitxml=../pytest-final-{index}.xml']
        proc = subprocess.Popen(args, cwd=root, stdout=stream, stderr=subprocess.STDOUT,
                                env={**os.environ, 'PYTHONUTF8': '1'},
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        processes.append(proc)
        print(f'shard {index}: {len(batch)} modules; pid={proc.pid}', flush=True)
    deadline = time.monotonic() + 3600
    completed = set()
    while len(completed) < len(processes):
        if time.monotonic() > deadline:
            raise TimeoutError('public verification deadline')
        for index, proc in enumerate(processes):
            if index not in completed and proc.poll() is not None:
                completed.add(index)
                print(f'shard {index}: exit={proc.returncode}', flush=True)
        time.sleep(1)
    sys.exit(1 if any(p.returncode for p in processes) else 0)
finally:
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=15)
    for stream in streams:
        stream.close()
