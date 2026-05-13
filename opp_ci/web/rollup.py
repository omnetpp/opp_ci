"""
Roll-up summary logic for test results.

Merges runs with the same status into summary rows.  Each row reports
per-dimension values: a single value when that dimension is constant
across the group, or a set of values when it varies.  A Cartesian
indicator (● or ○) shows whether the varying dimensions form a complete
cross-product.

Dimensions: project, test_type, mode, os, os_version, compiler,
compiler_version, git_ref.
"""

from collections import defaultdict
from functools import reduce
from operator import mul

ALL_DIMENSIONS = [
    "project", "test_type", "mode", "os", "os_version",
    "compiler", "compiler_version", "git_ref",
]


def rollup_runs(runs):
    """
    Produce a rolled-up summary of test runs.

    Algorithm:
    1. Group runs by status.
    2. Within each status group, find maximal merge groups: runs that
       share the same "signature" (the set of dimensions that are constant
       vs varying) AND can be merged into one row.
    3. For each merged row, classify dimensions and check Cartesian.

    Returns a list of dicts (one per summary row):
        {
            "columns": {dim: value_or_set, ...},
            "total": int,
            "cartesian": bool,
            "uniform": bool,
            "uniform_status": str | None,
            "breakdown": {"PASS": n, ...},
            "run_ids": [int, ...],
        }

    columns[dim] is either:
        - a string (single constant value)
        - a sorted list of strings (multiple values → shown as {v1,v2,...})
        - None (dimension has no value for any run in the group)
    """
    if not runs:
        return []

    # Group by status
    by_status = defaultdict(list)
    for run in runs:
        by_status[run.status.value].append(run)

    summaries = []
    for status, status_runs in by_status.items():
        summaries.extend(_merge_uniform_group(status_runs))

    # Sort: non-uniform first (shouldn't exist, but defensive), then by
    # first run id for stable ordering
    summaries.sort(key=lambda s: (s["uniform_status"] or "", s["run_ids"][0]))
    return summaries


def _merge_uniform_group(runs):
    """
    Given a list of runs all with the same status, find merge groups.

    Runs are grouped by their "constant signature" — the set of dimensions
    where they all share the same value.  Within each signature group, we
    then sub-group by the actual constant values so that rows with different
    constant values don't merge.
    """
    # For each run, compute its dimension values
    run_dims = []
    for run in runs:
        vals = {}
        for dim in ALL_DIMENSIONS:
            v = getattr(run, dim, None)
            vals[dim] = v
        run_dims.append((run, vals))

    # Group by the tuple of constant dimension values
    # Two runs merge if they share the same values on all constant dims
    # Strategy: group by the full tuple of values for constant dims,
    # where "constant" means: that dimension has the same value across
    # all runs in the prospective group.
    #
    # Simple approach: group runs that are identical on ALL dimensions
    # except the ones that vary.  We use iterative merging.
    groups = _find_merge_groups(run_dims)

    summaries = []
    for group_runs in groups:
        summaries.append(_make_summary(group_runs))
    return summaries


def _find_merge_groups(run_dims):
    """
    Partition runs into merge groups.

    Two runs can be in the same group if:
    - They have the same status (already guaranteed by caller)
    - The group remains describable: each dimension is either constant
      (same value for all runs) or varying (different values exist)

    We use a greedy approach: start with all runs as one group, then split
    on dimensions where splitting reduces ambiguity.  Actually simpler:
    try to merge everything, check if it makes sense, if not split by the
    dimension with most distinct values first.

    For now: group by all-constant-dims signature (dims where a run's value
    matches the most common value pattern).  Pragmatic: group by the tuple
    of values on dimensions that have only ONE distinct value across all runs.
    """
    if not run_dims:
        return []

    # Try: put all runs in one group
    all_runs = [r for r, _ in run_dims]
    # Check which dims are constant vs varying
    constant_dims, varying_dims = _classify_dims(run_dims)

    if not varying_dims:
        # All dimensions constant → one group
        return [all_runs]

    # If there are varying dims, we need to check if sub-groups exist
    # where runs share constant values but differ on varying dims.
    # Group by the values of constant dimensions.
    groups_by_const = defaultdict(list)
    for run, vals in run_dims:
        key = tuple(vals[d] for d in constant_dims)
        groups_by_const[key].append(run)

    return list(groups_by_const.values())


def _classify_dims(run_dims):
    """
    Classify dimensions into constant (single value) and varying (multiple values).
    Returns (constant_dims, varying_dims) — each a list of dimension names.
    """
    constant = []
    varying = []
    for dim in ALL_DIMENSIONS:
        values = set(vals[dim] for _, vals in run_dims)
        if len(values) <= 1:
            constant.append(dim)
        else:
            varying.append(dim)
    return constant, varying


def _is_cartesian(runs, varying_dims):
    """
    Check if the runs form a complete Cartesian product over the varying dims.
    True when |runs| == product of |distinct values| per varying dim.
    """
    if not varying_dims:
        return True

    dim_sizes = []
    for dim in varying_dims:
        values = set(getattr(r, dim, None) for r in runs)
        dim_sizes.append(len(values))

    expected = reduce(mul, dim_sizes, 1)
    return len(runs) == expected


def _make_summary(runs):
    """Build a summary dict for a merged group of runs."""
    run_dims = [(r, {d: getattr(r, d, None) for d in ALL_DIMENSIONS}) for r in runs]
    constant_dims, varying_dims = _classify_dims(run_dims)

    columns = {}
    for dim in ALL_DIMENSIONS:
        values = sorted(set(v for v in (vals[dim] for _, vals in run_dims) if v is not None))
        if len(values) == 0:
            columns[dim] = None
        elif len(values) == 1:
            columns[dim] = values[0]
        else:
            columns[dim] = values  # list → multi-value

    cartesian = _is_cartesian(runs, varying_dims)

    breakdown = defaultdict(int)
    run_ids = []
    for r in runs:
        breakdown[r.status.value] += 1
        run_ids.append(r.id)

    total = len(runs)
    statuses = list(breakdown.keys())
    uniform = len(statuses) == 1
    uniform_status = statuses[0] if uniform else None

    return {
        "columns": columns,
        "total": total,
        "cartesian": cartesian,
        "uniform": uniform,
        "uniform_status": uniform_status,
        "breakdown": dict(breakdown),
        "run_ids": run_ids,
    }
