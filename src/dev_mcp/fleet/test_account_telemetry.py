"""Regression for TZ-307 server-side account-overload telemetry."""
from __future__ import annotations

import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dev_mcp.fleet import account_telemetry, flow
from dev_mcp.fleet.store import Store


class AccountTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "fleet.sqlite")
        self.store = Store.open(self.db)
        self.now = int(datetime.now(timezone.utc).timestamp() * 1000)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def send(self, data):
        return account_telemetry.record(self.store, json.dumps(data))

    def test_roundtrip_flow_and_idempotence(self):
        o1 = self.now - 30 * 60_000
        o2 = self.now - 10 * 60_000
        a = {"type": "overload", "at": o1, "chats_1h": 2,
             "chats_3h": 4, "chats_5h": 7, "pause_min": 10}
        b = {"type": "overload", "at": o2, "chats_1h": 3,
             "chats_3h": 6, "chats_5h": 9, "pause_min": 20}
        self.assertTrue(self.send(a))
        self.assertFalse(self.send(a))
        self.assertTrue(self.send(b))
        r = {"type": "recovered", "at": self.now, "overload_at": o2}
        self.assertTrue(self.send(r))
        self.assertFalse(self.send(r))
        with closing(sqlite3.connect(self.db)) as conn:
            data = flow.flow(conn, [], flow.since_default())
        stats = data["account_limit"]
        self.assertEqual(stats["overloads"], 2)
        self.assertEqual(stats["recoveries"], 1)
        self.assertEqual(stats["median_chats_3h"], 5)
        self.assertAlmostEqual(stats["median_pause_min"], 10)
        self.assertTrue(any("лимит аккаунта:" in s and "перегрузок 2" in s
                            for s in flow.format_flow(data)))

    def test_bad_or_old_payload_does_not_write(self):
        bad = [
            {"type": "overload", "at": self.now, "chats_1h": True,
             "chats_3h": 2, "chats_5h": 3, "pause_min": 10},
            {"type": "overload", "at": self.now - 7 * 86_400_000,
             "chats_1h": 1, "chats_3h": 2, "chats_5h": 3, "pause_min": 10},
            {"type": "recovered", "at": self.now - 60_000,
             "overload_at": self.now},
            {"type": "recovered", "at": self.now, "overload_at": self.now,
             "sensitive": "leak"},
        ]
        for obj in bad:
            with self.subTest(obj=obj), self.assertRaises(ValueError):
                self.send(obj)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind IN ('account_overload', 'account_recovered')"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
