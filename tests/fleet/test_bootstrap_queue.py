"""Bootstrap host lock must queue without a 40-minute rejection."""
import ast
import io
import msvcrt
from pathlib import Path
from types import SimpleNamespace


def test_waiter_survives_40_minutes_and_eventually_acquires(monkeypatch, tmp_path):
    # Execute the real source function with a virtual clock and a fake OS
    # byte lock; no 40-minute sleep and no live host mutex are involved.
    source = Path(__file__).resolve().parents[2] / "tools" / "fleet.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    function = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_bootstrap_slot"
    )
    now = [0.0]
    attempts = []
    def clock():
        return now[0]
    def sleep(seconds):
        now[0] += 2401 if not now[0] else seconds
    def locking(handle, mode, amount):
        attempts.append((mode, amount))
        if len(attempts) <= 2:
            raise OSError("busy")
    monkeypatch.setattr(msvcrt, "locking", locking)
    ns = {
        "_BOOTSTRAP_LOCK": tmp_path / "bootstrap.lock",
        "_BOOTSTRAP_WAIT_S": 40 * 60,
        "time": SimpleNamespace(monotonic=clock, sleep=sleep),
        "sys": SimpleNamespace(stderr=io.StringIO()),
        "open": open,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), ns)
    ns["_bootstrap_slot"]()
    try:
        assert now[0] > 2400
        assert len(attempts) == 3
    finally:
        ns["_BOOTSTRAP_HANDLE"].close()
