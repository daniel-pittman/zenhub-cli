"""Tests for similarity.find_similar's threshold + min_results behavior
(v1.7.1). Exercises the scoring / filtering / backfill logic by
monkeypatching `_embed` and `_load_cache` so no real embedding model
or cache pickle is needed.

The fixtures use simple 2D normalized vectors where dot product (==
cosine similarity, since embeddings are normalized) is easy to reason
about against a query of [1, 0].
"""

from __future__ import annotations

import numpy as np
import pytest

import similarity
from similarity import IssueEntry, Match, find_similar


def _entry(number: int, vec, *, title: str = "", state: str = "open") -> IssueEntry:
    arr = np.array(vec, dtype=float)
    arr = arr / np.linalg.norm(arr)  # normalize so dot == cosine
    return IssueEntry(
        number=number,
        repo="acme/widgets",
        title=title or f"issue {number}",
        body_preview=f"body for {number}",
        state=state,
        updated_at="2026-05-28T00:00:00Z",
        embedding=arr,
    )


@pytest.fixture
def fake_repo(monkeypatch):
    # Query vector [1, 0]. Entry similarities to it (after normalize):
    #   A: [1, 0]            → 1.00
    #   B: [0.8, 0.6]        → 0.80
    #   C: [0.45, 0.893]     → 0.45  (just under the old 0.5, above 0.35)
    #   D: [0.3, 0.954]      → 0.30  (below 0.35)
    #   E: [0.1, 0.995]      → 0.10  (well below)
    entries = {
        "A": _entry(1, [1.0, 0.0], title="exact"),
        "B": _entry(2, [0.8, 0.6], title="strong"),
        "C": _entry(3, [0.45, np.sqrt(1 - 0.45**2)], title="moderate"),
        "D": _entry(4, [0.30, np.sqrt(1 - 0.30**2)], title="weak"),
        "E": _entry(5, [0.10, np.sqrt(1 - 0.10**2)], title="faint"),
    }
    monkeypatch.setattr(
        similarity, "_load_cache",
        lambda repo: {"version": 1, "indexed_at": "x", "entries": entries},
    )
    monkeypatch.setattr(similarity, "_auto_sync", lambda repo: {"ok": True})
    monkeypatch.setattr(similarity, "_embed", lambda text: np.array([1.0, 0.0]))
    return entries


def test_find_similar_default_threshold_is_0_35(fake_repo):
    # Default threshold 0.35 → A(1.0), B(0.8), C(0.45) clear it; D(0.30)
    # and E(0.10) don't. With min_results=0 (default), only the three
    # above-threshold come back.
    results = find_similar("q", "acme/widgets")
    nums = [m.number for m in results]
    assert nums == [1, 2, 3]
    assert all(m.meets_threshold for m in results)


def test_find_similar_min_results_backfills_below_threshold(fake_repo):
    # Only A, B, C clear 0.35 (3 matches), but min_results=5 forces
    # backfill with the next-closest below-threshold entries D, then E.
    results = find_similar("q", "acme/widgets", top_k=5, min_results=5)
    nums = [m.number for m in results]
    assert nums == [1, 2, 3, 4, 5]  # sorted by similarity desc
    flags = {m.number: m.meets_threshold for m in results}
    assert flags == {1: True, 2: True, 3: True, 4: False, 5: False}


def test_find_similar_backfill_capped_at_top_k(fake_repo):
    # min_results larger than top_k must still cap at top_k.
    results = find_similar("q", "acme/widgets", top_k=2, min_results=5)
    assert [m.number for m in results] == [1, 2]


def test_find_similar_high_threshold_with_backfill_returns_closest(fake_repo):
    # The exact zh_similar scenario that started this: nothing clears a
    # high threshold, but min_results surfaces the closest anyway —
    # never a bare empty list.
    results = find_similar(
        "q", "acme/widgets", top_k=3, threshold=0.95, min_results=3,
    )
    nums = [m.number for m in results]
    assert nums == [1, 2, 3]  # closest three
    # Only the 1.0 match clears 0.95; the rest are backfill.
    assert results[0].meets_threshold is True
    assert results[1].meets_threshold is False
    assert results[2].meets_threshold is False


def test_find_similar_min_results_zero_preserves_strict_behavior(fake_repo):
    # min_results=0 (the create_issue pre-flight default) → strict
    # threshold filtering, no backfill. At threshold 0.95 only A clears.
    results = find_similar("q", "acme/widgets", threshold=0.95)
    assert [m.number for m in results] == [1]


def test_find_similar_empty_cache_returns_empty(monkeypatch):
    monkeypatch.setattr(
        similarity, "_load_cache",
        lambda repo: {"version": 1, "indexed_at": None, "entries": {}},
    )
    monkeypatch.setattr(similarity, "_auto_sync", lambda repo: {"ok": True})
    assert find_similar("q", "acme/widgets", min_results=5) == []


def test_match_to_dict_includes_meets_threshold():
    m = Match(
        number=7, repo="acme/widgets", title="t", body_preview="b",
        state="open", similarity=0.4242, meets_threshold=False,
    )
    d = m.to_dict()
    assert d["meets_threshold"] is False
    assert d["similarity"] == 0.4242
    assert d["number"] == 7
