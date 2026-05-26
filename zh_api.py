"""Direct ZenHub GraphQL client for the MCP server.

The MCP server used to shell out to `zh --machine` and parse TSV/RESULT
lines. Four rounds of release review on v1.5.0's sub-issue feature
surfaced ~25 findings, roughly half architectural: every time the bash
emitter and the Python parser drifted, the wrapper lied. v1.6.0 replaces
the text-contract layer with this module — direct HTTP calls to ZenHub's
GraphQL API, native Python data structures all the way out.

This module is deliberately dependency-light:

  - urllib.request for HTTP (no third-party HTTP client)
  - json from stdlib
  - re from stdlib
  - subprocess only for the `git remote get-url` fallback when no
    explicit owner/repo is given

This keeps the MCP venv bootstrap path narrow (no extra wheels to
download on first run) and means the same module is testable without
network access by mocking `graphql_request`.

Auth + config resolution mirrors what `zh` (bash) does so users with an
existing `~/.config/zh/config` get the same behavior from MCP. Repo and
workspace resolution use the same GraphQL queries the bash script uses,
ported to Python.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

# =============================================================================
# Configuration / auth
# =============================================================================

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "zh" / "config"
ZH_GRAPHQL_URL = "https://api.zenhub.com/public/graphql"

_CONFIG_KEY_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


class ZhApiError(RuntimeError):
    """Anything wrong with config, transport, or the GraphQL response."""


def _strip_quotes(s: str) -> str:
    """Strip a single layer of surrounding quotes if present."""
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    return s


def load_config(config_path: Path | str | None = None) -> dict[str, str]:
    """Read ~/.config/zh/config into a dict.

    Mirrors the bash `source` semantics conservatively: KEY=value pairs,
    lines starting with `#` are comments, surrounding quotes are stripped.
    Env-var-style export prefixes (`export KEY=value`) are accepted.

    Args:
        config_path: Optional override. Falls back to DEFAULT_CONFIG_PATH.

    Returns:
        Dict of config values. Missing file returns {} (caller decides
        whether absence is fatal).
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        return {}

    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        m = _CONFIG_KEY_RE.match(line)
        if not m:
            continue
        out[m.group(1)] = _strip_quotes(m.group(2))
    return out


def resolve_token(config: dict[str, str] | None = None) -> str:
    """Resolve the ZenHub GraphQL token.

    Priority: ZH_TOKEN env var > config file. Raises ZhApiError if absent
    (or empty), since every GraphQL call needs it.
    """
    if env_token := os.environ.get("ZH_TOKEN", "").strip():
        return env_token
    if config is None:
        config = load_config()
    token = config.get("ZH_TOKEN", "").strip()
    if not token:
        raise ZhApiError(
            "ZH_TOKEN not set. Create ~/.config/zh/config with:\n"
            "  ZH_TOKEN=your_graphql_token\n"
            "Generate at: https://app.zenhub.com/settings/tokens"
        )
    return token


# =============================================================================
# GraphQL transport
# =============================================================================

def graphql_request(
    query: str,
    variables: dict | None = None,
    *,
    token: str | None = None,
    timeout: float = 30.0,
    url: str = ZH_GRAPHQL_URL,
) -> dict:
    """Send a GraphQL request and return the parsed JSON response.

    Errors at every layer are raised as ZhApiError. The caller is
    responsible for interpreting `errors` arrays inside the response
    (some ZenHub queries return partial data with errors).

    Args:
        query: GraphQL query string.
        variables: Optional variables dict.
        token: Optional explicit token (else uses resolve_token()).
        timeout: HTTP timeout seconds.
        url: GraphQL endpoint URL (default ZenHub public endpoint).

    Returns:
        Parsed JSON body (typically with `data` and possibly `errors`).
    """
    if token is None:
        token = resolve_token()
    payload = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # Try to extract the body for a meaningful error
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        raise ZhApiError(
            f"HTTP {e.code} from ZenHub GraphQL: {err_body or e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise ZhApiError(f"Transport error to ZenHub GraphQL: {e.reason}") from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise ZhApiError(f"Non-JSON response from ZenHub: {body[:200]!r}") from e


def check_graphql_errors(response: dict, *, context: str = "") -> None:
    """Raise ZhApiError if the GraphQL response has top-level `errors`.

    GraphQL allows partial-data responses where `data` is populated and
    `errors` is present. For mutations and structural queries the MCP
    relies on, we treat any top-level error as fatal.
    """
    errors = response.get("errors") or []
    if errors:
        msg = "; ".join(
            e.get("message", str(e)) for e in errors if isinstance(e, dict)
        )
        prefix = f"{context}: " if context else ""
        raise ZhApiError(f"{prefix}GraphQL errors: {msg or errors}")


# =============================================================================
# Repo + workspace resolution
# =============================================================================

_GH_URL_RE = re.compile(
    r"(?:git@github\.com:|https?://github\.com/)"
    r"(?P<owner>[^/]+)/"
    r"(?P<repo>[^/.]+?)(?:\.git)?/?$"
)


def get_owner_repo_from_git(cwd: str | os.PathLike | None = None) -> str:
    """Resolve `owner/repo` from the cwd's git origin remote.

    Raises ZhApiError if not a git repo, no origin remote, or origin URL
    doesn't parse as a GitHub URL.
    """
    try:
        out = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise ZhApiError(
            "Could not determine repository from git remote. "
            "Run this MCP tool with an explicit repo_path that points to "
            "a git checkout with a GitHub remote, or pass owner_repo "
            "directly to the underlying helper."
        ) from e

    m = _GH_URL_RE.search(out)
    if not m:
        raise ZhApiError(
            f"Origin remote URL did not parse as GitHub URL: {out!r}"
        )
    return f"{m.group('owner')}/{m.group('repo')}"


def get_gh_repo_id(owner_repo: str, *, gh_token: str | None = None) -> int:
    """Get the GitHub numeric repo ID via the GitHub REST API.

    Used as the input to ZenHub's `repositoryByGhId` / `repositoriesByGhId`
    queries.

    Args:
        owner_repo: "owner/repo" string. Case is preserved on input but
            GitHub matches case-insensitively.
        gh_token: Optional GitHub token. Falls back to `gh auth token`
            (matches the bash script's behaviour).
    """
    if gh_token is None:
        try:
            gh_token = subprocess.check_output(
                ["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise ZhApiError(
                "Could not get GitHub token via `gh auth token`. "
                "Either authenticate the gh CLI or pass gh_token explicitly."
            ) from e

    url = f"https://api.github.com/repos/{owner_repo}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {gh_token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ZhApiError(
            f"GitHub API HTTP {e.code} for repos/{owner_repo}: {e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise ZhApiError(f"GitHub API transport error: {e.reason}") from e

    repo_id = body.get("id")
    if not isinstance(repo_id, int):
        raise ZhApiError(
            f"GitHub API returned no numeric id for repos/{owner_repo}"
        )
    return repo_id


_REPO_ID_QUERY = """
query($ghIds: [Int!]!) {
  repositoriesByGhId(ghIds: $ghIds) {
    id
    name
    ownerName
  }
}
"""


def get_zenhub_repo_id(
    owner_repo: str, *, token: str | None = None, gh_token: str | None = None
) -> str:
    """Resolve the ZenHub repository ID for a GitHub `owner/repo`.

    Performs the same two-step resolution `zh` does in bash: gh REST →
    numeric repo id → ZenHub `repositoriesByGhId`.
    """
    gh_id = get_gh_repo_id(owner_repo, gh_token=gh_token)
    resp = graphql_request(
        _REPO_ID_QUERY, {"ghIds": [gh_id]}, token=token
    )
    check_graphql_errors(resp, context="repositoriesByGhId")
    nodes = (resp.get("data") or {}).get("repositoriesByGhId") or []
    if not nodes:
        raise ZhApiError(
            f"No ZenHub repository found for GitHub {owner_repo} (id {gh_id}). "
            "Connect the repo to a ZenHub workspace first."
        )
    return nodes[0]["id"]


_WORKSPACE_QUERY = """
query($ghIds: [Int!]!) {
  repositoriesByGhId(ghIds: $ghIds) {
    id
    workspacesConnection(first: 50) {
      nodes {
        id
        name
      }
    }
  }
}
"""


def get_workspace_id(
    owner_repo: str,
    *,
    workspace_name: str | None = None,
    token: str | None = None,
    gh_token: str | None = None,
) -> str:
    """Resolve the ZenHub workspace ID for a repo.

    If `workspace_name` is provided (case-insensitive match), the matching
    workspace is returned; otherwise the first workspace is used (mirrors
    bash behaviour).

    Raises ZhApiError if no workspace is found or the named workspace
    doesn't exist on this repo.
    """
    gh_id = get_gh_repo_id(owner_repo, gh_token=gh_token)
    resp = graphql_request(
        _WORKSPACE_QUERY, {"ghIds": [gh_id]}, token=token
    )
    check_graphql_errors(resp, context="workspacesConnection")
    repos = (resp.get("data") or {}).get("repositoriesByGhId") or []
    if not repos:
        raise ZhApiError(
            f"No ZenHub repository found for GitHub {owner_repo}"
        )
    nodes = (
        (repos[0].get("workspacesConnection") or {}).get("nodes") or []
    )
    if not nodes:
        raise ZhApiError(f"No workspace found for {owner_repo}")

    if workspace_name:
        want = workspace_name.lower()
        for n in nodes:
            if (n.get("name") or "").lower() == want:
                return n["id"]
        available = ", ".join(n.get("name") or "?" for n in nodes)
        raise ZhApiError(
            f"Workspace {workspace_name!r} not found for {owner_repo}. "
            f"Available: {available}"
        )
    return nodes[0]["id"]


# =============================================================================
# Issue resolution
# =============================================================================

_ISSUE_BY_INFO_QUERY = """
query($repoId: ID!, $issueNumber: Int!) {
  issueByInfo(repositoryId: $repoId, issueNumber: $issueNumber) {
    id
    number
    title
    state
    repository {
      ownerName
      name
    }
    parentIssue {
      id
      number
      title
      repository {
        ownerName
        name
      }
    }
  }
}
"""


def repos_match(a: dict | None, owner_repo: str) -> bool:
    """Case-insensitive comparison of a node's `.repository` vs `owner/repo`.

    Round-4 finding #4 caught a case-sensitive bug on the bash side; we
    do not reproduce it here. `a` is expected to look like
    `{"ownerName": "...", "name": "..."}`.
    """
    if not a:
        return False
    owner, _, repo = owner_repo.partition("/")
    return (
        (a.get("ownerName") or "").lower() == owner.lower()
        and (a.get("name") or "").lower() == repo.lower()
    )


# =============================================================================
# Resolver: bundle the common (owner_repo, repo_id, workspace_id) tuple
# =============================================================================

class RepoContext:
    """Resolved (owner_repo, repo_id, workspace_id) for a working directory.

    The MCP tools each need all three. Bundling them lets the tool body
    fail fast with a single resolution step instead of three round-trips
    that each report their own error.

    The `query` method is regular attribute access (not `__slots__`-pinned)
    so test suites can `patch.object(ctx, "query", ...)` and inject mock
    GraphQL responses without monkey-patching `urllib`.
    """

    def __init__(
        self,
        owner_repo: str,
        repo_id: str,
        workspace_id: str,
        token: str,
    ) -> None:
        self.owner_repo = owner_repo
        self.repo_id = repo_id
        self.workspace_id = workspace_id
        self.token = token

    def query(self, query: str, variables: dict | None = None) -> dict:
        return graphql_request(query, variables, token=self.token)


def resolve_context(
    cwd: str | os.PathLike | None = None,
    *,
    owner_repo: str | None = None,
    workspace_name: str | None = None,
    config: dict[str, str] | None = None,
) -> RepoContext:
    """One-shot resolution of (owner_repo, repo_id, workspace_id, token).

    Args:
        cwd: Working directory to read `git remote` from. Ignored when
            `owner_repo` is supplied.
        owner_repo: Optional explicit "owner/repo" (e.g. when called from
            an MCP tool without a usable git checkout).
        workspace_name: Optional workspace name for multi-workspace repos.
            Falls back to ZH_WORKSPACE config var, then first workspace.
        config: Pre-loaded config dict (else loaded from default path).
    """
    if config is None:
        config = load_config()
    token = resolve_token(config)
    if owner_repo is None:
        owner_repo = get_owner_repo_from_git(cwd=cwd)

    # Workspace name: explicit arg > ZH_WORKSPACE env > config > None
    if workspace_name is None:
        workspace_name = (
            os.environ.get("ZH_WORKSPACE", "").strip()
            or config.get("ZH_WORKSPACE", "").strip()
            or None
        )

    repo_id = get_zenhub_repo_id(owner_repo, token=token)
    workspace_id = get_workspace_id(
        owner_repo, workspace_name=workspace_name, token=token
    )
    return RepoContext(owner_repo, repo_id, workspace_id, token)
