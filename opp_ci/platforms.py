"""
Platform hierarchy registry for the test matrix axes.

opp_ci tests run against a three-level platform hierarchy:

    os ⊃ distro ⊃ flavor

* ``os`` is one of ``Linux``, ``Windows``, ``MacOS``.
* ``distro`` is meaningful only when ``os == "Linux"`` — ``Ubuntu``,
  ``Fedora``, ``Debian``, ``Arch``, ``RHEL``, ...
* ``flavor`` is a variant of a distro — ``Kubuntu``, ``Xubuntu``,
  ``Lubuntu``, ... — and shares its parent's package base.

Names are stored case-folded internally (``"ubuntu"``, not ``"Ubuntu"``)
so registry lookups are unambiguous. ``display_name()`` title-cases for
output; ``platform_slug()`` lower-cases for use in podman image tags
and worker capability tags.
"""

OS_NAMES = ("Linux", "Windows", "MacOS")

DISTROS = {
    "ubuntu":  {"os": "Linux"},
    "fedora":  {"os": "Linux"},
    "debian":  {"os": "Linux"},
    "arch":    {"os": "Linux"},
    "rhel":    {"os": "Linux"},
}

FLAVORS = {
    "kubuntu":  {"distro": "ubuntu"},
    "xubuntu":  {"distro": "ubuntu"},
    "lubuntu":  {"distro": "ubuntu"},
}


def _fold(name):
    return name.strip().lower() if isinstance(name, str) and name.strip() else None


def _os_canonical(name):
    """Return the canonical capitalisation of an OS name, or None."""
    if not name:
        return None
    folded = name.strip().lower()
    for canon in OS_NAMES:
        if canon.lower() == folded:
            return canon
    return name.strip()


def is_linux_distro(name):
    return _fold(name) in DISTROS


def is_known_flavor(name):
    return _fold(name) in FLAVORS


def os_for_distro(distro):
    entry = DISTROS.get(_fold(distro))
    return entry["os"] if entry else None


def distro_for_flavor(flavor):
    entry = FLAVORS.get(_fold(flavor))
    return entry["distro"] if entry else None


def resolve_platform(os=None, distro=None, flavor=None):
    """Fill in implied parents from the registry and validate consistency.

    Returns a ``(os, distro, flavor)`` triple with all three case-folded.
    None entries mean "unspecified at that level."

    Raises ``ValueError`` when explicit parents contradict the registry —
    e.g. ``os="Windows"`` with ``distro="Ubuntu"`` — or when a flavor is
    given without a parent and the registry doesn't know it.
    """
    os_folded = _fold(os)
    distro_folded = _fold(distro)
    flavor_folded = _fold(flavor)

    # OS must be one of the known three when supplied.
    if os_folded and os_folded not in (n.lower() for n in OS_NAMES):
        raise ValueError(
            f"os must be one of {', '.join(OS_NAMES)}, got {os!r}"
        )

    # Flavor implies a distro.
    if flavor_folded:
        implied_distro = distro_for_flavor(flavor_folded)
        if implied_distro is None and distro_folded is None:
            raise ValueError(
                f"flavor {flavor!r} is not in the registry; specify --distro explicitly"
            )
        if implied_distro and distro_folded and implied_distro != distro_folded:
            raise ValueError(
                f"flavor {flavor!r} belongs to distro {implied_distro!r}, "
                f"not {distro!r}"
            )
        distro_folded = distro_folded or implied_distro

    # Distro implies an OS (always Linux for known distros).
    if distro_folded:
        implied_os = os_for_distro(distro_folded)
        if implied_os is None and os_folded is None:
            # Unknown distro, no explicit OS — default to Linux (warn at caller).
            os_folded = "linux"
        elif implied_os and os_folded and implied_os.lower() != os_folded:
            raise ValueError(
                f"distro {distro!r} belongs to os {implied_os!r}, not {os!r}"
            )
        else:
            os_folded = os_folded or (implied_os.lower() if implied_os else None)

    # Cross-OS sanity: distro/flavor are Linux-only.
    if os_folded and os_folded != "linux" and (distro_folded or flavor_folded):
        raise ValueError(
            f"distro/flavor are only valid when os=Linux, got os={os!r}"
        )

    return (os_folded, distro_folded, flavor_folded)


def display_name(name):
    """Title-case a registry name for human display (``ubuntu`` → ``Ubuntu``).

    Returns the input unchanged when it is None or not a string.
    """
    if not isinstance(name, str) or not name:
        return name
    # MacOS and similar mixed-case OS names keep their canonical form.
    canon = _os_canonical(name)
    if canon in OS_NAMES:
        return canon
    return name[:1].upper() + name[1:]


def platform_slug(os=None, distro=None, flavor=None,
                  os_version=None, distro_version=None, flavor_version=None):
    """Return a lower-case ``name-version`` slug for the most specific level.

    Used for podman image tags and worker capability tag values:

        ("Linux", "Ubuntu", None, None, "24.04", None) → "ubuntu-24.04"
        ("Linux", "Ubuntu", "Kubuntu", None, "24.04", None) → "kubuntu-24.04"
        ("Windows", None, None, "11", None, None) → "windows-11"
        ("Linux", None, None, None, None, None) → "linux"

    The flavor version falls back to ``distro_version`` when unspecified,
    matching the rule documented in
    [test_matrix_dimensions.md](../doc/test_matrix_dimensions.md).
    """
    if flavor:
        ver = flavor_version or distro_version
        return f"{_fold(flavor)}-{ver}" if ver else _fold(flavor)
    if distro:
        return f"{_fold(distro)}-{distro_version}" if distro_version else _fold(distro)
    if os:
        return f"{_fold(os)}-{os_version}" if os_version else _fold(os)
    return None


def build_platform_desc(os=None, distro=None, flavor=None,
                        os_version=None, distro_version=None, flavor_version=None,
                        arch=None, compiler=None, compiler_version=None):
    """Human-readable platform string for the UI and the platform_desc column.

    The topmost named level carries the version. Flavors get a ``(Distro,
    arch)`` parenthetical so an "Ubuntu" results table reader notices that
    Kubuntu rows aren't plain Ubuntu.
    """
    parts = []
    if flavor:
        ver = flavor_version or distro_version
        name = display_name(flavor) + (f" {ver}" if ver else "")
        bracket = []
        if distro:
            bracket.append(display_name(distro))
        if arch:
            bracket.append(arch)
        if bracket:
            name = f"{name} ({', '.join(bracket)})"
        parts.append(name)
    elif distro:
        name = display_name(distro) + (f" {distro_version}" if distro_version else "")
        if arch:
            name = f"{name} ({arch})"
        parts.append(name)
    elif os:
        canon = _os_canonical(os) or os
        name = f"{canon} {os_version}" if os_version else canon
        if arch:
            name = f"{name} ({arch})"
        parts.append(name)
    elif arch:
        parts.append(arch)

    if compiler:
        parts.append(f"{compiler}-{compiler_version}" if compiler_version else compiler)
    return " / ".join(parts) if parts else None
