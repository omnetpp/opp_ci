"""Content-addressable fingerprinting for TestRuns (Phase 4).

`compute_cache_fingerprint(job)` returns a SHA-256 hex string keyed on
every input that can change the *outcome* of a TestRun. Two jobs with
the same fingerprint must produce the same outcome (modulo
non-determinism in the test itself, which the cache treats as a known
risk), so a TestRun already finished with that fingerprint can be
attributed to a fresh cell without rerunning.

The fingerprint deliberately *excludes*:

  * Expected result code / description — expectations are post-hoc
    annotations and a cache hit is graded against the currently-in-force
    expectation, not the one in force when the cached run finished.
  * Anything from the TestMatrixRun trigger / GitHub identity — same
    code, same outcome, regardless of which webhook called for it.

Conservative misses are always safe. If a SHA resolution fails (e.g.
GitHub is unreachable), the fingerprint falls back to the literal ref
string — moving refs then *don't* cache-hit until SHA resolution
recovers, which is the right side of the safety budget.
"""

import hashlib
import json
import logging

_logger = logging.getLogger(__name__)


# Coordinate axes whose values participate in the fingerprint. These are
# a superset of TEST_COORD_FIELDS plus the per-run knobs (git_ref,
# resolved_deps). Adding/removing a field is a fingerprint-rekey event.
_FINGERPRINT_AXES = (
    "project", "kind", "mode",
    "os", "os_version",
    "distro", "distro_version",
    "flavor", "flavor_version",
    "arch",
    "compiler", "compiler_version",
    "isolation", "toolchain",
    "opp_file",
)


def _resolve_git_ref_to_sha(project_name, git_ref):
    """Best-effort lookup of the SHA `git_ref` resolves to on the project's
    GitHub repository.

    Returns the SHA on success, the literal `git_ref` on any failure —
    conservative caching prefers a miss over an incorrect hit.
    """
    if not git_ref:
        return None
    if len(git_ref) == 40 and all(c in "0123456789abcdef" for c in git_ref.lower()):
        # Already a SHA; no lookup needed.
        return git_ref.lower()

    try:
        from opp_ci.db.connection import SessionLocal
        from opp_ci.db.models import Project
        from opp_ci.github.client import GitHubClient
        from sqlalchemy import select

        session = SessionLocal()
        try:
            proj = session.execute(
                select(Project).where(Project.name == project_name)
            ).scalar_one_or_none()
            if proj is None or not proj.github_owner or not proj.github_repo:
                return git_ref
            client = GitHubClient()
            if not client.is_configured:
                return git_ref
            # Try a few common GitHub endpoints; any one succeeding is enough.
            for ref_path in (f"heads/{git_ref}", f"tags/{git_ref}", git_ref):
                try:
                    sha = client.resolve_ref(proj.github_owner, proj.github_repo, ref_path)
                    if sha:
                        return sha
                except Exception:
                    continue
            return git_ref
        finally:
            session.close()
    except Exception as e:
        _logger.debug("ref-resolution fell back to literal for %s/%s: %s",
                      project_name, git_ref, e)
        return git_ref


def _normalised_deps(resolved_deps):
    """Canonicalise resolved_deps into a sorted-keys dict.

    `None` and empty dict are equivalent for fingerprint purposes.
    """
    if not resolved_deps:
        return {}
    if isinstance(resolved_deps, dict):
        return {k: resolved_deps[k] for k in sorted(resolved_deps)}
    # Defensive: stringify whatever it is so callers don't crash.
    return {"_raw": str(resolved_deps)}


def compute_cache_fingerprint(job, *, project=None, opp_file=None,
                              resolve_refs=True):
    """SHA-256 hex of the canonical JSON over every fingerprint-bearing input.

    `job` is an expand_matrix job spec (or equivalent dict). When
    `project` / `opp_file` are not on the job, the caller can supply
    them. `resolve_refs=False` skips the GitHub round-trip — useful in
    tests and in CLI paths that want a deterministic fingerprint
    independent of the network.
    """
    payload = {axis: job.get(axis) for axis in _FINGERPRINT_AXES}
    if project is not None:
        payload["project"] = project
    if opp_file is not None and payload.get("opp_file") is None:
        payload["opp_file"] = opp_file
    proj_name = payload.get("project")
    git_ref = job.get("git_ref")
    if resolve_refs:
        payload["resolved_project_ref"] = _resolve_git_ref_to_sha(proj_name, git_ref)
    else:
        payload["resolved_project_ref"] = git_ref
    payload["resolved_deps"] = _normalised_deps(job.get("resolved_deps"))
    # version is the opp_env version identifier; it can pin different
    # install plans even when the project name + ref match.
    payload["version"] = job.get("version")

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
