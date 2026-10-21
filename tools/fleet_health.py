"""Часовая проверка прогресса флота (для агента-смотрителя): по проектам выдачи/приёмки/отклонения/возвраты/отказы, причины отказов, замок интеграции, стадии интенций. Запуск: py -3 tools/fleet_health.py"""
import collections
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

c = sqlite3.connect(str(Path(__file__).resolve().parent.parent / "data" / "fleet" / "fleet.sqlite"))
t = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
rows = collections.defaultdict(collections.Counter)
for p, k in c.execute("select project,kind from events where ts>? and kind in "
                      "('take','accept','reject','requeue','claim_take_refused')", (t,)):
    rows[p or "-"][k] += 1
for p, cnt in sorted(rows.items(), key=lambda x: -sum(x[1].values())):
    print(f"{p[:18]:18} take {cnt['take']:3} acc {cnt['accept']:3} rej {cnt['reject']:3} "
          f"requeue {cnt['requeue']:3} refused {cnt['claim_take_refused']:5}")
why = collections.Counter()
for (pl,) in c.execute("select payload from events where kind='claim_take_refused' and ts>?", (t,)):
    why[str(json.loads(pl or "{}").get("reason"))[:70]] += 1
print("refusals:", why.most_common(3))
created = collections.Counter(tz.split(":")[0] for (tz,) in c.execute(
    "select tz from tasks where project='Fleet Platform' and created_at>?", (t,)))
print("FP created 1h:", dict(created))
# Интенции: общий датчик доказанного прогресса, тот же, что у сторожа.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dev_mcp.fleet.progress_watch import front_snapshot
c.row_factory = sqlite3.Row

class Snapshot:
    def query(self, sql, params=()):
        return [dict(row) for row in c.execute(sql, params)]
    def one(self, sql, params=()):
        row = c.execute(sql, params).fetchone()
        return dict(row) if row else None

for front in front_snapshot(Snapshot(), datetime.now(timezone.utc)):
    flag = ' << СТОИТ' if front['stalled'] else ''
    print(f"intent {front['project'][:28]:28} {front['stage']:17} "
          f"{front['state']:16} · без шага {front['minutes_without_progress']} мин"
          f"{flag} | {front['reason'][:140]}")
c.close()
