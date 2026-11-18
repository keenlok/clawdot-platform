"""The expense workbook render must stay byte-for-byte unchanged as workbook kinds are added.

The fixture holds an expense-provider model and the SHA-256 of its rendered plan, both captured
from v0.21.0 before the general workbook tool was added.
"""

import dataclasses
import hashlib
import json
from pathlib import Path

from chatcore_google.workbook import render_workbook

FIXTURE = Path(__file__).parent / "fixtures" / "expense_workbook_golden.json"


def test_expense_workbook_render_is_unchanged():
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    plan = render_workbook(
        golden["model"],
        title=golden["title"],
        max_cells=golden["max_cells"],
        max_cell_chars=golden["max_cell_chars"],
    )
    canonical = json.dumps(
        dataclasses.asdict(plan), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    assert plan.cells == golden["cells"]
    assert hashlib.sha256(canonical.encode()).hexdigest() == golden["plan_sha256"]
