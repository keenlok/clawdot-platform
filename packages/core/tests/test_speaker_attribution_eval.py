import importlib.util
from pathlib import Path

import pytest

EVAL_PATH = Path(__file__).parents[3] / "evals" / "speaker_attribution" / "run_eval.py"
SPEC = importlib.util.spec_from_file_location("speaker_attribution_eval", EVAL_PATH)
assert SPEC is not None and SPEC.loader is not None
EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVAL)
score = EVAL.score


@pytest.mark.parametrize(
    ("case", "reply", "wrong"),
    [
        ("ambient-only-a", "Hi nickname-A! Here are some ideas.", True),
        ("reply-to-other's-answer", "Member C, I can fix your split.", True),
        ("ambient-only-a", "Member B, here are ideas", False),
        ("summary", "You asked…", True),
    ],
)
def test_speaker_attribution_eval_scores_wrong_addressee_and_summary(case, reply, wrong):
    assert score(case, reply) is wrong
