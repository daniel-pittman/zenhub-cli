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


def test_similarity_regex_rejects_garbage_prefix():
    """Round-5 #6: `^` anchor rejects garbage prefixes. Mirrors the
    parity test in test_zh_api so the two regexes accept the same
    set of inputs."""
    garbage = [
        "prefix-junk-git@github.com:owner/repo",
        "noise https://github.com/owner/repo",
        "\nhttps://github.com/owner/repo",
        " git@github.com:owner/repo",
    ]
    for url in garbage:
        m = _GITHUB_URL_RE.search(url)
        assert m is None, (
            f"garbage-prefixed URL {url!r} should NOT match; got {m!r}"
        )


def test_repo_from_cwd_uses_git_remote_get_url(monkeypatch):
    """Round-5 #7: similarity must call `git remote get-url origin`
    (which honors `url.<x>.insteadOf` rewrites), NOT
    `git config --get remote.origin.url` (which returns the raw
    config value). Otherwise the similarity cache and the MCP
    context resolution can diverge when a user has insteadOf
    rewriting configured.

    Pinned by inspecting the subprocess argv: both call sites
    (this one and zh_api.get_owner_repo_from_git) must invoke
    `git remote get-url origin`.
    """
    import similarity

    captured_argv = []

    class FakeCompleted:
        def __init__(self, args):
            captured_argv.append(args)

        # subprocess.check_output returns a string when text=True;
        # we patch the call to return a known URL.

    def fake_check_output(args, **kwargs):
        captured_argv.append(args)
        return "git@github.com:acme/widgets.git\n"

    monkeypatch.setattr(
        similarity.subprocess, "check_output", fake_check_output
    )
    result = similarity.repo_from_cwd("/tmp/fake-checkout")
    assert result == "acme/widgets"
    assert len(captured_argv) == 1
    args = captured_argv[0]
    # The relevant subargs (skip `git -C <cwd>` prefix)
    assert "remote" in args and "get-url" in args, (
        f"similarity must use `git remote get-url origin`, got {args!r}"
    )
    assert "origin" in args
    # Negative assertion: must NOT use `git config --get`
    assert "config" not in args, (
        f"similarity must NOT use `git config --get`; got {args!r}"
    )


def test_zh_api_and_similarity_use_same_git_command(monkeypatch):
    """Round-5 #7 (parity): the two `owner/repo` derivers must
    invoke equivalent git subcommands so they agree on
    insteadOf-rewritten URLs.
    """
    import similarity
    import zh_api

    captured = []

    def fake_check_output(args, **kwargs):
        captured.append(list(args))
        return "git@github.com:acme/widgets.git\n"

    # Both modules import `subprocess` by name, so they share the
    # same module object. One patch covers both call sites.
    monkeypatch.setattr(
        similarity.subprocess, "check_output", fake_check_output
    )

    similarity.repo_from_cwd("/tmp/x")
    zh_api.get_owner_repo_from_git(cwd="/tmp/x")

    assert len(captured) == 2, f"expected 2 subprocess calls, got {captured!r}"
    sim_argv, zhapi_argv = captured

    # Both invocations must end with the same git subcommand:
    # `remote get-url origin`. Argv-prefix may differ (similarity
    # uses `git -C <cwd>`; zh_api uses subprocess `cwd=<cwd>`),
    # but the tail subcommand is the load-bearing comparison.
    sim_tail = [a for a in sim_argv if a in {"remote", "get-url", "origin"}]
    zhapi_tail = [a for a in zhapi_argv if a in {"remote", "get-url", "origin"}]
    assert sim_tail == zhapi_tail == ["remote", "get-url", "origin"], (
        f"command divergence: similarity={sim_argv!r}, "
        f"zh_api={zhapi_argv!r}"
    )
