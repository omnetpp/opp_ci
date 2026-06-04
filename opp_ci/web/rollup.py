"""
Roll-up summary logic for test results.

Merges runs with the same status into summary rows.  Each row reports
per-dimension values: a single value when that dimension is constant
across the group, or a set of values when it varies.  A Cartesian
indicator (● or ○) shows whether the varying dimensions form a complete
cross-product.  A separate "repetitions" value reports k when each cell
is filled exactly k times; the UI shows a Reps column only when some row
has k>1.

Primary dimensions: project, test, mode, os, os_version, compiler,
compiler_version, git_ref.

Extra dimensions (isolation, toolchain, commit_sha, version) participate
in classification and the Cartesian check but only appear as columns when
they actually vary on the page.
"""

from collections import Counter, defaultdict
from functools import reduce
from operator import mul

PRIMARY_DIMENSIONS = [
    "project", "test", "mode", "os", "os_version",
    "compiler", "compiler_version", "git_ref",
]

EXTRA_DIMENSIONS = ["isolation", "toolchain", "commit_sha", "version"]

ALL_DIMENSIONS = PRIMARY_DIMENSIONS + EXTRA_DIMENSIONS


def rollup_runs(runs, grouping="any"):
    """
    Produce a rolled-up summary of test runs.

    Algorithm:
    1. Group runs by status.
    2. Within each status group, find maximal merge groups: runs that
       share the same "signature" (the set of dimensions that are constant
       vs varying) AND can be merged into one row.
    3. For each merged row, classify dimensions and check Cartesian.

    grouping controls merge behavior:
        "any"       - merge freely (default)
        "cartesian" - split non-Cartesian groups into maximal Cartesian sub-groups
        "none"      - no grouping, each run is its own row

    Returns a list of dicts (one per summary row):
        {
            "columns": {dim: value_or_set, ...},
            "total": int,
            "cartesian": bool,
            "repetitions": int,   # k when each cell has k runs (>=1)
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

    cartesian_only = (grouping == "cartesian")

    summaries = []
    for status, status_runs in by_status.items():
        if grouping == "none":
            for run in status_runs:
                summaries.append(_make_summary([run]))
        else:
            merged = _merge_uniform_group(status_runs)
            if cartesian_only:
                for s in merged:
                    if s["cartesian"] or s["total"] == 1:
                        summaries.append(s)
                    else:
                        # Try to find Cartesian sub-groups by splitting on
                        # a varying dimension
                        group_runs = [r for r in status_runs if r.id in s["run_ids"]]
                        summaries.extend(_split_to_cartesian(group_runs))
            else:
                summaries.extend(merged)

    # Sort by dimension columns left-to-right (constant values before
    # sets before nulls), then by status, then by first run id.
    def _sort_key(s):
        cols = s["columns"]
        dim_keys = []
        for dim in ALL_DIMENSIONS:
            val = cols.get(dim)
            if val is None:
                dim_keys.append((2, ""))
            elif isinstance(val, str):
                dim_keys.append((0, val))
            else:
                dim_keys.append((1, val[0] if val else ""))
        return (*dim_keys, s["uniform_status"] or "", s["run_ids"][0])

    summaries.sort(key=_sort_key)
    return summaries


def visible_extra_dims(summaries):
    """
    Return the subset of EXTRA_DIMENSIONS that should be rendered as columns
    for this page — dims where values differ across the summaries (or appear
    as a varying set within any single summary).
    """
    visible = []
    for dim in EXTRA_DIMENSIONS:
        seen = set()
        varying_inside_row = False
        for s in summaries:
            v = s["columns"].get(dim)
            if v is None:
                continue
            if isinstance(v, str):
                seen.add(v)
            else:
                varying_inside_row = True
                seen.update(v)
        if varying_inside_row or len(seen) > 1:
            visible.append(dim)
    return visible


def _split_to_cartesian(runs):
    """
    Split a non-Cartesian group into maximal Cartesian sub-groups.

    Tries splitting on each varying dimension.  Picks the split that
    produces the most Cartesian sub-groups (by total runs covered).
    Recurses on any remaining non-Cartesian pieces.
    """
    run_dims = [(r, {d: getattr(r, d, None) for d in ALL_DIMENSIONS}) for r in runs]
    _, varying_dims = _classify_dims(run_dims)

    if not varying_dims:
        return [_make_summary(runs)]

    # Try splitting on each varying dimension; pick the best.
    # Prefer: most runs covered by Cartesian groups, then fewest groups.
    best_results = None
    best_cartesian_count = -1
    best_group_count = float('inf')

    for split_dim in varying_dims:
        # Group runs by the value of split_dim
        by_val = defaultdict(list)
        for run in runs:
            by_val[getattr(run, split_dim, None)].append(run)

        results = []
        cartesian_count = 0
        for val_runs in by_val.values():
            s = _make_summary(val_runs)
            if s["cartesian"]:
                cartesian_count += len(val_runs)
            results.append(s)

        if (cartesian_count > best_cartesian_count or
                (cartesian_count == best_cartesian_count and len(results) < best_group_count)):
            best_cartesian_count = cartesian_count
            best_group_count = len(results)
            best_results = results

    # For any remaining non-Cartesian sub-groups, split to individual runs
    final = []
    for s in best_results:
        if s["cartesian"] or s["total"] == 1:
            final.append(s)
        else:
            # Recurse one more level or fall back to individual runs
            group_runs = [r for r in runs if r.id in s["run_ids"]]
            sub_run_dims = [(r, {d: getattr(r, d, None) for d in ALL_DIMENSIONS}) for r in group_runs]
            _, sub_varying = _classify_dims(sub_run_dims)
            if len(sub_varying) > 1:
                final.extend(_split_to_cartesian(group_runs))
            else:
                # Single varying dim but not Cartesian (shouldn't happen),
                # fall back to individual rows
                for r in group_runs:
                    final.append(_make_summary([r]))

    return final


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

    Finds which dimensions are constant across ALL runs, then groups by
    the tuple of values on those constant dimensions.  Runs that share
    the same constant-dimension values end up in one merged group.
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


def _cartesian_repetitions(runs, varying_dims):
    """
    Return integer k >= 1 if the runs form a k-fold complete Cartesian product
    over varying_dims — every combination of values appears exactly k times.
    Return 0 if not.

    With no varying dims, returns len(runs) (k-fold "cross product" of 1 cell).
    """
    if not varying_dims:
        return len(runs)

    cells = Counter(
        tuple(getattr(r, d, None) for d in varying_dims) for r in runs
    )
    counts = set(cells.values())
    if len(counts) != 1:
        return 0
    k = counts.pop()

    dim_sizes = [len(set(getattr(r, d, None) for r in runs)) for d in varying_dims]
    expected_cells = reduce(mul, dim_sizes, 1)
    if len(cells) != expected_cells:
        return 0
    return k


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

    repetitions = _cartesian_repetitions(runs, varying_dims)
    cartesian = repetitions > 0

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
        "repetitions": repetitions,
        "uniform": uniform,
        "uniform_status": uniform_status,
        "breakdown": dict(breakdown),
        "run_ids": run_ids,
    }
