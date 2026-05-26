"""Test that similarity.py's GitHub URL regex accepts dots in repo names.

Round-3 finding #12: the regex at `similarity.py:200` still had the
old `[^/.]+?` group from before the round-1/round-2 fixes were
applied to `zh_api.py` and the bash side. Repo names like
`docs.github.io` would silently fail to parse — the same class of
bug fixed twice already on adjacent code.

The constant is exported as `_GITHUB_URL_RE` so we can exercise the
regex directly without needing a real git checkout.
"""

from __future__ import annotations

from similarity import _GITHUB_URL_RE


def test_similarity_regex_basic_forms():
    cases = [
        ("git@github.com:acme/widgets.git", "acme", "widgets"),
        ("git@github.com:acme/widgets", "acme", "widgets"),
        ("https://github.com/acme/widgets.git", "acme", "widgets"),
        ("https://github.com/acme/widgets", "acme", "widgets"),
    ]
    for url, owner, repo in cases:
        m = _GITHUB_URL_RE.search(url)
        assert m, f"failed to match {url!r}"
        assert m.group(1) == owner
        assert m.group(2) == repo


def test_similarity_regex_repo_with_dots():
    """Round-3 #12: `docs.github.io`-style names must parse."""
    cases = [
        ("git@github.com:acme/docs.github.io.git", "acme", "docs.github.io"),
        ("https://github.com/acme/docs.github.io", "acme", "docs.github.io"),
        ("git@github.com:acme/internal.docs.git", "acme", "internal.docs"),
        ("git@github.com:acme/my.tool", "acme", "my.tool"),
    ]
    for url, owner, repo in cases:
        m = _GITHUB_URL_RE.search(url)
        assert m, f"failed to match {url!r}"
        assert m.group(1) == owner
        assert m.group(2) == repo
