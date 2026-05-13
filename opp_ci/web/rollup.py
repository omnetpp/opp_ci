"""
Roll-up summary logic for test results.

Groups results **hierarchically** along a static dimension order and collapses
groups where all results share the same status into a single summary line.
Only expands to the next dimension level when statuses within a group differ.

The dimension order is static for now:
    project → test_type → mode → os → os_version → compiler → compiler_version

Later this should be computed dynamically based on which dimensions have
variation in the result set.
"""

from collections import OrderedDict

# Static hierarchical dimension order.
# Later: compute from result variance.
ALL_DIMENSIONS = ["project", "test_type", "mode", "os", "os_version", "compiler", "compiler_version"]


def rollup_runs(runs):
    """
    Given a list of TestRun objects, produce a hierarchical roll-up summary.

    The algorithm:
    1. Group at the coarsest level (first dimension only).
    2. If all runs in a group share the same status → emit one collapsed line.
    3. If statuses differ → expand by adding the next dimension and recurse.

    Returns a list of dicts:
        {
            "key": "inet / smoke",
            "dimensions": {"project": "inet", "test_type": "smoke"},
            "total": 6,
            "breakdown": {"PASS": 3, "FAIL": 3},
            "uniform": False,
            "uniform_status": None,
            "run_ids": [1, 2, 3, 4, 5, 6],
        }
    """
    if not runs:
        return []

    # Determine which dimensions are relevant (have non-None values)
    relevant_dims = []
    for dim in ALL_DIMENSIONS:
        values = set(getattr(r, dim, None) for r in runs)
        values.discard(None)
        if values:
            relevant_dims.append(dim)

    if not relevant_dims:
        relevant_dims = ["project"]

    return _rollup_recursive(runs, relevant_dims, depth=0)


def _rollup_recursive(runs, dims, depth):
    """
    Recursively roll up runs. At each depth, group by dims[0..depth].
    If a group is uniform → emit collapsed summary.
    If not and more dimensions remain → expand to next depth.
    If not and no more dimensions → emit expanded summary with breakdown.
    """
    if depth >= len(dims):
        # No more dimensions to expand; emit as-is
        return [_make_summary(runs, dims, len(dims))]

    # Group by dimensions 0..depth (inclusive)
    group_dims = dims[:depth + 1]
    groups = OrderedDict()
    for run in runs:
        key = tuple(getattr(run, dim, None) for dim in group_dims)
        if key not in groups:
            groups[key] = []
        groups[key].append(run)

    summaries = []
    for key, group_runs in groups.items():
        statuses = set(r.status.value for r in group_runs)
        if len(statuses) == 1:
            # Uniform — collapse into one line
            summaries.append(_make_summary(group_runs, group_dims, depth + 1))
        elif depth + 1 < len(dims):
            # Non-uniform — expand to next dimension level
            summaries.extend(_rollup_recursive(group_runs, dims, depth + 1))
        else:
            # Non-uniform but no more dimensions — show breakdown
            summaries.append(_make_summary(group_runs, group_dims, depth + 1))

    return summaries


def _make_summary(group_runs, dims_used, depth):
    """Build a summary dict for a group of runs."""
    breakdown = {}
    run_ids = []
    for r in group_runs:
        status_val = r.status.value
        breakdown[status_val] = breakdown.get(status_val, 0) + 1
        run_ids.append(r.id)

    total = len(group_runs)
    statuses = list(breakdown.keys())
    uniform = len(statuses) == 1
    uniform_status = statuses[0] if uniform else None

    key_parts = {}
    for dim in dims_used:
        val = getattr(group_runs[0], dim, None)
        key_parts[dim] = val

    display_key = " / ".join(str(v) for v in key_parts.values() if v is not None)

    return {
        "key": display_key,
        "dimensions": key_parts,
        "total": total,
        "breakdown": breakdown,
        "uniform": uniform,
        "uniform_status": uniform_status,
        "run_ids": run_ids,
        **key_parts,
    }
