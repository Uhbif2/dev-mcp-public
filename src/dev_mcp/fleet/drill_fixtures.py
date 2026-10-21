"""Synthetic, hermetic fault fixtures. No production adapters or credentials."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping


@dataclass(frozen=True)
class FaultFixture:
    name: str
    healthy: Mapping[str, str]
    broken: Mapping[str, str]
    route: tuple[str, ...]

    @property
    def expected_outcome(self) -> str:
        return "independent state probe matches the recorded healthy baseline"


@dataclass(frozen=True)
class FaultDiagnosis:
    """A typed simulated fault accepted by a domain-specific probe."""
    cause_code: str
    obstacle_type: str
    evidence_kind: str
    fault_marker: str


EXPECTED: dict[str, FaultDiagnosis] = {
    "red_gate": FaultDiagnosis("gate_red_own", "defect", "run", "signed-gate-red"),
    "resource_missing": FaultDiagnosis("resource_down", "external_fact", "measure", "world-sensor-down"),
    "corrupt_file": FaultDiagnosis("apply_failed_schema", "defect", "run", "json-parse-error"),
    "false_external_blocker": FaultDiagnosis("procedure_unknown", "external_fact", "external", "unverified-blocker"),
    "interrupted_delivery": FaultDiagnosis("producer_failed", "defect", "delivery", "receipt-not-delivered"),
}


def diagnose(project: Path, fixture: FaultFixture) -> FaultDiagnosis | None:
    """Never dispatch a fix if the corresponding typed fault is not observed."""
    try:
        if fixture.name == "red_gate":
            failed = (project / "gate/status").read_text(encoding="utf-8") == "fail\n"
        elif fixture.name == "resource_missing":
            failed = (project / "resources/worker").read_text(encoding="utf-8") == "missing\n"
        elif fixture.name == "corrupt_file":
            try:
                json.loads((project / "artifacts/result.json").read_text(encoding="utf-8"))
                failed = False
            except json.JSONDecodeError:
                failed = True
        elif fixture.name == "false_external_blocker":
            failed = json.loads((project / "obstacles/claim.json").read_text(encoding="utf-8")) == {
                "blocked": True, "source": "unverified",
            }
        elif fixture.name == "interrupted_delivery":
            failed = json.loads((project / "delivery/receipt.json").read_text(encoding="utf-8")) == {
                "delivered": False,
            }
        else:
            return None
    except (OSError, ValueError, UnicodeError):
        return None
    return EXPECTED[fixture.name] if failed else None


def apply_recovery(project: Path, fixture: FaultFixture, diagnosis: FaultDiagnosis) -> bool:
    """Five typed simulated suppliers; never mutate without a matching fault."""
    if diagnose(project, fixture) != diagnosis or EXPECTED.get(fixture.name) != diagnosis:
        return False
    if fixture.name == "red_gate":
        (project / "gate/status").write_text("pass\n", encoding="utf-8")
    elif fixture.name == "resource_missing":
        (project / "resources/worker").write_text("ready\n", encoding="utf-8")
    elif fixture.name == "corrupt_file":
        (project / "artifacts/result.json").write_text(
            fixture.healthy["artifacts/result.json"], encoding="utf-8")
    elif fixture.name == "false_external_blocker":
        (project / "obstacles/claim.json").write_text('{"blocked":false}\n', encoding="utf-8")
    elif fixture.name == "interrupted_delivery":
        (project / "delivery/receipt.json").write_text('{"delivered":true}\n', encoding="utf-8")
    else:
        return False
    return True


def recheck_domain(project: Path, fixture: FaultFixture, diagnosis: FaultDiagnosis) -> bool:
    """Independent semantic probe, separate from the supplier's claim."""
    if EXPECTED.get(fixture.name) != diagnosis:
        return False
    try:
        if fixture.name == "red_gate":
            return (project / "gate/status").read_text(encoding="utf-8") == "pass\n"
        if fixture.name == "resource_missing":
            return (project / "resources/worker").read_text(encoding="utf-8") == "ready\n"
        if fixture.name == "corrupt_file":
            return json.loads((project / "artifacts/result.json").read_text(encoding="utf-8")) == {"valid": True}
        if fixture.name == "false_external_blocker":
            return json.loads((project / "obstacles/claim.json").read_text(encoding="utf-8")) == {"blocked": False}
        if fixture.name == "interrupted_delivery":
            return json.loads((project / "delivery/receipt.json").read_text(encoding="utf-8")) == {"delivered": True}
    except (OSError, ValueError, UnicodeError):
        return False
    return False


FIXTURES: tuple[FaultFixture, ...] = (
    FaultFixture(
        "red_gate", {"gate/status": "pass\n"}, {"gate/status": "fail\n"},
        ("gate:detect_red", "work:repair_fixture", "gate:independent_recheck"),
    ),
    FaultFixture(
        "resource_missing", {"resources/worker": "ready\n"},
        {"resources/worker": "missing\n"},
        ("resource:observe", "work:restore_fixture", "resource:independent_recheck"),
    ),
    FaultFixture(
        "corrupt_file", {"artifacts/result.json": '{"valid":true}\n'},
        {"artifacts/result.json": '{"valid":'},
        ("file:detect_corruption", "work:restore_fixture", "file:independent_parse_and_hash"),
    ),
    FaultFixture(
        "false_external_blocker", {"obstacles/claim.json": '{"blocked":false}\n'},
        {"obstacles/claim.json": '{"blocked":true,"source":"unverified"}\n'},
        ("blocker:challenge_source", "work:remove_false_blocker", "blocker:independent_recheck"),
    ),
    FaultFixture(
        "interrupted_delivery", {"delivery/receipt.json": '{"delivered":true}\n'},
        {"delivery/receipt.json": '{"delivered":false}\n'},
        ("delivery:detect_missing_receipt", "work:resume_fixture", "delivery:independent_receipt"),
    ),
)


def fingerprint(files: Mapping[str, str]) -> str:
    canonical = json.dumps(dict(files), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _relative(name: str) -> Path:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or "\\" in name or ":" in name
            or any(part in {"", ".", ".."} for part in path.parts)
            or str(path) != name):
        raise ValueError("invalid fixture path")
    return Path(*path.parts)


def write_state(project: Path, state: Mapping[str, str]) -> None:
    """Only the hard-coded fixture paths can be written inside a private project."""
    for name, text in state.items():
        rel = _relative(name)
        target = project / rel
        if target.is_symlink():
            raise ValueError("symlink in fixture")
        target.parent.mkdir(parents=True, exist_ok=True)
        if any(parent.is_symlink() for parent in target.parents if parent != project.parent):
            raise ValueError("symlink parent in fixture")
        target.write_text(text, encoding="utf-8")


def observe(project: Path, fixture: FaultFixture) -> tuple[str, bool]:
    """Independent state probe; also rejects extra files or symlink substitution."""
    found: dict[str, str] = {}
    expected = set(fixture.healthy)
    for path in project.rglob("*"):
        if path.is_symlink():
            return "", False
        if path.is_file() and path.name != ".drill-owner":
            rel = path.relative_to(project).as_posix()
            if rel not in expected:
                return "", False
            try:
                found[rel] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return "", False
    if set(found) != expected:
        return "", False
    if fixture.name == "corrupt_file":
        try:
            json.loads(found["artifacts/result.json"])
        except (ValueError, KeyError):
            return fingerprint(found), False
    return fingerprint(found), found == dict(fixture.healthy)


def restore_fixture(project: Path, fixture: FaultFixture) -> None:
    """Simulated repair; never invokes the real fleet controller."""
    write_state(project, fixture.healthy)
