"""Read-only replay of saved overseer turns using the same free local Qwen model.

Example:
    python tools/overseer_replay.py <chat-id> --data-dir data/fleet
    python tools/overseer_replay.py data/fleet/overseer/replies/<chat-id>.jsonl

Never sends a ChatGPT/owner message, writes a card, or touches the live journal.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from dev_mcp.fleet import overseer  # noqa: E402


def replay_archive(path: Path, *,
                   generate: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
                   ) -> dict[str, Any]:
    """Evaluate archived turns in sequence. Only the local model is invoked."""
    generate = generate or overseer._local_generate
    owners: list[str] = []
    previous = ""
    card: dict[str, Any] = {"sent_semantics": [], "probe_errors": {}, "repeat_turns": 0}
    output: list[dict[str, Any]] = []
    counts = {"turns": 0, "messages": 0, "silences": 0, "duplicates": 0,
              "old_messages": 0, "old_silences": 0}
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            reply = str(row.get("reply") or "")
            for message in row.get("owner_replies") or []:
                if message:
                    owners.append(str(message))
            goals = overseer._owner_goals(owners)
            owner_key = overseer._sha("\n".join(goals))
            actions = row.get("agent_actions") or []
            probes = row.get("probes") or []
            evidence = {
                "owner_instructions": goals,
                "assistant_reply": reply[-12000:],
                "previous_assistant_reply": previous[-5000:],
                "agent_actions": actions[-25:],
                "probes": probes,
            }
            generated = generate(evidence) if goals and reply else None
            verdict, reason = overseer._evaluate_semantics(
                generated, goals, owners, reply, probes, card, owner_key)
            if not reply:
                reason = "missing_reply"
            mode = str(row.get("mode") or "send")
            if not reason and mode != "send":
                reason = "shadow"
            sent = not reason
            if sent and isinstance(generated, dict):
                card["sent_semantics"].append({
                    "owner_key": owner_key, "issue": generated["issue"],
                    "message": generated["message"], "verdict": verdict})
                card["sent_semantics"] = card["sent_semantics"][-30:]
                if verdict in overseer._STOPS:
                    card["owner_notice_key"] = owner_key
            old_sent = bool(row.get("sent_text"))
            counts["turns"] += 1
            counts["old_messages" if old_sent else "old_silences"] += 1
            counts["messages" if sent else "silences"] += 1
            if reason == "duplicate_semantics":
                counts["duplicates"] += 1
            output.append({
                "line": number, "turn": row.get("turn", ""),
                "before": f"{row.get('verdict', 'unknown')} → message" if old_sent
                          else f"silence ({row.get('skipped_reason') or 'unknown'})",
                "after": f"{verdict} → message" if sent else f"silence ({reason})",
                "before_text": str(row.get("sent_text") or ""),
                "after_text": (
                    overseer._archive_redact(f"{overseer.MARK} {generated['message']}")
                    if sent and isinstance(generated, dict) else ""),
            })
            previous = reply
    return {"rows": output, "summary": counts}


def replay_conversation(path: Path, *,
                        generate: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
                        checkpoint: Path | None = None,
                        max_new: int | None = None,
                        focus_indices: set[int] | None = None,
                        frozen_generations: dict[int, Any] | None = None
                        ) -> dict[str, Any]:
    """Replay real completed assistant turns with optional append-only model cache.

    Missing probe/action history is not invented. An interrupted live-Qwen run can
    resume from checkpoint without rerunning cached generations or resetting the
    duplicate-suppression card. Checkpoint is only for ignored local realcases data.
    Human usefulness must still be reviewed independently.
    """
    generate = generate or overseer._local_generate
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
        raise ValueError("Expected convdump JSON containing messages[]")
    signature = hashlib.sha256((
        json.dumps(document, ensure_ascii=False, sort_keys=True)
        + inspect.getsource(overseer._local_generate)
        + inspect.getsource(overseer._evaluate_semantics)
    ).encode("utf-8")).hexdigest()[:20]
    cached: dict[int, Any] = {}
    if checkpoint is not None:
        checkpoint = Path(checkpoint).resolve()
        allowed = (REPO / "data" / "fleet" / "overseer" / "realcases").resolve()
        if checkpoint.parent != allowed or checkpoint.suffix != ".jsonl":
            raise ValueError("Checkpoint must be JSONL directly under ignored realcases/")
        if checkpoint.is_file():
            for line in checkpoint.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("signature") != signature:
                    raise ValueError("Checkpoint belongs to a different export/model code")
                cached[int(record["message_index"])] = record.get("generated")
    if frozen_generations:
        cached.update(frozen_generations)
    owners: list[str] = []
    previous = ""
    card: dict[str, Any] = {"sent_semantics": [], "probe_errors": {}, "repeat_turns": 0}
    rows: list[dict[str, Any]] = []
    silences: dict[str, int] = {}
    new_count = 0
    total_turns = sum(isinstance(m, dict) and m.get("role") == "assistant"
                      and bool(m.get("complete", False)) for m in document["messages"])
    for number, item in enumerate(document["messages"], 1):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = str(item.get("text") or "")
        if role == "user":
            if text and not text.startswith(overseer.MARK):
                owners.append(text)
            continue
        if role != "assistant" or not item.get("complete", False):
            continue
        if focus_indices is not None and number not in focus_indices:
            previous = text
            continue
        goals = overseer._owner_goals(owners)
        owner_key = overseer._sha("\n".join(goals))
        evidence = {"owner_instructions": goals, "assistant_reply": text[-12000:],
                    "previous_assistant_reply": previous[-5000:],
                    "latest_owner_turn": owners[-1] if owners else "",
                    "agent_actions": [], "probes": []}
        if number in cached:
            generated = cached[number]
        else:
            if max_new is not None and new_count >= max_new:
                break
            generated = generate(evidence) if goals and text else None
            new_count += 1
            if checkpoint is not None:
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                record = {"signature": signature, "message_index": number,
                          "generated": generated}
                # Checkpoints must be locally redacted just like live archives.
                with checkpoint.open("a", encoding="utf-8") as stream:
                    stream.write(overseer._archive_redact(
                        json.dumps(record, ensure_ascii=False)) + "\n")
        verdict, reason = overseer._evaluate_semantics(
            generated, goals, owners, text, [], card, owner_key)
        if not text:
            reason = "missing_reply"
        if not reason and isinstance(generated, dict):
            card["sent_semantics"].append({
                "owner_key": owner_key, "issue": generated["issue"],
                "message": generated["message"], "verdict": verdict})
            card["sent_semantics"] = card["sent_semantics"][-30:]
            if verdict in overseer._STOPS:
                card["owner_notice_key"] = owner_key
        else:
            silences[reason] = silences.get(reason, 0) + 1
        rows.append({
            "message_index": number, "created": item.get("created"),
            "reply_glimpse": overseer._archive_redact(text[:180]),
            "verdict": verdict, "reason": reason or "sent",
            "message": overseer._archive_redact(str(generated["message"]))
                       if not reason and isinstance(generated, dict) else "",
            "review": "unrated",
        })
        previous = text
    return {"conversation": document.get("conversation"), "rows": rows,
            "summary": {"completed_assistant_turns": len(rows),
                        "total_assistant_turns": total_turns,
                        "complete": len(rows) == total_turns,
                        "new_generations_this_run": new_count,
                        "messages": sum(bool(r["message"]) for r in rows),
                        "silences": silences,
                        "human_evaluation": "pending"}}


def rejudge_frozen_conversation(path: Path, frozen_path: Path) -> dict[str, Any]:
    """Re-evaluate already-generated Qwen candidates under CURRENT production guards.

    This never invokes a model, and is therefore suitable for before/after
    comparisons when the validator code changed after generation. Checkpoint
    files must be from the ignored realcases directory; cache version is
    recorded, but the user explicitly permits rejudging different code.
    """
    allowed = (REPO / "data" / "fleet" / "overseer" / "realcases").resolve()
    frozen_path = Path(frozen_path).resolve()
    if frozen_path.parent != allowed or frozen_path.suffix != ".jsonl":
        raise ValueError("Frozen output must be JSONL directly under ignored realcases/")
    frozen = {}
    generation_signatures = set()
    for line in frozen_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if "signature" not in record or "message_index" not in record:
            raise ValueError("Malformed frozen generation")
        number = int(record["message_index"])
        if number in frozen:
            raise ValueError(f"Duplicate frozen message index: {number}")
        generation_signatures.add(record["signature"])
        frozen[number] = record.get("generated")
    if len(generation_signatures) != 1:
        raise ValueError("Frozen run mixes model versions")
    def never_call_model(_: dict[str, Any]) -> None:
        raise AssertionError("Frozen evaluation must never invoke live model")
    report = replay_conversation(
        path, generate=never_call_model, focus_indices=set(frozen),
        frozen_generations=frozen)
    report["frozen_generation_signature"] = next(iter(generation_signatures))
    report["rejudge_only"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline ChatGPT delivery, local free-model replay")
    parser.add_argument("chat", help="Chat id or path to archived JSONL")
    parser.add_argument("--data-dir", type=Path, default=REPO / "data" / "fleet")
    parser.add_argument("--json", action="store_true", help="Print machine-readable results")
    parser.add_argument("--out", type=Path, help="Save review table under ignored data/fleet/overseer/realcases")
    parser.add_argument("--checkpoint", type=Path, help="Append-only JSONL local model cache for resumable real replay")
    parser.add_argument("--max-new", type=int, default=None, help="Maximum uncached model invocations in one run")
    parser.add_argument("--focus", help="Comma-separated source message indices for diagnostic sampling")
    parser.add_argument("--model-metadata", action="store_true",
                        help="Print safe local-model timing/finish metadata; no prompt or response")
    parser.add_argument("--rejudge-cache", type=Path,
                        help="Apply current guards to frozen real Qwen candidates, without model calls")
    ns = parser.parse_args(argv)
    if ns.max_new is not None and ns.max_new < 1:
        parser.error("--max-new must be positive")
    try:
        focus = {int(part) for part in ns.focus.split(",")} if ns.focus else None
        if focus is not None and any(i <= 0 for i in focus):
            raise ValueError("indices must be positive")
    except ValueError:
        parser.error("--focus must contain positive comma-separated integers")
    is_conversation = ns.chat.endswith(".json")
    path = Path(ns.chat) if ns.chat.endswith((".json", ".jsonl")) else overseer.Book(ns.data_dir).reply_archive(ns.chat)
    if not path.is_file():
        parser.error(f"archive not found: {path}")
    def model_with_metadata(evidence: dict[str, Any]) -> dict[str, Any] | None:
        return overseer._local_generate(
            evidence, diagnostics=lambda data: print(
                "Qwen metadata: " + json.dumps(data, ensure_ascii=False), flush=True))
    if ns.rejudge_cache:
        if not is_conversation or ns.checkpoint or ns.max_new is not None:
            parser.error("--rejudge-cache expects conversation JSON without live checkpoint/max-new")
        report = rejudge_frozen_conversation(path, ns.rejudge_cache)
    else:
        report = (replay_conversation(
            path, generate=model_with_metadata if ns.model_metadata else None,
            checkpoint=ns.checkpoint, max_new=ns.max_new, focus_indices=focus)
            if is_conversation else replay_archive(path))
    if ns.out:
        destination = ns.out.resolve()
        allowed = (REPO / "data" / "fleet" / "overseer" / "realcases").resolve()
        if destination.parent != allowed or destination.suffix != ".json":
            parser.error("--out must name a JSON file directly inside data/fleet/overseer/realcases")
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if ns.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif is_conversation:
        for row in report["rows"]:
            print(f"{row['message_index']:4d}: {row['reply_glimpse'][:100]}")
            print(f"     {row['reason']}: {row['message'][:300] or '—'}")
        s = report["summary"]
        print(f"Итого {s['completed_assistant_turns']} реальных завершённых ходов: "
              f"кандидатов {s['messages']}, молчаний {sum(s['silences'].values())}; "
              "полезность сообщений требует ручной оценки")
    else:
        for row in report["rows"]:
            print(f"{row['line']:4d} {str(row['turn'])[:10]}: {row['before']} → {row['after']}")
            if row["before_text"] or row["after_text"]:
                print(f"     было: {row['before_text'][:220] or '—'}")
                print(f"     стало: {row['after_text'][:220] or '—'}")
        s = report["summary"]
        print(f"Итого {s['turns']} ходов: было {s['old_messages']} сообщений / "
              f"{s['old_silences']} молчаний; стало {s['messages']} сообщений / "
              f"{s['silences']} молчаний; дублей {s['duplicates']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
