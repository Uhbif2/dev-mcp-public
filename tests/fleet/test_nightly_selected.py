"""B6 (25.11.2026): кандидаты, переданные итогу ночи, учитываются по ID в отчёте."""
from __future__ import annotations

import json

from dev_mcp.fleet import nightly_review


def test_report_lists_every_selected_candidate_with_disposition():
    answer = json.dumps({"findings": [{"severity": "high", "location": "src/a.py:10",
                                       "summary": "дефект A", "evidence": "строка 10"}],
                         "duplicates": [], "disputed": []}, ensure_ascii=False)
    selected = [{"id": "c1", "severity": "high", "location": "src/a.py:10", "summary": "дефект A"},
                {"id": "c2", "severity": "medium", "location": "src/b.py:5", "summary": "дефект B"}]
    text = nightly_review.render_summary("P", "2026-11-25", answer, pending_count=3, selected=selected)
    assert "## Переданы итогу (2; в топе по месту — 1)" in text
    assert "`c1`" in text and "(в топе)" in text
    assert "`c2`" in text and "(без отметки итога)" in text


def test_report_without_selected_keeps_old_layout():
    text = nightly_review.render_summary("P", "2026-11-25", "{}", pending_count=0)
    assert "Переданы итогу" not in text
