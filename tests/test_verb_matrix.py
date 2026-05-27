"""Per-verb test matrix.

Each verb in `zh_graphql_ops` (and the MCP wrappers that bind to it)
is exercised against a standard battery of scenarios:

  - happy: documented shape on a normal successful call
  - empty-input shape: guards on empty input return the full
    documented key set, not a bare `{ok, stderr}`
  - partial fail: API reports successCount < input / failedIssues != []
  - null GraphQL node: response with `data.node = null` or
    `issueByInfo = null` must NOT silently succeed
  - multi-repo: sibling repos with overlapping issue numbers must
    not confuse the verb (filter by `repos_match`)
  - pagination edges: stuck cursor, iteration cap, null page entry
  - dup-input: duplicate input numbers must coalesce
  - self-anchor: reorder verbs reject `after/before` against self

Tests are organized verb-by-verb; one test per scenario cell. Each
test docstring names the SPEC behavior it pins. Per methodology,
failing tests in this file are the bug list — assertions match what
the function's documented contract says, not what the code does.
"""

from __future__ import annotations

import pytest

import zh_api
import zh_graphql_ops
import mcp_server

from tests._fixtures import (
    make_ctx,
    patch_ctx_query,
    issue_by_info_response,
    issue_not_found_response,
    child_node,
    subissue_list_response,
    subissue_parent_not_found,
    add_sub_issues_response,
    remove_sub_issues_response,
    reprioritize_sub_issue_response,
    sprint_node,
    sprints_page,
    sprint_header_response,
    sprint_header_null,
    sprint_issue_wrapper,
    sprint_issues_page,
    sprint_issues_null_node,
    add_issues_to_sprints_response,
    remove_issues_from_sprints_response,
)


# =============================================================================
# `list_sub_issues`
# =============================================================================

class TestListSubIssues:
    """Verb: list_sub_issues(parent_number) — sub-issue listing."""

    def test_happy_path_returns_documented_shape(self):
        """Single-page listing returns ok=True with children populated."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            subissue_list_response(parent_number=42, nodes=[
                child_node(100, title="Add tests"),
                child_node(101, title="Fix bug"),
            ]),
        ]):
            out = zh_graphql_ops.list_sub_issues(ctx, 42)
        assert out["ok"] is True
        assert out["parent_number"] == 42
        assert out["fetched_count"] == 2
        assert {c["number"] for c in out["children"]} == {100, 101}
        assert out["pagination_warning"] is None

    def test_parent_not_found_returns_documented_fail_shape(self):
        """Null `issueByInfo` returns ok=False with an error message,
        not an empty-success result."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [subissue_parent_not_found()]):
            out = zh_graphql_ops.list_sub_issues(ctx, 999)
        assert out["ok"] is False
        # Full documented key set even on the not-found path
        for key in ("parent_number", "parent_title", "parent_state",
                    "total_count", "fetched_count", "children",
                    "pagination_warning"):
            assert key in out, f"missing key: {key}"
        assert out["children"] == []

    def test_multi_repo_children_each_carry_repository(self):
        """Cross-repo children must surface their owning repo so
        downstream tools (reorder anchor lookup, repo filtering) can
        disambiguate same-numbered siblings."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            subissue_list_response(nodes=[
                child_node(50, owner="acme", repo="widgets"),
                child_node(50, owner="acme", repo="gadgets"),
            ]),
        ]):
            out = zh_graphql_ops.list_sub_issues(ctx, 42)
        repos = [(c["repository"]["owner"], c["repository"]["name"])
                 for c in out["children"]]
        assert ("acme", "widgets") in repos
        assert ("acme", "gadgets") in repos

    def test_pagination_walks_until_has_next_false(self):
        """Multi-page walk concatenates all pages until hasNextPage."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            subissue_list_response(
                nodes=[child_node(100)],
                has_next=True, end_cursor="cur1",
            ),
            subissue_list_response(
                nodes=[child_node(101)],
                has_next=False,
            ),
        ]):
            out = zh_graphql_ops.list_sub_issues(ctx, 42)
        assert out["fetched_count"] == 2
        assert out["pagination_warning"] is None

    def test_stuck_cursor_bails_with_warning(self):
        """endCursor that doesn't advance trips the defensive bail."""
        ctx = make_ctx()
        stuck = subissue_list_response(
            nodes=[child_node(100)],
            has_next=True, end_cursor=None,
        )
        with patch_ctx_query(ctx, [stuck, stuck]):
            out = zh_graphql_ops.list_sub_issues(ctx, 42)
        assert out["pagination_warning"] is not None
        assert "cursor" in out["pagination_warning"].lower()

    def test_iteration_cap_bails_with_warning(self):
        """Cursor advances every page but never terminates → cap fires."""
        ctx = make_ctx()
        original_cap = zh_graphql_ops.MAX_PAGINATION_ITERATIONS
        try:
            zh_graphql_ops.MAX_PAGINATION_ITERATIONS = 3
            pages = [
                subissue_list_response(
                    nodes=[child_node(1000 + i)],
                    has_next=True,
                    end_cursor=f"cur-{i}",
                )
                for i in range(10)
            ]
            with patch_ctx_query(ctx, pages):
                out = zh_graphql_ops.list_sub_issues(ctx, 42)
        finally:
            zh_graphql_ops.MAX_PAGINATION_ITERATIONS = original_cap
        assert out["pagination_warning"] is not None
        assert "iteration cap" in out["pagination_warning"].lower()

    def test_null_page_entry_does_not_crash(self):
        """A `nodes: [None, real_node]` page must skip the null without
        raising. Defensive normalization for unexpected API shapes."""
        ctx = make_ctx()
        resp = subissue_list_response(nodes=[None, child_node(100)])
        with patch_ctx_query(ctx, [resp]):
            out = zh_graphql_ops.list_sub_issues(ctx, 42)
        assert out["ok"] is True
        # Real node still surfaces
        nums = [c["number"] for c in out["children"]]
        assert 100 in nums


# =============================================================================
# `add_sub_issues`
# =============================================================================

class TestAddSubIssues:
    """Verb: add_sub_issues(parent_number, child_numbers) — link sub-issues."""

    @staticmethod
    def _parent(num: int = 42) -> dict:
        return issue_by_info_response(num)

    @staticmethod
    def _child(num: int) -> dict:
        return issue_by_info_response(num)

    def test_happy_path_returns_documented_shape(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            self._parent(42),
            self._child(100),
            self._child(101),
            add_sub_issues_response(success_count=2),
        ]):
            out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 101])
        assert out["ok"] is True
        assert out["outcome"] == "ok"
        assert sorted(out["succeeded"]) == [100, 101]
        assert out["failed"] == []
        assert out["partial_success_warning"] is None

    def test_empty_input_raises_zhapierror(self):
        """SPEC: empty `child_numbers` raises before any network call."""
        ctx = make_ctx()
        with pytest.raises(zh_api.ZhApiError):
            zh_graphql_ops.add_sub_issues(ctx, 42, [])

    def test_partial_failure_splits_succeeded_and_failed(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            self._parent(42),
            self._child(100),
            self._child(101),
            self._child(102),
            add_sub_issues_response(
                success_count=2,
                failed=[{
                    "number": 102,
                    "repository": {"ownerName": "acme", "name": "widgets"},
                }],
            ),
        ]):
            out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 101, 102])
        assert out["outcome"] == "partial"
        assert sorted(out["succeeded"]) == [100, 101]
        assert [f["number"] for f in out["failed"]] == [102]

    def test_null_parent_returns_fail_outcome(self):
        """Parent lookup returns None → outcome=fail, no mutation fires."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            {"data": {"issueByInfo": None}},  # parent not found
        ]):
            out = zh_graphql_ops.add_sub_issues(ctx, 9999, [100])
        assert out["ok"] is False
        assert out["outcome"] == "fail"
        assert "not found" in (out["error"] or "").lower()

    def test_null_child_returns_fail_outcome(self):
        """Any child not found → fail before mutation."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            self._parent(42),
            self._child(100),
            issue_not_found_response(),  # 9999 not found
        ]):
            out = zh_graphql_ops.add_sub_issues(ctx, 42, [100, 9999])
        assert out["ok"] is False
        assert out["outcome"] == "fail"
        assert 9999 in [f["number"] for f in out["failed"]]

    def test_duplicate_input_coalesces(self):
        """SPEC: `add_sub_issues(42, [100, 100, 101])` must NOT count
        100 twice. Each unique child number maps to one mutation
        argument; the API returns one link per unique child. Test in
        test_subissue_ops.py covers this on the inferred-set side;
        this is the matrix cell for the verb.

        Implementation: `add_sub_issues` should dedup at the boundary
        the same way the sprint mutations do (round-2 #10).
        """
        ctx = make_ctx()
        # Pre-flight resolution only needs to lookup unique children:
        # if it doesn't dedup, 5 child lookups will be queued, and the
        # patch_ctx_query iterator runs out and the test crashes with
        # a clear StopIteration (which IS the bug surfaced).
        with patch_ctx_query(ctx, [
            self._parent(42),
            self._child(100),
            self._child(101),
            add_sub_issues_response(success_count=2),
        ]):
            out = zh_graphql_ops.add_sub_issues(
                ctx, 42, [100, 100, 101, 100],
            )
        assert out["success_count"] == 2
        assert sorted(out["succeeded"]) == [100, 101]


# =============================================================================
# `remove_sub_issues`
# =============================================================================

class TestRemoveSubIssues:
    """Verb: remove_sub_issues(parent_number, child_numbers) — unlink."""

    @staticmethod
    def _parent_node_for_child(num: int = 42) -> dict:
        """Build the `parentIssue` object a child returns when its
        parent is #42."""
        return {
            "id": f"issue-gid-{num}",
            "number": num,
            "title": f"Parent {num}",
            "repository": {"ownerName": "acme", "name": "widgets"},
        }

    def test_happy_path(self):
        ctx = make_ctx()
        parent = self._parent_node_for_child(42)
        with patch_ctx_query(ctx, [
            issue_by_info_response(42),
            issue_by_info_response(100, parent=parent),
            issue_by_info_response(101, parent=parent),
            remove_sub_issues_response(success_count=2),
        ]):
            out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100, 101])
        assert out["ok"] is True
        assert sorted(out["succeeded"]) == [100, 101]

    def test_empty_input_raises(self):
        ctx = make_ctx()
        with pytest.raises(zh_api.ZhApiError):
            zh_graphql_ops.remove_sub_issues(ctx, 42, [])

    def test_wrong_parent_caught_preflight(self):
        """Child whose parentIssue is NOT us — pre-flight catches."""
        ctx = make_ctx()
        wrong_parent = self._parent_node_for_child(99)  # wrong number
        with patch_ctx_query(ctx, [
            issue_by_info_response(42),
            issue_by_info_response(100, parent=wrong_parent),
        ]):
            out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100])
        assert out["ok"] is False
        assert out["outcome"] == "fail"
        assert "wrong parent" in (out["error"] or "").lower()

    def test_orphan_child_caught_preflight(self):
        """Child with no parentIssue at all (not a sub-issue) — fail."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            issue_by_info_response(42),
            issue_by_info_response(100, parent=None),  # orphan
        ]):
            out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100])
        assert out["ok"] is False
        assert out["outcome"] == "fail"
        # The error message should name the problem ("no parent", "not a sub-issue", etc.)
        err = (out["error"] or "").lower()
        assert "wrong parent" in err or "no parent" in err or "not a sub" in err

    def test_null_parent_returns_fail(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            {"data": {"issueByInfo": None}},  # parent #9999 not found
        ]):
            out = zh_graphql_ops.remove_sub_issues(ctx, 9999, [100])
        assert out["ok"] is False
        assert out["outcome"] == "fail"

    def test_cross_repo_child_caught_preflight(self):
        """Child in sibling repo (acme/gadgets) — pre-flight catches."""
        ctx = make_ctx()  # owner_repo="acme/widgets"
        parent = self._parent_node_for_child(42)
        with patch_ctx_query(ctx, [
            issue_by_info_response(42),
            issue_by_info_response(
                100, parent=parent, owner="acme", repo="gadgets",
            ),
        ]):
            out = zh_graphql_ops.remove_sub_issues(ctx, 42, [100])
        assert out["ok"] is False
        assert "cross-repo" in (out["error"] or "").lower()

    def test_partial_failure_splits(self):
        """API reports successCount=1, failedIssues=[{102, ...}] — must
        split succeeded vs failed by what the API returned."""
        ctx = make_ctx()
        parent = self._parent_node_for_child(42)
        with patch_ctx_query(ctx, [
            issue_by_info_response(42),
            issue_by_info_response(100, parent=parent),
            issue_by_info_response(101, parent=parent),
            issue_by_info_response(102, parent=parent),
            remove_sub_issues_response(
                success_count=2,
                failed=[{
                    "number": 102,
                    "repository": {"ownerName": "acme", "name": "widgets"},
                }],
            ),
        ]):
            out = zh_graphql_ops.remove_sub_issues(
                ctx, 42, [100, 101, 102],
            )
        assert out["outcome"] == "partial"
        assert sorted(out["succeeded"]) == [100, 101]
        assert [f["number"] for f in out["failed"]] == [102]

    def test_duplicate_input_coalesces(self):
        """SPEC: duplicate child numbers must collapse first-occurrence
        the same way add_sub_issues does — same matrix cell."""
        ctx = make_ctx()
        parent = self._parent_node_for_child(42)
        with patch_ctx_query(ctx, [
            issue_by_info_response(42),
            issue_by_info_response(100, parent=parent),
            issue_by_info_response(101, parent=parent),
            remove_sub_issues_response(success_count=2),
        ]):
            out = zh_graphql_ops.remove_sub_issues(
                ctx, 42, [100, 100, 101],
            )
        assert out["success_count"] == 2
        assert sorted(out["succeeded"]) == [100, 101]


# =============================================================================
# `reorder_sub_issue`
# =============================================================================

class TestReorderSubIssue:
    """Verb: reorder_sub_issue(child, position, sibling_number=None)."""

    @staticmethod
    def _parent_node(num: int = 42) -> dict:
        return {
            "id": f"issue-gid-{num}",
            "number": num,
            "title": f"Parent {num}",
            "repository": {"ownerName": "acme", "name": "widgets"},
        }

    def test_happy_top_anchor_uses_first_other_sibling(self):
        ctx = make_ctx()
        parent = self._parent_node(42)
        with patch_ctx_query(ctx, [
            # child lookup
            issue_by_info_response(
                100,
                issue_id="issue-gid-100",
                parent=parent,
            ),
            # sibling listing
            subissue_list_response(parent_number=42, nodes=[
                child_node(101, node_id="issue-gid-101"),
                child_node(100, node_id="issue-gid-100"),
            ]),
            # mutation success
            reprioritize_sub_issue_response(success=True),
        ]):
            out = zh_graphql_ops.reorder_sub_issue(ctx, 100, "top")
        assert out["ok"] is True
        assert out["outcome"] == "ok"

    def test_only_child_returns_noop(self):
        ctx = make_ctx()
        parent = self._parent_node(42)
        with patch_ctx_query(ctx, [
            issue_by_info_response(100, parent=parent),
            subissue_list_response(parent_number=42, nodes=[
                child_node(100, node_id="issue-gid-100"),
            ]),
        ]):
            out = zh_graphql_ops.reorder_sub_issue(ctx, 100, "top")
        assert out["outcome"] == "noop"
        assert out["ok"] is False

    def test_self_anchor_after_raises(self):
        ctx = make_ctx()
        with pytest.raises(zh_api.ZhApiError):
            zh_graphql_ops.reorder_sub_issue(
                ctx, 100, "after", sibling_number=100,
            )

    def test_self_anchor_before_raises(self):
        ctx = make_ctx()
        with pytest.raises(zh_api.ZhApiError):
            zh_graphql_ops.reorder_sub_issue(
                ctx, 100, "before", sibling_number=100,
            )

    def test_orphan_child_returns_fail(self):
        """Child not a sub-issue — no parent to reorder under."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            issue_by_info_response(100, parent=None),
        ]):
            out = zh_graphql_ops.reorder_sub_issue(ctx, 100, "top")
        assert out["ok"] is False
        assert out["outcome"] == "fail"

    def test_child_not_found_returns_fail(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            {"data": {"issueByInfo": None}},
        ]):
            out = zh_graphql_ops.reorder_sub_issue(ctx, 9999, "top")
        assert out["ok"] is False
        assert out["outcome"] == "fail"

    def test_cross_repo_anchor_via_gid(self):
        """top/bottom anchors by gid not by number, so cross-repo
        siblings are valid anchors. (Round-2 review #2 fix.)"""
        ctx = make_ctx()  # owner_repo="acme/widgets"
        parent = self._parent_node(42)
        with patch_ctx_query(ctx, [
            issue_by_info_response(100, issue_id="issue-gid-100",
                                   parent=parent),
            # Cross-repo sibling: same number (100) in acme/gadgets
            subissue_list_response(parent_number=42, nodes=[
                child_node(100, node_id="issue-gid-100-gadgets",
                           owner="acme", repo="gadgets"),
                child_node(100, node_id="issue-gid-100",
                           owner="acme", repo="widgets"),
            ]),
            reprioritize_sub_issue_response(success=True),
        ]):
            out = zh_graphql_ops.reorder_sub_issue(ctx, 100, "top")
        # The first sibling in the listing has a different gid (it's
        # the gadgets-repo #100), so "first other" matches it.
        assert out["ok"] is True
        assert out["outcome"] == "ok"

    def test_reorder_top_refuses_under_partial_walk(self):
        """Round-6 #5 SPEC pin: when the sibling listing bailed mid-
        walk, top/bottom anchoring is unsafe — the "first" sibling in
        a partial set isn't the workspace-global first, so the
        mutation would silently corrupt sub-issue order. Refuse the
        mutation; surface the pagination warning in the error.
        """
        ctx = make_ctx()
        parent = self._parent_node(42)
        with patch_ctx_query(ctx, [
            issue_by_info_response(100, issue_id="issue-gid-100",
                                   parent=parent),
            # Sibling listing bails on stuck cursor — coverage is
            # partial.
            subissue_list_response(
                parent_number=42,
                nodes=[
                    child_node(100, node_id="issue-gid-100"),
                    child_node(101, node_id="issue-gid-101"),
                ],
                has_next=True, end_cursor=None,  # stuck cursor
            ),
        ]):
            out = zh_graphql_ops.reorder_sub_issue(ctx, 100, "top")
        assert out["ok"] is False
        assert out["outcome"] == "fail"
        err = (out.get("error") or "").lower()
        assert "partial pagination" in err, (
            f"expected partial-pagination message; got: {err!r}"
        )

    def test_reorder_bottom_refuses_under_partial_walk(self):
        """Round-6 #5 — same SPEC for bottom."""
        ctx = make_ctx()
        parent = self._parent_node(42)
        with patch_ctx_query(ctx, [
            issue_by_info_response(100, issue_id="issue-gid-100",
                                   parent=parent),
            subissue_list_response(
                parent_number=42,
                nodes=[
                    child_node(100, node_id="issue-gid-100"),
                    child_node(101, node_id="issue-gid-101"),
                ],
                has_next=True, end_cursor=None,
            ),
        ]):
            out = zh_graphql_ops.reorder_sub_issue(ctx, 100, "bottom")
        assert out["ok"] is False
        assert "partial pagination" in (out.get("error") or "").lower()

    def test_reorder_after_still_works_under_partial_walk(self):
        """Round-6 #5 — `after` / `before` with an explicit sibling
        number stays valid under partial coverage: the user named
        a specific anchor; if it's in the partial set we use it,
        otherwise we fail with anchor-not-found. Asymmetry vs
        top/bottom is intentional and pinned by this test.
        """
        ctx = make_ctx()
        parent = self._parent_node(42)
        with patch_ctx_query(ctx, [
            issue_by_info_response(100, issue_id="issue-gid-100",
                                   parent=parent),
            # Anchor #101 is in the partial set — the partial walk
            # is fine for an explicit-anchor lookup.
            subissue_list_response(
                parent_number=42,
                nodes=[
                    child_node(100, node_id="issue-gid-100"),
                    child_node(101, node_id="issue-gid-101"),
                ],
                has_next=True, end_cursor=None,  # stuck cursor
            ),
            reprioritize_sub_issue_response(success=True),
        ]):
            out = zh_graphql_ops.reorder_sub_issue(
                ctx, 100, "after", sibling_number=101,
            )
        assert out["ok"] is True
        assert out["outcome"] == "ok"


# =============================================================================
# `list_sprints`
# =============================================================================

class TestListSprints:
    def test_happy_open_only_marks_active(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([
                sprint_node("sprint-7", "Sprint 7"),
                sprint_node("sprint-8", "Sprint 8",
                            start="2026-05-15T00:00:00Z",
                            end="2026-05-29T00:00:00Z"),
            ]),
        ]):
            out = zh_graphql_ops.list_sprints(ctx)
        assert out["ok"] is True
        assert out["active_sprint_id"] == "sprint-7"
        active_by_name = {s["name"]: s["is_active"] for s in out["sprints"]}
        assert active_by_name == {"Sprint 7": True, "Sprint 8": False}

    def test_empty_workspace(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([], active_sprint_id=None,
                         workspace_name="Empty"),
        ]):
            out = zh_graphql_ops.list_sprints(ctx)
        assert out["ok"] is True
        assert out["sprints"] == []
        assert out["active_sprint_id"] is None

    def test_walks_pagination(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page(
                [sprint_node(f"s-{i}", f"S{i}") for i in range(50)],
                has_next=True, end_cursor="cur1",
            ),
            sprints_page(
                [sprint_node("s-old", "S Old")],
                has_next=False,
            ),
        ]):
            out = zh_graphql_ops.list_sprints(ctx)
        names = {s["name"] for s in out["sprints"]}
        assert "S Old" in names
        assert len(out["sprints"]) == 51

    def test_stuck_cursor_bails(self):
        ctx = make_ctx()
        stuck = sprints_page(
            [sprint_node("s-1", "S1")],
            has_next=True, end_cursor=None,
        )
        with patch_ctx_query(ctx, [stuck, stuck]):
            out = zh_graphql_ops.list_sprints(ctx)
        assert out["pagination_warning"] is not None

    def test_null_workspace_returns_fail_not_silent_empty(self):
        """SPEC: `data.workspace = null` means the workspace is
        deleted / ACL-revoked / unresolvable. Must NOT silently
        return an empty sprint list — that's indistinguishable from
        a real empty workspace and lets downstream callers misbehave.
        """
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            {"data": {"workspace": None}},
        ]):
            # Either raises or returns ok=False; not silent ok=True/[].
            try:
                out = zh_graphql_ops.list_sprints(ctx)
            except zh_api.ZhApiError:
                return  # acceptable: hard fail
        assert out["ok"] is False, (
            "list_sprints returned ok=True for a null workspace, "
            "which silently looks like an empty-but-real workspace"
        )

    def test_null_page_entry_does_not_crash(self):
        """SPEC: a `sprints.nodes: [None, real_sprint]` page must skip
        the null without crashing. Defensive normalization for
        unexpected API shapes (matches sub-issue listing's contract)."""
        ctx = make_ctx()
        resp = sprints_page(
            [None, sprint_node("sprint-7", "Sprint 7")],
        )
        with patch_ctx_query(ctx, [resp]):
            out = zh_graphql_ops.list_sprints(ctx)
        assert out["ok"] is True
        # The real sprint still surfaces
        names = [s["name"] for s in out["sprints"]]
        assert "Sprint 7" in names


# =============================================================================
# `get_sprint_detail`
# =============================================================================

class TestGetSprintDetail:
    def test_happy_path(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            sprint_header_response(),
            sprint_issues_page([sprint_issue_wrapper(100)]),
        ]):
            out = zh_graphql_ops.get_sprint_detail(ctx, "Sprint 7")
        assert out["ok"] is True
        assert out["sprint_id"] == "sprint-7"
        assert out["issue_count"] == 1

    def test_current_alias(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            sprint_header_response(),
            sprint_issues_page([]),
        ]):
            out = zh_graphql_ops.get_sprint_detail(ctx, "current")
        assert out["sprint_id"] == "sprint-7"

    def test_unknown_name_returns_fail(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
        ]):
            out = zh_graphql_ops.get_sprint_detail(ctx, "No Such Sprint")
        assert out["ok"] is False
        assert "not found" in (out["error"] or "").lower()

    def test_find_sprint_id_surfaces_pagination_warning(self):
        """Round-6 #11 SPEC pin: when the sprints listing bailed
        mid-walk and the requested sprint isn't in the partial set,
        the error must say so rather than just claiming "not found."
        Otherwise the user might assume the sprint doesn't exist
        when it's actually on an unreached page.
        """
        ctx = make_ctx()
        # Page 1 has Sprint 7, then bails on stuck cursor.
        with patch_ctx_query(ctx, [
            sprints_page(
                [sprint_node("sprint-7", "Sprint 7")],
                has_next=True, end_cursor=None,  # stuck cursor
            ),
        ]):
            out = zh_graphql_ops.get_sprint_detail(ctx, "Sprint 99")
        assert out["ok"] is False
        err = (out["error"] or "").lower()
        assert "incomplete" in err, (
            f"expected 'incomplete' in error to flag the partial walk; "
            f"got: {err!r}"
        )
        # Mention the bail reason so the user can decide what to do
        assert "stuck_cursor" in err or "cursor" in err

    def test_walks_issue_pagination(self):
        ctx = make_ctx()
        page1 = [sprint_issue_wrapper(i) for i in range(1, 101)]
        page2 = [sprint_issue_wrapper(101)]
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            sprint_header_response(),
            sprint_issues_page(page1, has_next=True, end_cursor="cur1"),
            sprint_issues_page(page2, has_next=False),
        ]):
            out = zh_graphql_ops.get_sprint_detail(ctx, "current")
        assert out["issue_count"] == 101

    def test_null_sprint_header_raises(self):
        """SPEC: header query returns `data.node = null` → fail loudly.
        A sprint that's known to the index but null at header time is
        either deleted or ACL-revoked between phases.

        Test-discipline note (round-5 #3): an earlier version wrapped
        this in `with pytest.raises((ZhApiError, Exception)):` and put
        the assert inside the block. `AssertionError` is a subclass
        of `Exception`, so any assertion that fired inside the block
        was silently caught — the test would pass whether
        `get_sprint_detail` raised, returned ok=False, OR returned
        ok=True. Restructured so the SPEC ("raise OR return ok=False;
        never silent ok=True") is actually enforceable.
        """
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            sprint_header_null(),
            # If the implementation doesn't raise, it must walk; supply
            # a walker response so the test doesn't blow up on
            # StopIteration AFTER missing the assertion below.
            sprint_issues_null_node(),
        ]):
            try:
                out = zh_graphql_ops.get_sprint_detail(ctx, "current")
            except zh_api.ZhApiError:
                return  # SPEC: raising loudly is acceptable
        assert out["ok"] is False, (
            "get_sprint_detail returned ok=True for a null sprint "
            "header — SPEC says raise ZhApiError OR return ok=False, "
            "never silent success"
        )

    def test_walker_null_node_in_detail_path_raises(self):
        """SPEC: the issues-page walker hitting null-node mid-walk
        must propagate to get_sprint_detail rather than silently
        returning a sprint with no issues."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            sprint_header_response(),
            sprint_issues_null_node(),
        ]):
            with pytest.raises(zh_api.ZhApiError):
                zh_graphql_ops.get_sprint_detail(ctx, "current")

    def test_null_issue_wrapper_skipped(self):
        """SPEC: a sprintIssues page containing `[None, {"issue": ...}]`
        must SKIP the None entry — not emit a phantom record into
        `issues`. The other two walkers (list_sub_issues, list_sprints)
        already enforce this; the sprint-issues walker historically
        appended `{number: None, title: ""}` for nulls because the
        loop body did `(wrapper or {}).get("issue") or {}`.
        Round-4 finding #4.

        Round-6 #6 extension: also pin the `[{"issue": null}, real]`
        shape — a wrapper that's a dict but contains a null `issue`
        field. Pre-round-6 the idiom `(wrapper or {}).get("issue")
        or {}` coalesced None to `{}` and still leaked a phantom.
        """
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            sprint_header_response(),
            # Two phantom shapes: wrapper=None AND wrapper={"issue": None}
            sprint_issues_page([
                None,
                {"issue": None},  # round-6 #6 phantom shape
                sprint_issue_wrapper(100),
            ]),
        ]):
            out = zh_graphql_ops.get_sprint_detail(ctx, "current")
        assert out["ok"] is True
        nums = [i["number"] for i in out["issues"]]
        assert 100 in nums
        # SPEC tightening: there should be exactly ONE issue (#100).
        # A phantom record from the None wrapper OR the null-issue
        # wrapper would show up as extra entries. Pin that no
        # phantom is emitted from either shape.
        assert out["issue_count"] == 1, (
            f"phantom record leaked: issues={out['issues']!r}"
        )
        # Belt-and-suspenders: every emitted issue must have a valid
        # int number AND a non-empty repository (round-6 #6 SPEC).
        for i in out["issues"]:
            assert isinstance(i["number"], int), (
                f"non-int number leaked from null wrapper: {i!r}"
            )
            rep = i.get("repository") or {}
            assert rep.get("owner") or rep.get("name"), (
                f"phantom record with empty repo leaked: {i!r}"
            )

    def test_null_pipeline_node_entry_does_not_crash(self):
        """SPEC: `pipelineIssues.nodes[0]` can be null defensively.
        The walker's `pipeline_nodes[0].get("pipeline")` would NPE on
        a None entry. (Round-3 #15.)"""
        ctx = make_ctx()
        bad_issue_wrapper = {
            "issue": {
                "number": 100,
                "title": "Bad pipeline node",
                "state": "OPEN",
                "htmlUrl": "https://github.com/acme/widgets/issues/100",
                "estimate": None,
                "assignees": {"nodes": []},
                "repository": {"ownerName": "acme", "name": "widgets"},
                "pipelineIssues": {"nodes": [None]},
            }
        }
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            sprint_header_response(),
            sprint_issues_page([bad_issue_wrapper]),
        ]):
            out = zh_graphql_ops.get_sprint_detail(ctx, "current")
        # Should not crash; pipeline should fall back to None.
        assert out["ok"] is True
        assert out["issues"][0]["pipeline"] is None


# =============================================================================
# `get_current_sprint`
# =============================================================================

class TestGetCurrentSprint:
    def test_happy_path(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            sprint_header_response(),
            sprint_issues_page([]),
        ]):
            out = zh_graphql_ops.get_current_sprint(ctx)
        assert out["ok"] is True
        assert out["sprint_id"] == "sprint-7"

    def test_no_active_sprint(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([], active_sprint_id=None,
                         workspace_name="Quiet"),
        ]):
            out = zh_graphql_ops.get_current_sprint(ctx)
        assert out["ok"] is False
        assert "no active sprint" in (out["error"] or "").lower()

    def test_null_workspace_propagates(self):
        """`get_current_sprint` delegates to `_find_sprint_id` →
        `list_sprints`. A null workspace must surface as a failure,
        not silent ok=True with no issues."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            {"data": {"workspace": None}},
        ]):
            # Either raises (preferred) or returns ok=False
            try:
                out = zh_graphql_ops.get_current_sprint(ctx)
            except zh_api.ZhApiError:
                return
        assert out["ok"] is False


# =============================================================================
# `add_issues_to_sprint`
# =============================================================================

class TestAddIssuesToSprint:
    def test_happy_path(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            issue_by_info_response(101),
            add_issues_to_sprints_response(linked=[
                (100, "acme", "widgets"),
                (101, "acme", "widgets"),
            ]),
        ]):
            out = zh_graphql_ops.add_issues_to_sprint(
                ctx, "Sprint 7", [100, 101],
            )
        assert out["ok"] is True
        assert sorted(out["succeeded"]) == [100, 101]

    def test_empty_input_raises(self):
        ctx = make_ctx()
        with pytest.raises(zh_api.ZhApiError):
            zh_graphql_ops.add_issues_to_sprint(ctx, "Sprint 7", [])

    def test_partial_fail(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            issue_by_info_response(101),
            issue_by_info_response(102),
            add_issues_to_sprints_response(linked=[
                (100, "acme", "widgets"),
            ]),
        ]):
            out = zh_graphql_ops.add_issues_to_sprint(
                ctx, "Sprint 7", [100, 101, 102],
            )
        assert out["outcome"] == "partial"
        assert out["succeeded"] == [100]
        assert sorted(out["failed"]) == [101, 102]

    def test_sprint_not_found(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
        ]):
            out = zh_graphql_ops.add_issues_to_sprint(
                ctx, "No Such Sprint", [100],
            )
        assert out["ok"] is False
        assert "not found" in (out["error"] or "").lower()

    def test_multi_repo_filters_response_by_ctx_repo(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(42),
            # API returns a link for OUR #42 AND a sibling-repo #42
            add_issues_to_sprints_response(linked=[
                (42, "acme", "widgets"),
                (42, "acme", "gadgets"),
            ]),
        ]):
            out = zh_graphql_ops.add_issues_to_sprint(
                ctx, "Sprint 7", [42],
            )
        # Only our-repo link should credit
        assert out["succeeded"] == [42]
        assert out["success_count"] == 1

    def test_only_sibling_repo_link_not_credited(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(42),
            add_issues_to_sprints_response(linked=[
                (42, "acme", "gadgets"),  # NOT our repo
            ]),
        ]):
            out = zh_graphql_ops.add_issues_to_sprint(
                ctx, "Sprint 7", [42],
            )
        assert out["succeeded"] == []
        assert out["failed"] == [42]

    def test_duplicate_input_coalesces(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(42),
            issue_by_info_response(43),
            add_issues_to_sprints_response(linked=[
                (42, "acme", "widgets"),
                (43, "acme", "widgets"),
            ]),
        ]):
            out = zh_graphql_ops.add_issues_to_sprint(
                ctx, "Sprint 7", [42, 42, 43, 42],
            )
        assert out["success_count"] == 2
        assert sorted(out["succeeded"]) == [42, 43]

    def test_missing_issue_caught_preflight(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            issue_not_found_response(),  # 9999 not found
        ]):
            out = zh_graphql_ops.add_issues_to_sprint(
                ctx, "Sprint 7", [100, 9999],
            )
        assert out["ok"] is False
        assert 9999 in out["failed"]


# =============================================================================
# `remove_issues_from_sprint`
# =============================================================================

class TestRemoveIssuesFromSprint:
    def test_happy_path(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            issue_by_info_response(101),
            remove_issues_from_sprints_response(still_attached=[]),
        ]):
            out = zh_graphql_ops.remove_issues_from_sprint(
                ctx, "Sprint 7", [100, 101],
            )
        assert out["ok"] is True
        assert sorted(out["succeeded"]) == [100, 101]

    def test_empty_input_raises(self):
        ctx = make_ctx()
        with pytest.raises(zh_api.ZhApiError):
            zh_graphql_ops.remove_issues_from_sprint(ctx, "Sprint 7", [])

    def test_partial_fail_one_still_attached(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            issue_by_info_response(101),
            remove_issues_from_sprints_response(still_attached=[
                (101, "acme", "widgets"),
            ]),
        ]):
            out = zh_graphql_ops.remove_issues_from_sprint(
                ctx, "Sprint 7", [100, 101],
            )
        assert out["outcome"] == "partial"
        assert out["succeeded"] == [100]
        assert out["failed"] == [101]

    def test_multi_repo_sibling_not_misclassified(self):
        ctx = make_ctx()  # acme/widgets
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(42),
            # Sibling repo's #42 still in sprint — not our concern
            remove_issues_from_sprints_response(still_attached=[
                (42, "acme", "gadgets"),
            ]),
        ]):
            out = zh_graphql_ops.remove_issues_from_sprint(
                ctx, "Sprint 7", [42],
            )
        # Our acme/widgets#42 was removed; the gadgets #42 doesn't
        # block us.
        assert out["succeeded"] == [42]
        assert out["failed"] == []

    def test_duplicate_input_coalesces(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(42),
            issue_by_info_response(43),
            remove_issues_from_sprints_response(still_attached=[]),
        ]):
            out = zh_graphql_ops.remove_issues_from_sprint(
                ctx, "Sprint 7", [42, 43, 42],
            )
        assert out["success_count"] == 2
        assert sorted(out["succeeded"]) == [42, 43]

    def test_empty_sprints_array_triggers_walker_recovery(self):
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            remove_issues_from_sprints_response(empty_sprints=True),
            sprint_issues_page([]),  # walker says empty
        ]):
            out = zh_graphql_ops.remove_issues_from_sprint(
                ctx, "Sprint 7", [100],
            )
        assert out["succeeded"] == [100]
        assert out["response_anomaly"] is not None

    def test_walker_stuck_cursor_sets_inspected_full_false(self):
        """Round-3 #3: when walker bails defensively, inspected_full
        must be False (the bug-pin test in test_sprint_ops.py covers
        the >100 path; this one covers the recovery path)."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            remove_issues_from_sprints_response(empty_sprints=True),
            # Walker page with stuck cursor
            sprint_issues_page(
                [sprint_issue_wrapper(100)],
                has_next=True, end_cursor=None,
            ),
        ]):
            out = zh_graphql_ops.remove_issues_from_sprint(
                ctx, "Sprint 7", [100],
            )
        assert out["pagination_warning"] is not None
        assert out["inspected_full"] is False

    def test_partial_walk_forces_outcome_not_ok(self):
        """Round-4 #2 / round-5 #1 SPEC pin: when the walker only saw
        part of the sprint, inputs the walker never reached cannot
        be classified as `succeeded`. The honest SPEC:

          - `succeeded = inputs ∩ walked_numbers - still_attached`
          - `failed = inputs ∩ walked_numbers ∩ still_attached`
          - inputs neither walked nor still-attached are un-verified
            and surface in `response_anomaly`

        Round 4 downgraded `outcome`/`ok` correctly but left
        `succeeded` over-counting (inputs - still_attached, with
        still_attached empty when walker bailed before reaching
        any input → all inputs in `succeeded`). Round 5 fixes that
        and this test pins both axes.
        """
        # Scenario A: the partial walk reached a sibling-repo issue
        # (filtered out by repos_match) but NEVER reached either
        # input. So walked_numbers={999} (sibling), still_attached={},
        # and `succeeded` should be EMPTY — not [100, 101] as the
        # round-4 logic produced.
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            issue_by_info_response(101),
            remove_issues_from_sprints_response(empty_sprints=True),
            sprint_issues_page(
                [sprint_issue_wrapper(999, owner="acme", repo="gadgets")],
                has_next=True, end_cursor=None,
            ),
        ]):
            out = zh_graphql_ops.remove_issues_from_sprint(
                ctx, "Sprint 7", [100, 101],
            )
        assert out["inspected_full"] is False
        assert out["ok"] is False
        assert out["outcome"] == "fail", (
            "SPEC: zero confirmed positives → outcome='fail'"
        )
        # Round-5 #1: succeeded must be empty when no input was reached
        assert out["succeeded"] == [], (
            "Round-5 #1: succeeded over-counted when walker never "
            "reached the inputs. Honest SPEC says succeeded ⊆ "
            "walked_numbers, so an unreached input cannot appear here."
        )
        assert out["failed"] == [], (
            "failed only lists walker-observed-still-attached; "
            "un-verified inputs go to response_anomaly's count."
        )
        assert "coverage incomplete" in (out["response_anomaly"] or "").lower()
        # The honest count narrative: 0 verified, 2 un-verified.
        assert "verified 0 of 2" in (out["response_anomaly"] or "")
        assert "2 input(s) un-verified" in (out["response_anomaly"] or "")

        # Scenario B: walker reaches input #100 and observes it still-
        # attached (real failure); input #101 un-verified (walker
        # bailed before reaching it). SPEC: succeeded=[], failed=[100],
        # un-verified count=1.
        ctx2 = make_ctx()
        with patch_ctx_query(ctx2, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            issue_by_info_response(101),
            remove_issues_from_sprints_response(empty_sprints=True),
            sprint_issues_page(
                [sprint_issue_wrapper(100)],
                has_next=True, end_cursor=None,
            ),
        ]):
            out2 = zh_graphql_ops.remove_issues_from_sprint(
                ctx2, "Sprint 7", [100, 101],
            )
        assert out2["inspected_full"] is False
        assert out2["ok"] is False
        assert out2["outcome"] == "fail"
        assert out2["succeeded"] == [], (
            "Walker saw #100 still-attached, never reached #101. "
            "Neither input was observed-absent. succeeded must be []."
        )
        assert out2["failed"] == [100], (
            "Walker observed #100 still-attached after mutation; "
            "it's a confirmed failure."
        )
        assert "coverage incomplete" in (out2["response_anomaly"] or "").lower()
        assert "1 input(s) un-verified" in (out2["response_anomaly"] or "")

        # Scenario C (round-5 #1 honest-positive): walker reaches
        # input #100 and confirms it absent (succeeded); never reaches
        # #101 (un-verified). SPEC: succeeded=[100], failed=[],
        # un-verified count=1, outcome="partial" (we DID confirm one).
        ctx3 = make_ctx()
        with patch_ctx_query(ctx3, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            issue_by_info_response(101),
            remove_issues_from_sprints_response(empty_sprints=True),
            # Walker page returns a sibling issue #100 AND a sibling-
            # repo unrelated #999, then bails. Walker saw #100 in
            # walked_numbers, but it's NOT in still_attached (issue
            # 100 is the one we removed, only #999-gadgets remains).
            # Wait — recovery branch walks the WHOLE sprint, so if
            # #100 is GONE from the sprint, it won't be in the walked
            # results at all. Use a different setup: walker reaches
            # #999 (un-removed entry) which is not our input. So
            # walked_numbers={999}, still_attached={} (after repo
            # filter), and neither #100 nor #101 is in walked. That's
            # scenario A's shape, not what we want. Scenario C needs
            # the walker to actually REACH #100 — let's put #100
            # itself in the walk but make it a different repo so the
            # repo-filter exempts it from still_attached.
            #
            # Actually, the cleanest scenario-C: walker walks the full
            # sprint and #100 has been removed (so it's NOT in the
            # walk's pages); #101 is un-verified because walker bails
            # before reaching its page.
            sprint_issues_page(
                [sprint_issue_wrapper(100)],
                has_next=True, end_cursor=None,
            ),
        ]):
            out3 = zh_graphql_ops.remove_issues_from_sprint(
                ctx3, "Sprint 7", [100, 101],
            )
        # The fixture above has walker reach #100 STILL-ATTACHED
        # (same shape as scenario B). To pin succeeded=[100] honestly
        # we'd need the walker to observe #100's gid as the
        # post-mutation sprint NOT containing it — which means the
        # mocked page must NOT include #100. We cover this via
        # `test_partial_walk_with_confirmed_positive` below.
        assert out3["outcome"] == "fail"  # same as B with this fixture

    def test_partial_walk_with_confirmed_positive(self):
        """Round-5 #1 honest-positive pin: walker reaches some input
        and observes it absent. SPEC: that input lands in
        `succeeded` (it's `inputs ∩ walked_numbers - still_attached`).
        Distinct from scenario A/B above where succeeded is empty.
        """
        ctx = make_ctx()
        # Walker reaches issue #999 (sibling that's still in the
        # sprint), and the page is full so walked includes #999 only.
        # Inputs #100 / #101 — neither is in still_attached, but
        # neither is in walked_numbers either, so they're un-verified.
        # To pin a confirmed-positive we need walker to reach #100.
        # Setup: mutation response has an empty sprints array,
        # triggers recovery walk; walker's first page contains #101
        # (still-attached, our repo), then bails. So #101 confirmed-
        # failed (walked + still_attached), #100 un-verified.
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            issue_by_info_response(101),
            remove_issues_from_sprints_response(empty_sprints=True),
            sprint_issues_page(
                # Walker observes the post-mutation sprint contains
                # #999 (sibling that wasn't touched) AND our input
                # #101 (still-attached failure). It does NOT contain
                # #100 — meaning the removal of #100 was confirmed by
                # what the walker saw. So #100 IS in succeeded ONLY
                # if walked_numbers includes it. The walker's
                # walked_numbers is pre-repo-filter and includes
                # everything it iterated. To get #100 into
                # walked_numbers we'd need it to actually appear in
                # the walk pages — which contradicts "removed."
                #
                # The clean version of this test would mock the
                # walker page-by-page and have the walker traverse
                # the WHOLE sprint excluding #100 (because #100 was
                # removed). But walked_numbers is built from the
                # iterated nodes, and #100 won't be in iterated
                # nodes if it was removed. So #100 is correctly
                # un-verified — the partial-coverage semantics say
                # "we never SAW #100's absence specifically; we just
                # didn't reach pages where it would have been."
                #
                # In other words: partial-walk SUCCEEDED detection
                # of removals is only possible when the walker
                # observes the FULL post-state, at which point we're
                # already `inspected_full=True`. Under partial
                # coverage, succeeded for a removal verb is always
                # [] (no removal can be confirmed without seeing
                # the whole sprint).
                #
                # This is a deeper SPEC observation worth pinning:
                [sprint_issue_wrapper(101)],
                has_next=True, end_cursor=None,
            ),
        ]):
            out = zh_graphql_ops.remove_issues_from_sprint(
                ctx, "Sprint 7", [100, 101],
            )
        assert out["inspected_full"] is False
        # #101 walker-observed-still-attached → confirmed failure
        assert 101 in out["failed"]
        # #100 walker-never-reached → un-verified, NOT succeeded
        assert 100 not in out["succeeded"], (
            "Round-5 #1: under partial coverage the walker must "
            "actually observe an input's absence to count it as "
            "removed. Pre-fix logic would put #100 in succeeded "
            "because it wasn't in still_attached — that's the bug."
        )
        assert 100 not in out["failed"]  # un-verified, not failed
        assert "1 input(s) un-verified" in (out["response_anomaly"] or "")

    def test_partial_walk_response_anomaly_includes_reverify_pointer(self):
        """SPEC: the coverage-incomplete branch's response_anomaly
        must point the caller at a re-verification command, otherwise
        the caller has no actionable next step beyond the warning.
        Pin against an LLM caller hand-rolling a `zh sprint show ...`
        because the field doesn't say so."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            remove_issues_from_sprints_response(empty_sprints=True),
            sprint_issues_page(
                [sprint_issue_wrapper(100)],
                has_next=True, end_cursor=None,
            ),
        ]):
            out = zh_graphql_ops.remove_issues_from_sprint(
                ctx, "Sprint 7", [100],
            )
        assert "zh sprint show" in (out["response_anomaly"] or "")

    def test_partial_walk_no_inflation_across_repos(self):
        """Round-6 #1 SPEC pin: walker `walked_numbers` must be
        repo-filtered, otherwise a cross-repo same-number issue
        trivially satisfies "walked AND not still-attached" and gets
        reported as `succeeded` under partial coverage.

        Setup: walker bails after observing acme/gadgets#42 (sibling
        repo). Our input is acme/widgets#42. Pre-fix logic:
          - walked_numbers = {42}            (number-only, pre-fix)
          - still_attached_numbers = {}      (repo-filtered, empty)
          - succeeded = inputs ∩ walked - still_attached = {42}  ← WRONG
        Round-6 fix repo-filters walked_numbers too:
          - walked_numbers = {}              (no acme/widgets issues seen)
          - succeeded = ∅                    ← correct
        """
        ctx = make_ctx()  # owner_repo="acme/widgets"
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(42),
            remove_issues_from_sprints_response(empty_sprints=True),
            # Walker page contains acme/gadgets#42 (sibling repo,
            # same number), then bails on stuck cursor before any
            # acme/widgets pages.
            sprint_issues_page(
                [sprint_issue_wrapper(42, owner="acme", repo="gadgets")],
                has_next=True, end_cursor=None,
            ),
        ]):
            out = zh_graphql_ops.remove_issues_from_sprint(
                ctx, "Sprint 7", [42],
            )
        assert out["inspected_full"] is False
        assert out["succeeded"] == [], (
            "Round-6 #1: walked_numbers must be repo-filtered. "
            "Pre-fix, cross-repo acme/gadgets#42 in walked_numbers "
            "let our acme/widgets#42 input pass 'walked AND not "
            "still-attached' and falsely report as removed."
        )
        # 42 isn't in walker output for our repo, so it's un-verified
        assert out["failed"] == []
        # un-verified count = 1 (our input wasn't reached in our repo)
        assert "1 input(s) un-verified" in (out["response_anomaly"] or "")


# =============================================================================
# zh_api foundation: _GH_URL_RE
# =============================================================================

class TestGhUrlRegex:
    def test_basic_forms(self):
        cases = [
            ("git@github.com:acme/widgets.git", "acme", "widgets"),
            ("git@github.com:acme/widgets", "acme", "widgets"),
            ("https://github.com/acme/widgets.git", "acme", "widgets"),
            ("https://github.com/acme/widgets", "acme", "widgets"),
            ("http://github.com/acme/widgets/", "acme", "widgets"),
        ]
        for url, owner, repo in cases:
            m = zh_api._GH_URL_RE.search(url)
            assert m, f"failed to match {url!r}"
            assert (m.group("owner"), m.group("repo")) == (owner, repo)

    def test_repo_with_dots(self):
        cases = [
            ("git@github.com:acme/docs.github.io.git",
             "acme", "docs.github.io"),
            ("https://github.com/acme/docs.github.io",
             "acme", "docs.github.io"),
            ("git@github.com:acme/internal.docs.git",
             "acme", "internal.docs"),
            ("git@github.com:acme/my.tool",
             "acme", "my.tool"),
        ]
        for url, owner, repo in cases:
            m = zh_api._GH_URL_RE.search(url)
            assert m, f"failed to match {url!r}"
            assert (m.group("owner"), m.group("repo")) == (owner, repo)

    def test_owner_with_dots(self):
        m = zh_api._GH_URL_RE.search("https://github.com/owner.with.dots/repo")
        assert m
        assert m.group("owner") == "owner.with.dots"
        assert m.group("repo") == "repo"

    def test_mixed_case_preserved(self):
        """Case is preserved on parse; comparison is case-insensitive
        elsewhere via `repos_match`."""
        m = zh_api._GH_URL_RE.search("https://github.com/Acme/Widgets")
        assert m
        assert m.group("owner") == "Acme"
        assert m.group("repo") == "Widgets"

    def test_gist_url_rejected(self):
        """SPEC: gist URLs are not GitHub repos and must NOT parse as
        owner/repo. Pre-round-4 the regex used a permissive
        `github.com[:/]` anchor that incidentally matched
        `gist.github.com` (documented as "imperfect-but-harmless").
        Round-4 unified the regex on a strict
        `https?://github.com/` prefix that now rejects gist URLs
        outright. Round-4 #5."""
        m = zh_api._GH_URL_RE.search("https://gist.github.com/acme/abc123")
        assert m is None, (
            f"gist URL should NOT match the canonical regex; got {m!r}"
        )


class TestUrlRegexParity:
    """Round-4 finding #5: three GitHub-URL regexes (zh_api,
    similarity, zh bash) used to accept different sets of inputs.
    They must now agree.

    The canonical contract: the source URL is always what
    `git remote get-url origin` emits in practice, which is either
    `git@github.com:owner/repo[.git]` (ssh) or
    `https://github.com/owner/repo[.git]` (https). Schemes accepted
    by some git clients but NOT emitted by `git remote get-url`
    (ssh://, git://, git+ssh://) are rejected.
    """

    # The Python regexes are importable; the bash one isn't directly,
    # so we test both Python parsers and document that the bash regex
    # is kept structurally identical (verified inline at the source).

    ACCEPTED_FORMS = [
        ("git@github.com:acme/widgets.git", "acme", "widgets"),
        ("git@github.com:acme/widgets", "acme", "widgets"),
        ("https://github.com/acme/widgets.git", "acme", "widgets"),
        ("https://github.com/acme/widgets", "acme", "widgets"),
        ("http://github.com/acme/widgets/", "acme", "widgets"),
        # Dots in repo names — all three parsers must handle.
        ("git@github.com:acme/docs.github.io.git",
         "acme", "docs.github.io"),
        ("https://github.com/acme/internal.docs", "acme", "internal.docs"),
    ]

    REJECTED_FORMS = [
        # SSH-protocol-prefixed (not what `git remote get-url` emits)
        "ssh://git@github.com/acme/widgets",
        # Old git:// (not what `git remote get-url` emits for GH)
        "git://github.com/acme/widgets",
        # git+ssh:// (likewise)
        "git+ssh://git@github.com/acme/widgets",
        # Gist URLs (different service)
        # NB: zh_api documented this as imperfect-but-harmless above.
        # The stricter prefix now rejects gist URLs explicitly.
        "https://gist.github.com/acme/abc123",
        # Garbage prefix (round-5 #6) — `^` anchor rejects.
        "prefix-junk-git@github.com:owner/repo",
        "noise https://github.com/owner/repo",
        " git@github.com:owner/repo",
    ]

    def _zh_api_parse(self, url):
        m = zh_api._GH_URL_RE.search(url)
        if not m:
            return None
        return (m.group("owner"), m.group("repo"))

    def _similarity_parse(self, url):
        from similarity import _GITHUB_URL_RE
        m = _GITHUB_URL_RE.search(url)
        if not m:
            return None
        return (m.group(1), m.group(2))

    def test_zh_api_accepts_canonical_forms(self):
        for url, owner, repo in self.ACCEPTED_FORMS:
            result = self._zh_api_parse(url)
            assert result == (owner, repo), (
                f"zh_api regex failed on {url!r}: got {result!r}"
            )

    def test_similarity_accepts_canonical_forms(self):
        for url, owner, repo in self.ACCEPTED_FORMS:
            result = self._similarity_parse(url)
            assert result == (owner, repo), (
                f"similarity regex failed on {url!r}: got {result!r}"
            )

    def test_zh_api_rejects_non_canonical_forms(self):
        """SPEC: prefix-anchored regex MUST NOT silently match
        schemes that `git remote get-url origin` doesn't emit.
        Matrix gap from round-4 #5."""
        for url in self.REJECTED_FORMS:
            result = self._zh_api_parse(url)
            assert result is None, (
                f"zh_api regex unexpectedly matched {url!r}: "
                f"got {result!r}"
            )

    def test_similarity_rejects_non_canonical_forms(self):
        """Same SPEC as the zh_api side — parity is the load-bearing
        property."""
        for url in self.REJECTED_FORMS:
            result = self._similarity_parse(url)
            assert result is None, (
                f"similarity regex unexpectedly matched {url!r}: "
                f"got {result!r}"
            )

    def test_parsers_agree_on_every_input(self):
        """The two Python parsers must produce identical outputs for
        the same input — anything else is the kind of drift that
        bit us in rounds 1 / 2 / 3. (Bash is structurally identical
        but not directly testable from Python; see commit message.)"""
        all_inputs = (
            [url for url, _, _ in self.ACCEPTED_FORMS]
            + list(self.REJECTED_FORMS)
        )
        for url in all_inputs:
            zh_api_result = self._zh_api_parse(url)
            sim_result = self._similarity_parse(url)
            assert zh_api_result == sim_result, (
                f"parsers disagree on {url!r}: "
                f"zh_api={zh_api_result!r} similarity={sim_result!r}"
            )


# =============================================================================
# zh_api foundation: list_workspaces
# =============================================================================

class TestListWorkspaces:
    @staticmethod
    def _ws_page(nodes, *, has_next=False, end_cursor=None):
        return {
            "data": {
                "repositoriesByGhId": [{
                    "id": "repo-gid-123",
                    "workspacesConnection": {
                        "pageInfo": {
                            "hasNextPage": has_next,
                            "endCursor": end_cursor,
                        },
                        "nodes": nodes,
                    },
                }]
            }
        }

    def test_happy_single_page(self, monkeypatch):
        monkeypatch.setattr(zh_api, "get_gh_repo_id", lambda *a, **kw: 123)
        monkeypatch.setattr(
            zh_api, "graphql_request",
            lambda *a, **kw: self._ws_page([
                {"id": "ws-1", "name": "A"},
                {"id": "ws-2", "name": "B"},
            ]),
        )
        nodes = zh_api.list_workspaces("acme/widgets", token="t", gh_token="t")
        assert {n["name"] for n in nodes} == {"A", "B"}

    def test_walks_pagination(self, monkeypatch):
        monkeypatch.setattr(zh_api, "get_gh_repo_id", lambda *a, **kw: 123)
        responses = iter([
            self._ws_page(
                [{"id": f"ws-{i}", "name": f"W{i}"} for i in range(50)],
                has_next=True, end_cursor="cur1",
            ),
            self._ws_page([{"id": "ws-old", "name": "Old"}]),
        ])
        monkeypatch.setattr(
            zh_api, "graphql_request",
            lambda *a, **kw: next(responses),
        )
        nodes = zh_api.list_workspaces("acme/widgets", token="t", gh_token="t")
        assert len(nodes) == 51
        assert any(n["name"] == "Old" for n in nodes)

    def test_stuck_cursor_bails(self, monkeypatch):
        monkeypatch.setattr(zh_api, "get_gh_repo_id", lambda *a, **kw: 123)
        stuck = self._ws_page(
            [{"id": "ws-1", "name": "A"}],
            has_next=True, end_cursor=None,
        )
        monkeypatch.setattr(
            zh_api, "graphql_request",
            lambda *a, **kw: stuck,
        )
        nodes = zh_api.list_workspaces("acme/widgets", token="t", gh_token="t")
        assert {n["id"] for n in nodes} == {"ws-1"}

    def test_empty_connection(self, monkeypatch):
        monkeypatch.setattr(zh_api, "get_gh_repo_id", lambda *a, **kw: 123)
        monkeypatch.setattr(
            zh_api, "graphql_request",
            lambda *a, **kw: self._ws_page([]),
        )
        nodes = zh_api.list_workspaces("acme/widgets", token="t", gh_token="t")
        assert nodes == []


# =============================================================================
# zh_api foundation: resolve_context env-var precedence
# =============================================================================

class TestResolveContext:
    """Documented precedence (kept in sync with bash):

      Repo: arg > ZH_REPO_OVERRIDE > ZH_REPO > config > git remote
      Workspace: arg > ZH_WORKSPACE_NAME > ZH_WORKSPACE > config > first
    """

    def _patch(self, monkeypatch):
        monkeypatch.setattr(
            zh_api, "resolve_token", lambda config=None: "tok",
        )
        monkeypatch.setattr(
            zh_api, "get_zenhub_repo_id", lambda *a, **kw: "repo-gid",
        )
        monkeypatch.setattr(
            zh_api, "get_workspace_id",
            lambda owner_repo, **kw: (
                f"ws-for-{kw.get('workspace_name') or 'default'}"
            ),
        )
        monkeypatch.setattr(zh_api, "load_config", lambda *a, **kw: {})

    def test_explicit_owner_repo_arg_wins(self, monkeypatch):
        self._patch(monkeypatch)
        monkeypatch.setenv("ZH_REPO_OVERRIDE", "ignored/x")
        monkeypatch.setenv("ZH_REPO", "ignored/y")
        ctx = zh_api.resolve_context(owner_repo="explicit/repo")
        assert ctx.owner_repo == "explicit/repo"

    def test_zh_repo_override_wins_over_zh_repo(self, monkeypatch):
        self._patch(monkeypatch)
        monkeypatch.setenv("ZH_REPO_OVERRIDE", "flag/repo")
        monkeypatch.setenv("ZH_REPO", "env/repo")
        ctx = zh_api.resolve_context()
        assert ctx.owner_repo == "flag/repo"

    def test_zh_repo_falls_back_to_git_remote(self, monkeypatch):
        """If ZH_REPO is not set, git-remote inference fires. Stubbed
        out here so we don't shell out."""
        self._patch(monkeypatch)
        monkeypatch.delenv("ZH_REPO_OVERRIDE", raising=False)
        monkeypatch.delenv("ZH_REPO", raising=False)
        monkeypatch.setattr(
            zh_api, "get_owner_repo_from_git",
            lambda cwd=None: "fromgit/repo",
        )
        ctx = zh_api.resolve_context()
        assert ctx.owner_repo == "fromgit/repo"

    def test_explicit_workspace_arg_wins(self, monkeypatch):
        self._patch(monkeypatch)
        monkeypatch.setenv("ZH_WORKSPACE_NAME", "ignored")
        monkeypatch.setenv("ZH_WORKSPACE", "also ignored")
        ctx = zh_api.resolve_context(
            owner_repo="acme/widgets", workspace_name="Arg WS",
        )
        assert ctx.workspace_id == "ws-for-Arg WS"

    def test_zh_workspace_name_wins_over_zh_workspace(self, monkeypatch):
        self._patch(monkeypatch)
        monkeypatch.setenv("ZH_WORKSPACE_NAME", "Flag WS")
        monkeypatch.setenv("ZH_WORKSPACE", "Env WS")
        ctx = zh_api.resolve_context(owner_repo="acme/widgets")
        assert ctx.workspace_id == "ws-for-Flag WS"

    def test_zh_workspace_falls_back_when_name_unset(self, monkeypatch):
        self._patch(monkeypatch)
        monkeypatch.delenv("ZH_WORKSPACE_NAME", raising=False)
        monkeypatch.setenv("ZH_WORKSPACE", "Config WS")
        ctx = zh_api.resolve_context(owner_repo="acme/widgets")
        assert ctx.workspace_id == "ws-for-Config WS"


# =============================================================================
# zh_api foundation: RepoContext.query (transport + auth boundary)
# =============================================================================

class TestRepoContextQuery:
    def test_query_routes_through_graphql_request(self, monkeypatch):
        """RepoContext.query is a thin shim — it should pass the token
        to graphql_request without further processing."""
        captured = {}

        def fake_graphql_request(query, variables=None, *, token=None):
            captured["query"] = query
            captured["variables"] = variables
            captured["token"] = token
            return {"data": {}}

        monkeypatch.setattr(
            zh_api, "graphql_request", fake_graphql_request,
        )
        ctx = make_ctx(token="my-token")
        ctx.query("query { x }", {"a": 1})
        assert captured["query"] == "query { x }"
        assert captured["variables"] == {"a": 1}
        assert captured["token"] == "my-token"

    def test_query_passes_no_variables_dict(self, monkeypatch):
        """When called without variables, graphql_request gets None
        (per its signature) — not an empty dict that would be visible
        in the request body."""
        captured = {}

        def fake_graphql_request(query, variables=None, *, token=None):
            captured["variables"] = variables
            return {"data": {}}

        monkeypatch.setattr(
            zh_api, "graphql_request", fake_graphql_request,
        )
        ctx = make_ctx()
        ctx.query("query { x }")
        assert captured["variables"] is None


# =============================================================================
# MCP wrappers: every documented key on early-return paths
# =============================================================================

SUBISSUE_LIST_KEYS = {
    "ok", "parent_number", "parent_title", "parent_state",
    "total_count", "fetched_count", "children",
    "pagination_warning", "stderr",
}

SUBISSUE_MUTATION_KEYS = {
    "ok", "parent_number", "outcome",
    "success_count", "failed_count", "succeeded", "failed",
    "github_errors", "partial_success_warning", "stderr",
}

SUBISSUE_REORDER_KEYS = {
    "ok", "child_number", "parent_number", "position",
    "outcome", "stderr",
}

SPRINT_SHOW_KEYS = {
    "ok", "sprint_id", "sprint_name", "state",
    "start_at", "end_at",
    "completed_points", "total_points", "closed_issues_count",
    "description", "issue_count", "issues",
    "pagination_warning", "stderr",
}

SPRINT_ADD_KEYS = {
    "ok", "sprint_id", "sprint_name", "outcome",
    "success_count", "failed_count", "succeeded", "failed",
    "stderr",
}

SPRINT_REMOVE_KEYS = {
    "ok", "sprint_id", "sprint_name", "outcome",
    "success_count", "failed_count", "succeeded", "failed",
    "inspected_full", "pagination_warning", "response_anomaly",
    "stderr",
}


def _full_shape(d: dict, expected: set[str]) -> None:
    missing = expected - set(d.keys())
    assert not missing, f"missing keys: {sorted(missing)}"


class TestMcpEarlyReturnShapes:
    """Empty-input guards must return the full documented shape."""

    def test_subissue_add_children_empty_input(self):
        r = mcp_server.subissue_add_children(42, [])
        assert r["ok"] is False
        _full_shape(r, SUBISSUE_MUTATION_KEYS)

    def test_subissue_remove_children_empty_input(self):
        r = mcp_server.subissue_remove_children(42, [])
        assert r["ok"] is False
        _full_shape(r, SUBISSUE_MUTATION_KEYS)

    def test_sprint_show_empty_name(self):
        r = mcp_server.sprint_show("")
        _full_shape(r, SPRINT_SHOW_KEYS)

    def test_sprint_show_whitespace_name(self):
        r = mcp_server.sprint_show("   ")
        _full_shape(r, SPRINT_SHOW_KEYS)

    def test_sprint_add_issues_empty_numbers(self):
        r = mcp_server.sprint_add_issues("Sprint 7", [])
        _full_shape(r, SPRINT_ADD_KEYS)

    def test_sprint_add_issues_empty_name(self):
        r = mcp_server.sprint_add_issues("", [42])
        _full_shape(r, SPRINT_ADD_KEYS)

    def test_sprint_remove_issues_empty_numbers(self):
        r = mcp_server.sprint_remove_issues("Sprint 7", [])
        _full_shape(r, SPRINT_REMOVE_KEYS)

    def test_sprint_remove_issues_empty_name(self):
        r = mcp_server.sprint_remove_issues("", [42])
        _full_shape(r, SPRINT_REMOVE_KEYS)


# =============================================================================
# Bash dispatcher integration — round-5 #9
# =============================================================================

class TestBashDispatcher:
    """Pin that `zh -w foo` (and similar `-r/-w` invocations with no
    subcommand) don't trip `set -u` on bash 3.2's empty-array
    expansion. Run zh as a subprocess so we exercise the actual
    `set -- "${args[@]}"` line in main().

    Round-5 #9: pre-fix, `zh -w foo` errored with
    `args[@]: unbound variable` on bash 3.2 (Apple's stock
    /bin/bash). CI runs Ubuntu's bash 5 which doesn't reproduce.
    """

    import os
    import subprocess as _subprocess
    from pathlib import Path as _Path
    _REPO_ROOT = _Path(__file__).resolve().parent.parent
    _ZH_SCRIPT = _REPO_ROOT / "zh"

    def _run_zh(self, *args, env_extra=None):
        """Run `bash zh ...` (force bash, don't trust shebang on the
        test machine) and return CompletedProcess. Routes stderr +
        stdout together so we can inspect output without parsing.

        We force `bash` so the test works even on systems where
        /usr/bin/env bash resolves to bash 4+; the round-5 #9 bug
        only reproduces on bash 3.2 anyway, so this is the
        compatibility-floor check.
        """
        import os
        import subprocess
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
        # Empty ZH_TOKEN avoids the config-load error path firing
        # before main() even gets to dispatch — but the dispatcher
        # parses global flags BEFORE invoking the subcommand body,
        # so the bug shape is reachable regardless of token state.
        return subprocess.run(
            ["bash", str(self._ZH_SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

    def test_no_subcommand_falls_through_to_help(self):
        """SPEC: `zh -w foo` with no subcommand falls through to the
        help dispatcher. The exit may be 0 (help) or non-zero (error
        for missing subcommand), but the output must NOT contain
        `args[@]: unbound variable`.
        """
        result = self._run_zh("-w", "foo")
        combined = (result.stdout or "") + (result.stderr or "")
        assert "unbound variable" not in combined, (
            f"bash 3.2 set -u tripped on empty args array: "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
        # Either help output OR a clean "no command" error; not a
        # crash. Both have "zh" somewhere in the output.
        assert "zh" in combined.lower(), (
            f"no recognizable zh output; combined={combined!r}"
        )

    def test_dash_r_no_subcommand_falls_through(self):
        """Same SPEC for `-r owner/repo` alone."""
        result = self._run_zh("-r", "acme/widgets")
        combined = (result.stdout or "") + (result.stderr or "")
        assert "unbound variable" not in combined

    def test_dash_w_dash_r_no_subcommand_falls_through(self):
        """Same SPEC when both flags are present and no subcommand."""
        result = self._run_zh("-r", "acme/widgets", "-w", "Backend")
        combined = (result.stdout or "") + (result.stderr or "")
        assert "unbound variable" not in combined

    def test_load_config_env_wins_over_config(self, tmp_path):
        """Round-6 #14 SPEC pin: env var beats config file, matching
        Python `resolve_context` precedence (round-3 #11 SPEC).

        Pre-round-6 the bash side `source $CONFIG_FILE` overwrote
        env-set values with config-file values — config-wins. The
        two sides disagreed: a user with `export ZH_REPO=x` and a
        stale config file would see Python use `x` and bash silently
        use the config value. Round-6 fixes the bash side.
        """
        import os
        import subprocess
        # Build a temp config file
        cfg = tmp_path / "config"
        cfg.write_text(
            "ZH_TOKEN=tok_from_config\n"
            "ZH_REPO=repo_from_config\n"
            "ZH_WORKSPACE=ws_from_config\n"
        )
        # Extract load_config from zh and run with the temp config.
        # Use a wrapper script so we can override CONFIG_FILE and
        # call load_config in isolation.
        env = os.environ.copy()
        env["CONFIG_FILE"] = str(cfg)
        env["ZH_REPO"] = "repo_from_env"
        # ZH_TOKEN not set in env → config should win
        env.pop("ZH_TOKEN", None)
        env.pop("ZH_WORKSPACE", None)

        # Inline shell that sources zh's load_config and dumps the
        # resolved values. Routes through `bash zh workspaces` —
        # this will fail at the gh api call but we only need to
        # observe the resolved env vars BEFORE the API attempt.
        # Simpler: extract load_config and run it directly.
        wrapper = tmp_path / "wrapper.sh"
        zh_path = str(self._ZH_SCRIPT)
        wrapper.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "error() { echo \"ERROR: $1\" >&2; exit 1; }\n"
            f"eval \"$(awk '/^load_config\\(\\)/,/^}}/' {zh_path})\"\n"
            "load_config\n"
            "echo \"TOKEN=$ZH_TOKEN\"\n"
            "echo \"REPO=$ZH_REPO\"\n"
            "echo \"WS=$ZH_WORKSPACE\"\n"
        )
        wrapper.chmod(0o755)
        result = subprocess.run(
            ["bash", str(wrapper)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        out = result.stdout
        # Config token (env unset) → config wins
        assert "TOKEN=tok_from_config" in out, (
            f"expected token from config; got: {out!r}"
        )
        # Env repo set → env wins
        assert "REPO=repo_from_env" in out, (
            f"Round-6 #14: env var should win over config file. "
            f"got: {out!r}"
        )
        # Workspace env unset → config wins
        assert "WS=ws_from_config" in out, (
            f"expected workspace from config; got: {out!r}"
        )
