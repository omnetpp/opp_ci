"""
Roll-up summary logic for test results.

Groups results hierarchically along a static dimension order and collapses
groups where all results share the same status into a single summary line.

The dimension order is static for now:
    project → test_type → status

Later this should be computed dynamically based on which dimensions have
variation in the result set.
"""

from collections import OrderedDict

# Static roll-up dimension order.  Later: compute from result variance.
ROLLUP_DIMENSIONS = ["project", "test_type"]


def rollup_runs(runs):
    """
    Given a list of TestRun objects, produce a hierarchical roll-up summary.

    Returns a list of dicts:
        {
            "key": "inet / smoke",
            "project": "inet",
            "test_type": "smoke",
            "total": 5,
            "breakdown": {"passed": 3, "failed": 2},
            "uniform": False,          # True if all results share one status
            "uniform_status": None,    # the common status if uniform
            "run_ids": [1, 2, 3, 4, 5],
        }
    """
    groups = OrderedDict()
    for run in runs:
        key = tuple(getattr(run, dim) for dim in ROLLUP_DIMENSIONS)
        if key not in groups:
            groups[key] = []
        groups[key].append(run)

    summaries = []
    for key, group_runs in groups.items():
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

        key_parts = dict(zip(ROLLUP_DIMENSIONS, key))
        display_key = " / ".join(str(v) for v in key)

        summaries.append({
            "key": display_key,
            "total": total,
            "breakdown": breakdown,
            "uniform": uniform,
            "uniform_status": uniform_status,
            "run_ids": run_ids,
            **key_parts,
        })

    return summaries
