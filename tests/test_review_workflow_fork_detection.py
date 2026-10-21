"""Fork detection in the review workflow compares repositories, not `fork`.

`head.repo.fork` is true for an internal branch whenever the repository is
itself a fork, which would skip the review and block the gate on every PR.
"""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/claude-code-review.yml"
SAME_REPO_TEST = "github.event.pull_request.head.repo.full_name != github.repository"


def test_fork_detection_compares_repository_names():
    text = WORKFLOW.read_text()
    assert "head.repo.fork" not in text
    review_if = text.split("  claude-review:\n", 1)[1].split("    if: ${{ ", 1)[1].split(" }}", 1)[0]
    assert SAME_REPO_TEST in review_if
    gate = text.split("  review-gate:\n", 1)[1]
    is_fork = next(line for line in gate.splitlines() if line.strip().startswith("IS_FORK:"))
    assert SAME_REPO_TEST in is_fork

