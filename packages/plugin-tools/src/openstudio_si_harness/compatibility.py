"""One npm-compatible range implementation for the public plugin contract."""

import re

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from semantic_version import Version

# Deliberately restrict input to full-version comparator conjunctions. This is
# also accepted by node-semver, including its default prerelease exclusion.
TOKEN = re.compile(
    r"(?:>=|<=|>|<|=)?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)


def semver(version: str) -> Version:
    return Version(version)


def parse_range(expression: str) -> list[tuple[str, Version]]:
    tokens = expression.split()
    if not tokens or any(not TOKEN.fullmatch(token) for token in tokens):
        raise ValueError(
            f"Use space-separated npm SemVer comparators with full versions: {expression}"
        )
    result = []
    for token in tokens:
        match = re.fullmatch(r"(>=|<=|>|<|=)?(.+)", token)
        operator, version = match.groups()
        semver(version)  # Validate build metadata before removing it for precedence.
        result.append((operator or "=", semver(version.partition("+")[0])))
    return result


def satisfies(version: str, expression: str) -> bool:
    semver(version)
    actual = semver(version.partition("+")[0])
    comparators = parse_range(expression)
    release = (actual.major, actual.minor, actual.patch)
    # npm admits a prerelease only when a comparator explicitly mentions a
    # prerelease of that same major/minor/patch tuple, then applies every bound.
    if actual.prerelease and not any(
        expected.prerelease
        and (expected.major, expected.minor, expected.patch) == release
        for _, expected in comparators
    ):
        return False
    return all(
        {
            ">=": actual >= expected,
            "<=": actual <= expected,
            ">": actual > expected,
            "<": actual < expected,
            "=": actual == expected,
        }[operator]
        for operator, expected in comparators
    )


def validate_range(kind: str, expression: str) -> None:
    if kind == "python-distribution":
        try:
            SpecifierSet(expression)
        except InvalidSpecifier as exc:
            raise ValueError(
                f"Invalid Python dependency requirement: {expression}"
            ) from exc
    else:
        parse_range(expression)
