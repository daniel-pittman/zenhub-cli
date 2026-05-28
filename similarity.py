"""
ZenHub similarity search — finds existing issues that look semantically
similar to a query string (or a proposed new title+body).

Built to support the librarian-style "is this a duplicate?" check that
catches paraphrased duplicates which keyword search misses (e.g. "Auth
token refresh race condition under load" vs "Users randomly logged
out around 5pm" — same underlying bug, no shared keywords).

Architecture:
  - Sentence embeddings via sentence-transformers (all-MiniLM-L6-v2,
    384-dim, ~80MB on disk, ~30s cold start, ~10ms per query)
  - Per-repo pickled cache at ~/.config/zh/index/<owner_repo>.pkl
    (durable across reboots — model + index outlive any rebuild of the
    MCP venv)
  - Delta sync on every query via GitHub's
    `GET /repos/{owner}/{repo}/issues?since=<ISO8601>` filter
    (only re-embeds issues whose title/body/state actually changed)
  - TTL: 5 minutes between delta syncs; queries inside the window
    skip the network round-trip entirely
  - First-run fallback: full index pull if cache is empty or stale
    (>7 days since last sync)

Public surface:
  find_similar(query, repo, top_k=5, threshold=0.35, min_results=0) -> list[Match]
  check_duplicate(title, body, repo) -> dict  # for create_issue pre-flight
  reindex(repo, full=False) -> dict           # manual cache refresh
"""

from __future__ import annotations

import json
import os
import pickle
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

INDEX_DIR = Path(os.path.expanduser("~/.config/zh/index"))
CACHE_VERSION = 1

# How long between automatic delta-syncs. Queries inside this window
# skip the network call entirely.
AUTO_SYNC_TTL_SECONDS = 300  # 5 minutes

# If the cache hasn't been touched in this long, force a full reindex
# instead of trusting the delta. Catches the case where indexed_at is
# very stale and the delta would still be huge.
FULL_REBUILD_AFTER_SECONDS = 7 * 24 * 60 * 60  # 7 days

# all-MiniLM-L6-v2: small (384-dim), fast, decent semantic quality.
# Stored in ~/.cache/huggingface/ by default (persists across reboots).
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Cosine-similarity thresholds for the duplicate check in create_issue.
# >= HARD: very likely a real duplicate; refuse to create unless caller
#          explicitly passes confirm_create=True
# >= SOFT: worth surfacing as candidates but not a hard block
#
# Thresholds calibrated against the RC backlog:
#   - 0.75+ : identical title with different body (same ticket, blocked)
#   - 0.60-0.70: semantically related but distinct (e.g. multiple WCAG
#     contrast fixes on different widgets — surface, don't block)
#   - < 0.55: just a topic neighbor (ignore)
DUPLICATE_HARD_THRESHOLD = 0.70
DUPLICATE_SOFT_THRESHOLD = 0.55

# Cap body length we feed to the embedder. Sentence-transformers handles
# long input fine but the cost scales with token count; 1500 chars is
# plenty for "what is this issue about" semantics.
MAX_BODY_CHARS = 1500

# -----------------------------------------------------------------------------
# Model — lazy-loaded singleton
# -----------------------------------------------------------------------------

_MODEL = None


def _get_model():
    """Load the sentence-transformer model on first use. Subsequent calls
    are no-ops. The model itself is cached on disk by HuggingFace under
    ~/.cache/huggingface/ so it only downloads once."""
    global _MODEL
    if _MODEL is None:
        # Suppress sentence-transformers' verbose load chatter on stderr.
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        from sentence_transformers import SentenceTransformer

        _MODEL = SentenceTransformer(DEFAULT_MODEL)
    return _MODEL


def _embed(text: str):
    """Return a normalized embedding (L2 norm = 1) so dot product = cosine."""
    return _get_model().encode(
        text or "",
        convert_to_numpy=True,
        normalize_embeddings=True,
    )


# -----------------------------------------------------------------------------
# Cache I/O
# -----------------------------------------------------------------------------


@dataclass
class IssueEntry:
    """One indexed issue."""

    number: int
    repo: str  # owner/repo
    title: str
    body_preview: str  # truncated to MAX_BODY_CHARS
    state: str  # "open" | "closed"
    updated_at: str  # ISO8601, as reported by the GitHub API
    embedding: object  # numpy.ndarray; kept as object to avoid hard numpy import here


def _cache_path(repo: str) -> Path:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    safe = repo.replace("/", "_").replace(":", "_")
    return INDEX_DIR / f"{safe}.pkl"


def _empty_cache() -> dict:
    return {"version": CACHE_VERSION, "indexed_at": None, "entries": {}}


def _load_cache(repo: str) -> dict:
    p = _cache_path(repo)
    if not p.exists():
        return _empty_cache()
    try:
        with p.open("rb") as f:
            data = pickle.load(f)
    except (pickle.PickleError, EOFError, OSError):
        return _empty_cache()
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return _empty_cache()
    return data


def _save_cache(repo: str, cache: dict) -> None:
    p = _cache_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write to a tempfile then rename — survives crashes mid-write.
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("wb") as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, p)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds_since(iso: Optional[str]) -> float:
    if not iso:
        return float("inf")
    try:
        ts = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - ts).total_seconds()


# -----------------------------------------------------------------------------
# Repo derivation
# -----------------------------------------------------------------------------


# Repo names on GitHub can contain dots ("docs.github.io",
# "my.tool", "internal.docs").
#
# The prefix `(?:git@github\.com:|https?://github\.com/)` is the
# canonical form, unified with `zh_api._GH_URL_RE` and the bash
# regex in `zh:get_repo_info`. The source URL always comes from
# `git remote get-url origin` (or the `ZH_REPO` config override),
# which produces either `git@github.com:owner/repo[.git]` (ssh) or
# `https://github.com/owner/repo[.git]` (https). Schemes like
# `ssh://git@github.com/...`, `git://...`, and `git+ssh://...` are
# accepted by some git clients but are NOT what `git remote get-url`
# emits, so the regex rejects them to keep the three parser
# locations in lockstep. Round-4 finding #5.
_GITHUB_URL_RE = re.compile(
    # `^` anchor (round-5 #6) rejects garbage prefixes. Matches the
    # zh_api._GH_URL_RE form exactly.
    r"^(?:git@github\.com:|https?://github\.com/)"
    r"([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def repo_from_cwd(cwd: str) -> str:
    """Derive `owner/repo` from a git checkout's origin remote.

    Supports both https and ssh remote URL forms.
    Raises RuntimeError on failure.

    Uses `git remote get-url origin` rather than `git config --get
    remote.origin.url` — the former honors `url.<x>.insteadOf`
    rewriting that the user may have configured globally (typical
    for corporate-network mirrors or push/pull splits). The latter
    returns the raw config value, which can disagree with what
    `git fetch` / `git push` actually use. `zh_api.get_owner_repo_
    from_git` already uses `git remote get-url`; aligning here so
    the similarity cache and the MCP context resolution see the
    same effective remote. Round-5 #7.
    """
    try:
        url = subprocess.check_output(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Cannot derive repo from {cwd}: {e.stderr.strip() or 'not a git checkout'}"
        )
    m = _GITHUB_URL_RE.search(url)
    if not m:
        raise RuntimeError(f"Cannot parse GitHub repo from URL: {url}")
    return f"{m.group(1)}/{m.group(2)}"


# -----------------------------------------------------------------------------
# GitHub API helpers
# -----------------------------------------------------------------------------


def _gh_api_paginated(path: str) -> list[dict]:
    """Call `gh api --paginate <path>` and return the flattened JSON array.

    `gh --paginate` concatenates result arrays across pages into a single
    JSON document. Raises RuntimeError on non-zero exit.
    """
    result = subprocess.run(
        ["gh", "api", "--paginate", path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api failed: {result.stderr.strip() or 'unknown error'}"
        )
    # When --paginate concatenates JSON arrays, the output is one valid
    # JSON array; when it concatenates JSON objects (less common) it
    # emits one object per line. Issues endpoint returns an array.
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Fall back: parse line-by-line in case gh emitted concatenated
        # JSON documents.
        data = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, list):
                    data.extend(parsed)
                else:
                    data.append(parsed)
            except json.JSONDecodeError:
                continue
    if isinstance(data, dict):
        data = [data]
    return data


def _fetch_issues(repo: str, since: Optional[str] = None,
                  state: str = "all") -> list[dict]:
    """Pull issues from GitHub. `since` filters by `updated_at >=` if set."""
    path = (
        f"repos/{repo}/issues"
        f"?per_page=100&state={state}&sort=updated&direction=desc"
    )
    if since:
        # `since` is documented to filter issues updated at or after the
        # given ISO 8601 timestamp.
        path += f"&since={since}"
    raw = _gh_api_paginated(path)
    # Filter out PRs: GitHub returns them in the issues endpoint and
    # marks them with a `pull_request` key.
    return [i for i in raw if "pull_request" not in i]


# -----------------------------------------------------------------------------
# Index build / sync
# -----------------------------------------------------------------------------


def _build_entry(issue: dict, embedding) -> IssueEntry:
    body = issue.get("body") or ""
    return IssueEntry(
        number=int(issue["number"]),
        repo=issue["repository_url"].rsplit("/", 2)[-2]
        + "/"
        + issue["repository_url"].rsplit("/", 1)[-1],
        title=issue.get("title") or "",
        body_preview=body[:MAX_BODY_CHARS],
        state=issue.get("state") or "open",
        updated_at=issue.get("updated_at") or "",
        embedding=embedding,
    )


def _embedding_text(title: str, body: str) -> str:
    """Combined input fed to the embedder. Title is the strongest signal
    so we prepend it; body adds context for the semantic match."""
    body_part = (body or "")[:MAX_BODY_CHARS]
    return f"{title or ''}\n\n{body_part}".strip()


def _apply_delta(cache: dict, issues: list[dict]) -> tuple[int, int, int]:
    """Update cache entries from a list of changed issues.

    Returns (added, updated, removed) counts.
    """
    added = updated = removed = 0
    for issue in issues:
        n = int(issue["number"])
        key = str(n)
        if issue.get("state") == "closed":
            # We index open issues only — drop closed entries from the cache.
            if key in cache["entries"]:
                del cache["entries"][key]
                removed += 1
            continue
        existing = cache["entries"].get(key)
        text = _embedding_text(issue.get("title") or "", issue.get("body") or "")
        emb = _embed(text)
        entry = _build_entry(issue, emb)
        cache["entries"][key] = entry
        if existing is None:
            added += 1
        else:
            updated += 1
    return added, updated, removed


def reindex(repo: str, *, full: bool = False) -> dict:
    """Refresh the embeddings cache for `repo`.

    Args:
        repo: "owner/repo"
        full: if True, rebuild from scratch ignoring any existing cache.
              Otherwise do a delta sync from the cache's indexed_at.

    Returns:
        dict with: ok, repo, mode ('full'/'delta'/'skipped'),
                   added, updated, removed, indexed_at, total_entries
    """
    cache = _empty_cache() if full else _load_cache(repo)

    since = None
    mode = "full"
    if not full and cache.get("indexed_at"):
        age = _seconds_since(cache["indexed_at"])
        if age >= FULL_REBUILD_AFTER_SECONDS:
            cache = _empty_cache()
            mode = "full"
        else:
            since = cache["indexed_at"]
            mode = "delta"

    issues = _fetch_issues(repo, since=since, state="all")
    if mode == "full":
        # Full pull returns everything — open + closed mixed. We index
        # only open ones, but we also need to remove any closed entries
        # that snuck in from a prior partial state.
        cache["entries"] = {}

    added, upd, removed = _apply_delta(cache, issues)
    cache["indexed_at"] = _now_iso()
    _save_cache(repo, cache)

    return {
        "ok": True,
        "repo": repo,
        "mode": mode,
        "added": added,
        "updated": upd,
        "removed": removed,
        "indexed_at": cache["indexed_at"],
        "total_entries": len(cache["entries"]),
    }


def _auto_sync(repo: str) -> dict:
    """Run a delta sync if the cache is older than AUTO_SYNC_TTL_SECONDS.

    Returns a brief status dict (may be empty if nothing to do).
    """
    cache = _load_cache(repo)
    age = _seconds_since(cache.get("indexed_at"))
    if age >= AUTO_SYNC_TTL_SECONDS:
        return reindex(repo, full=(not cache.get("indexed_at")))
    return {"ok": True, "repo": repo, "mode": "skipped",
            "indexed_at": cache.get("indexed_at"),
            "total_entries": len(cache.get("entries", {}))}


# -----------------------------------------------------------------------------
# Query
# -----------------------------------------------------------------------------


@dataclass
class Match:
    number: int
    repo: str
    title: str
    body_preview: str
    state: str
    similarity: float
    # True when `similarity >= threshold` at query time. False for
    # entries surfaced only because `min_results` backfilled the result
    # set below the threshold — lets callers distinguish "strong match"
    # from "closest we could find" instead of seeing a bare empty list.
    meets_threshold: bool = True

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "repo": self.repo,
            "title": self.title,
            "body_preview": self.body_preview,
            "state": self.state,
            "similarity": round(self.similarity, 4),
            "meets_threshold": self.meets_threshold,
        }


def find_similar(query_text: str, repo: str, *, top_k: int = 5,
                 threshold: float = 0.35,
                 min_results: int = 0,
                 auto_sync: bool = True) -> list[Match]:
    """Return top-K issues semantically similar to `query_text` from `repo`.

    Args:
        query_text: the text to compare against. For pre-flight duplicate
            checks, pass `title + body` of the candidate issue.
        repo: "owner/repo"
        top_k: max results to return
        threshold: minimum cosine similarity (0.0–1.0) for a match to be
            flagged `meets_threshold=True`. Default 0.35 — calibrated for
            short ad-hoc `zh_similar` lookups, where a keyword-style query
            (vs. a full title+body) embeds more diffusely and scores
            lower. The `create_issue` duplicate pre-flight passes its own
            higher thresholds (0.55 soft / 0.70 hard) since it feeds rich
            title+body text.
        min_results: backfill the result set with the highest-scoring
            BELOW-threshold entries until it holds this many (capped at
            `top_k`). Lets `zh_similar` always surface the closest issues
            — annotated `meets_threshold=False` — instead of returning a
            bare empty list when nothing clears the bar. Default 0
            preserves the strict "matches only" behavior the
            `create_issue` pre-flight relies on.
        auto_sync: if True, transparently delta-sync the cache before
            querying (skipped if cache is fresh per AUTO_SYNC_TTL_SECONDS).

    Returns:
        List of Match objects, sorted by similarity descending. Each
        carries a `meets_threshold` flag.
    """
    if auto_sync:
        _auto_sync(repo)

    cache = _load_cache(repo)
    entries: dict[str, IssueEntry] = cache.get("entries", {})
    if not entries:
        return []

    import numpy as np

    q = _embed(query_text)
    # Score every entry, then sort once. Embeddings are normalized so
    # dot product == cosine similarity.
    scored = sorted(
        ((float(np.dot(q, e.embedding)), e) for e in entries.values()),
        key=lambda t: t[0],
        reverse=True,
    )

    above = [(sim, e) for sim, e in scored if sim >= threshold]
    selected = above
    if len(selected) < min_results:
        # Backfill from the next-highest below-threshold entries so the
        # caller always sees the closest candidates (annotated as not
        # meeting the threshold) rather than nothing.
        below = [(sim, e) for sim, e in scored if sim < threshold]
        selected = above + below[: min_results - len(above)]
    selected = selected[:top_k]

    return [
        Match(
            number=e.number,
            repo=e.repo,
            title=e.title,
            body_preview=e.body_preview[:200],
            state=e.state,
            similarity=sim,
            meets_threshold=(sim >= threshold),
        )
        for sim, e in selected
    ]


def check_duplicate(title: str, body: str, repo: str) -> dict:
    """Pre-flight duplicate check for create_issue.

    Returns a dict describing what to do:
        {
          "ok": True,
          "matches": [...top candidates...],
          "any_above_hard": bool,    # at least one match >= HARD threshold
          "any_above_soft": bool,    # at least one match >= SOFT threshold
          "recommendation": "create" | "warn" | "block",
        }

    Callers may pass `confirm_create=True` to bypass a "block"
    recommendation.
    """
    query = _embedding_text(title, body)
    matches = find_similar(
        query, repo, top_k=5, threshold=DUPLICATE_SOFT_THRESHOLD
    )
    any_hard = any(m.similarity >= DUPLICATE_HARD_THRESHOLD for m in matches)
    any_soft = any(m.similarity >= DUPLICATE_SOFT_THRESHOLD for m in matches)
    if any_hard:
        rec = "block"
    elif any_soft:
        rec = "warn"
    else:
        rec = "create"
    return {
        "ok": True,
        "matches": [m.to_dict() for m in matches],
        "any_above_hard": any_hard,
        "any_above_soft": any_soft,
        "recommendation": rec,
        "hard_threshold": DUPLICATE_HARD_THRESHOLD,
        "soft_threshold": DUPLICATE_SOFT_THRESHOLD,
    }
