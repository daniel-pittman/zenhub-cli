"""Fork detection in the review workflow compares repositories, not `fork`.

`head.repo.fork` is true for an internal branch whenever the repository is
itself a fork, which would skip the review and block the gate on every PR.
The gate decides FORK in its verdict step, from a same-repository comparison,
before any other verdict.
"""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/claude-code-review.yml"
SAME_REPO_TEST = "github.event.pull_request.head.repo.full_name == github.repository"


def _job(text: str, name: str) -> str:
    lines = text.split(f"  {name}:\n", 1)[1].splitlines()
    end = next(
        (i for i, line in enumerate(lines) if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":")),
        len(lines),
    )
    return "\n".join(lines[:end])


def _job_if(text: str, name: str) -> str:
    return _job(text, name).split("    if: ${{ ", 1)[1].split(" }}", 1)[0]


def test_fork_detection_compares_repository_names():
    text = WORKFLOW.read_text()
    assert "head.repo.fork" not in text
    assert "IS_FORK" not in text
    for job in ("await-tests", "claude-review", "verdict-extract", "clear-stale-ack"):
        assert SAME_REPO_TEST in _job_if(text, job), job


def test_gate_decides_fork_first_in_its_verdict_step():
    gate = _job(WORKFLOW.read_text(), "review-gate")
    verdict_step = gate.split("- name: Evaluate the verdict\n", 1)[1]
    env_line = next(line for line in verdict_step.splitlines() if line.strip().startswith("SAME_REPO:"))
    assert SAME_REPO_TEST in env_line
    run = verdict_step.split("run: |\n", 1)[1]
    fork_at = run.index('if [ "$SAME_REPO" != true ]; then\n            verdict=FORK')
    assert fork_at < run.index("verdict=INCONCLUSIVE")
    assert "Gate: FORK.**" in gate
