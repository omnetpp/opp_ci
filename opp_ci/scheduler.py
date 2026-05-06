"""
Matrix expansion and job scheduling.

A TestMatrix config is a JSON dict with axes to cross-product:
{
    "test_types": ["smoke", "fingerprint"],
    "modes": ["release", "debug"],
    "platforms": ["Ubuntu 24.04 / gcc-14", "Ubuntu 24.04 / clang-18"],
    "versions": ["inet-4.5", "inet-4.4"],   # optional: override project versions
    "features": []                           # optional: INET feature flags
}

The scheduler expands this into individual jobs (one per combination).
"""

import itertools
import logging

_logger = logging.getLogger(__name__)


def expand_matrix(project, config):
    """
    Expand a matrix config into a list of individual job specs.

    Each job spec is a dict:
        {
            "project": "inet-4.5",
            "test_type": "smoke",
            "mode": "release",
            "platform_desc": "Ubuntu 24.04 / gcc-14",
        }
    """
    test_types = config.get("test_types", ["smoke"])
    modes = config.get("modes", ["release"])
    platforms = config.get("platforms", [None])
    versions = config.get("versions", [project])

    jobs = []
    for version, test_type, mode, platform in itertools.product(versions, test_types, modes, platforms):
        jobs.append({
            "project": version,
            "test_type": test_type,
            "mode": mode,
            "platform_desc": platform,
        })

    _logger.info("Expanded matrix for %s: %d jobs", project, len(jobs))
    return jobs


DEFAULT_MATRICES = {
    "inet-default": {
        "project": "inet",
        "config": {
            "test_types": ["smoke", "fingerprint", "statistical"],
            "modes": ["release", "debug"],
            "platforms": [None],
            "versions": ["inet"],
        },
    },
    "omnetpp-default": {
        "project": "omnetpp",
        "config": {
            "test_types": ["smoke", "build"],
            "modes": ["release", "debug"],
            "platforms": [None],
            "versions": ["omnetpp"],
        },
    },
}
