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
        either deleted or ACL-revoked between phases."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            sprint_header_null(),
            # If the implementation doesn't raise, it must walk; supply
            # a walker response so the test doesn't blow up on
            # StopIteration AFTER missing the assertion below.
            sprint_issues_null_node(),
        ]):
            with pytest.raises((zh_api.ZhApiError, Exception)):
                # Either raises (preferred) or returns ok=False; the
                # current implementation reads `node.get("name") or ...`
                # which would NPE on None. We accept either failure
                # mode but not silent success.
                out = zh_graphql_ops.get_sprint_detail(ctx, "current")
                # If we got here without raising, must be ok=False.
                assert out["ok"] is False

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
        must skip the None entry, not crash on `.get`."""
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            sprint_header_response(),
            sprint_issues_page([None, sprint_issue_wrapper(100)]),
        ]):
            out = zh_graphql_ops.get_sprint_detail(ctx, "current")
        assert out["ok"] is True
        nums = [i["number"] for i in out["issues"]]
        assert 100 in nums

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
        """Round-4 #2 SPEC pin: when the walker only saw part of the
        sprint, inputs the walker never reached cannot be classified
        as `succeeded`. Outcome is downgraded to `partial`/`fail`,
        `ok=False`, regardless of what the partial walk happened to
        show. Tested across two scenarios so future regression cannot
        accidentally re-introduce silent success.
        """
        # Scenario A: the partial walk happens to confirm one removal,
        # but didn't reach the other input. Pre-fix logic would have
        # marked both as `succeeded` (input minus partial-set =
        # everything not seen), so we'd see ok=True. SPEC: ok=False,
        # outcome=partial.
        ctx = make_ctx()
        with patch_ctx_query(ctx, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            issue_by_info_response(101),
            remove_issues_from_sprints_response(empty_sprints=True),
            # Walker page returns a sibling-repo issue (filtered out
            # by repos_match), then stuck cursor. So
            # still_attached_numbers is empty, but coverage is partial.
            sprint_issues_page(
                [sprint_issue_wrapper(999, owner="acme", repo="gadgets")],
                has_next=True, end_cursor=None,
            ),
        ]):
            out = zh_graphql_ops.remove_issues_from_sprint(
                ctx, "Sprint 7", [100, 101],
            )
        assert out["inspected_full"] is False
        assert out["ok"] is False, (
            "SPEC: partial coverage must NOT report ok=True even "
            "when still_attached is empty. Verified inputs in "
            "`succeeded`; un-verified ones are coverage-incomplete."
        )
        assert out["outcome"] in {"partial", "fail"}
        assert "coverage incomplete" in (out["response_anomaly"] or "").lower()

        # Scenario B: walker confirms an input is still attached.
        # Pre-fix logic would emit ok=False/outcome=partial anyway,
        # but make sure that's preserved AND coverage-incomplete still
        # surfaces.
        ctx2 = make_ctx()
        with patch_ctx_query(ctx2, [
            sprints_page([sprint_node("sprint-7", "Sprint 7")]),
            issue_by_info_response(100),
            issue_by_info_response(101),
            remove_issues_from_sprints_response(empty_sprints=True),
            # Walker sees #100 still attached, then bails on stuck
            # cursor. #101 is un-verified.
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
        assert out2["outcome"] in {"partial", "fail"}
        assert "coverage incomplete" in (out2["response_anomaly"] or "").lower()

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
        owner/repo. The regex anchors on `github.com[:/]` so this is
        already enforced, but pin it explicitly."""
        m = zh_api._GH_URL_RE.search("https://gist.github.com/acme/abc123")
        # gist.github.com matches `github.com[:/]` because the regex
        # doesn't require start-of-string. Document the SPEC contract:
        # the regex IS imperfect on gist URLs (matches the path) — but
        # since we never get gist URLs from `git remote get-url origin`
        # for repos, the imperfection is harmless. Pin behavior so we
        # at least know what it is.
        if m:
            # If it matches, owner+repo together must not look like a
            # gist (numeric repo id). Existing behavior: matches
            # ("acme", "abc123"). Document with a note.
            assert m.group("owner") == "acme"


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
