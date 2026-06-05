"""Tests for the structural-relative duplicate downgrade (issue #46).

When bulk-loading a deeply-structured backlog, a child issue's body
legitimately matches the parent that enumerates it (a parent epic whose
body lists "Wave A, Wave B, ..." scores in the 0.70-0.78 range against
the "Wave A" sub-task about to be wired under it). That is not a
duplicate, it is the `addSubIssues` target. `check_duplicate` must
downgrade such a hard match from `block` to `warn` when the matched
issue is the intended `parent` or a caller-declared `related_issues`
sibling, while leaving genuine candidate duplicates blocked.

These tests monkeypatch `similarity.find_similar` so no embedding model
or cache is needed; they exercise only the recommendation / match_kind
logic in `check_duplicate`.
"""

from __future__ import annotations

import similarity
from similarity import Match, check_duplicate


def _match(number: int, similarity_score: float) -> Match:
    return Match(
        number=number,
        repo="acme/widgets",
        title=f"issue {number}",
        body_preview=f"body {number}",
        state="open",
        similarity=similarity_score,
    )


def _patch_matches(monkeypatch, matches):
    monkeypatch.setattr(
        similarity, "find_similar",
        lambda query, repo, **kwargs: list(matches),
    )


def test_hard_match_against_parent_downgrades_to_warn(monkeypatch):
    """A hard match whose number IS the intended parent is structural:
    block → warn, tagged structural_relative, downgraded_structural=True."""
    _patch_matches(monkeypatch, [_match(42, 0.76)])
    out = check_duplicate("Wave A", "does the text rule", "acme/widgets",
                          parent=42)
    assert out["recommendation"] == "warn"
    assert out["downgraded_structural"] is True
    assert out["any_above_hard"] is True
    assert out["matches"][0]["match_kind"] == "structural_relative"


def test_hard_match_against_non_parent_still_blocks(monkeypatch):
    """A hard match against an unrelated issue is a genuine candidate
    duplicate and must still block."""
    _patch_matches(monkeypatch, [_match(99, 0.88)])
    out = check_duplicate("Auth redesign", "body", "acme/widgets", parent=42)
    assert out["recommendation"] == "block"
    assert out["downgraded_structural"] is False
    assert out["matches"][0]["match_kind"] == "candidate"


def test_hard_match_against_related_sibling_downgrades(monkeypatch):
    """A caller-declared sibling (related_issues) is also structural."""
    _patch_matches(monkeypatch, [_match(101, 0.72)])
    out = check_duplicate("Wave B", "does the image rule", "acme/widgets",
                          parent=42, related_issues=[100, 101, 102])
    assert out["recommendation"] == "warn"
    assert out["downgraded_structural"] is True
    assert out["matches"][0]["match_kind"] == "structural_relative"


def test_mixed_structural_and_genuine_hard_still_blocks(monkeypatch):
    """If ANY hard match is a genuine (non-structural) candidate, the
    recommendation stays block even when a structural hard match is also
    present. A real duplicate is not masked by a structural sibling."""
    _patch_matches(monkeypatch, [_match(42, 0.80), _match(99, 0.91)])
    out = check_duplicate("Wave A", "body", "acme/widgets", parent=42)
    assert out["recommendation"] == "block"
    assert out["downgraded_structural"] is False
    kinds = {m["number"]: m["match_kind"] for m in out["matches"]}
    assert kinds[42] == "structural_relative"
    assert kinds[99] == "candidate"


def test_no_parent_preserves_legacy_block(monkeypatch):
    """Without parent / related_issues, behavior is unchanged: a hard
    match blocks and every match is a plain candidate."""
    _patch_matches(monkeypatch, [_match(7, 0.95)])
    out = check_duplicate("Title", "body", "acme/widgets")
    assert out["recommendation"] == "block"
    assert out["downgraded_structural"] is False
    assert out["matches"][0]["match_kind"] == "candidate"


def test_soft_only_match_warns_regardless_of_structure(monkeypatch):
    """A soft-but-not-hard match warns as before; structural tagging does
    not change a non-hard recommendation."""
    _patch_matches(monkeypatch, [_match(42, 0.60)])
    out = check_duplicate("Wave A", "body", "acme/widgets", parent=42)
    assert out["recommendation"] == "warn"
    assert out["any_above_hard"] is False
    assert out["downgraded_structural"] is False
    assert out["matches"][0]["match_kind"] == "structural_relative"
