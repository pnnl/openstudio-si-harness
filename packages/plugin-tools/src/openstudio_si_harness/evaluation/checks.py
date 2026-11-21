"""Observable checks adapted from pnnl/openstudio-si-harness PR #35 (BSD-3-Clause)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    name: str  # bare MCP tool name, server prefix stripped
    provider: (
        str  # "harness", "agent" (host built-ins like Skill), or the MCP server name
    )
    arguments: Any = None


@dataclass(frozen=True)
class CheckResult:
    check: str
    passed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_required(
    calls: list[ToolCall], required: Iterable[Mapping[str, str]]
) -> list[CheckResult]:
    """Each ``{"tool": name, "provider": p}`` must appear at least once."""
    results = []
    for spec in required:
        name, provider = spec["tool"], spec.get("provider")
        hits = [
            c
            for c in calls
            if c.name == name and (provider is None or c.provider == provider)
        ]
        label = f"required:{name}" + (f"@{provider}" if provider else "")
        results.append(
            CheckResult(
                label,
                bool(hits),
                f"called {len(hits)} time(s)" if hits else "never called",
            )
        )
    return results


def check_forbidden(
    calls: list[ToolCall], patterns: Iterable[str]
) -> list[CheckResult]:
    """No call may match a forbidden glob (e.g. ``sim_*``)."""
    results = []
    for pattern in patterns:
        hits = sorted({c.name for c in calls if fnmatch(c.name, pattern)})
        results.append(
            CheckResult(
                f"forbidden:{pattern}",
                not hits,
                f"called {', '.join(hits)}" if hits else "not called",
            )
        )
    return results


def loaded_skills(calls: list[ToolCall]) -> list[str]:
    """Skill names loaded through the host ``Skill`` tool, preserving prefixes."""
    names = []
    for c in calls:
        if (
            c.name == "Skill"
            and c.provider == "agent"
            and isinstance(c.arguments, Mapping)
        ):
            skill = c.arguments.get("skill")
            if isinstance(skill, str):
                names.append(skill)
    return names


def check_skills(
    calls: list[ToolCall], required: Iterable[str] = (), forbidden: Iterable[str] = ()
) -> list[CheckResult]:
    """Each ``required`` skill must be loaded; no loaded skill may match a ``forbidden`` glob."""
    skills = loaded_skills(calls)
    results = []
    for name in required:
        matches = [
            s
            for s in skills
            if s == name or (":" not in name and s.rpartition(":")[2] == name)
        ]
        n = len(matches) if ":" in name or len(set(matches)) <= 1 else 0
        results.append(
            CheckResult(
                f"required_skill:{name}",
                bool(n),
                f"loaded {n} time(s)" if n else "never loaded",
            )
        )
    for pattern in forbidden:
        hits = sorted(
            {
                s
                for s in skills
                if fnmatch(s if ":" in pattern else s.rpartition(":")[2], pattern)
            }
        )
        results.append(
            CheckResult(
                f"forbidden_skill:{pattern}",
                not hits,
                f"loaded {', '.join(hits)}" if hits else "not loaded",
            )
        )
    return results


def score_case(spec: Mapping[str, Any], calls: list[ToolCall]) -> list[CheckResult]:
    return [
        *check_required(calls, spec.get("required_tools", [])),
        *check_forbidden(calls, spec.get("forbidden_tools", [])),
        *check_skills(
            calls, spec.get("required_skills", []), spec.get("forbidden_skills", [])
        ),
    ]


def _numbers(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in re.findall(r"-?\d[\d,]*\.?\d*", text)]


def check_expected(
    expected: Mapping[str, Any] | None, answer: str, files: Iterable[str] = ()
) -> list[CheckResult]:
    """Outcome golden: ``{"metrics": [{"name", "value", "tolerance_pct", "unit_hint"?}], "artifacts": [...]}``.

    A metric passes when a number within ``tolerance_pct`` of ``value`` appears
    on the same line as ``name`` (case-insensitive) in the final answer.
    Artifacts are file-name globs that must match one of ``files``.
    """
    if not expected:
        return []
    results = []
    for metric in expected.get("metrics", []):
        name, target, tol = (
            metric["name"],
            float(metric["value"]),
            float(metric.get("tolerance_pct", 5)),
        )
        found = [
            n
            for line in answer.splitlines()
            if name.lower() in line.lower()
            for n in _numbers(line)
        ]
        close = [n for n in found if abs(n - target) <= abs(target) * tol / 100]
        reason = (
            f"{close[0]} within {tol}% of {target}"
            if close
            else f"no value within {tol}% of {target} (saw {found[:5] or 'none'})"
        )
        results.append(CheckResult(f"expected:{name}", bool(close), reason))
    names = list(files)
    for pattern in expected.get("artifacts", []):
        hits = [f for f in names if fnmatch(f, pattern)]
        results.append(
            CheckResult(f"artifact:{pattern}", bool(hits), f"{len(hits)} match(es)")
        )
    return results


@dataclass(frozen=True)
class OsmObject:
    type: str  # e.g. "OS:ThermalZone"
    fields: tuple[
        tuple[str, str], ...
    ]  # (label, value); label is the "!- Label" comment, units stripped

    def get(self, label: str) -> str | None:
        return next((v for k, v in self.fields if k.lower() == label.lower()), None)


def parse_osm(text: str) -> list[OsmObject]:
    """Parse OpenStudio ``.osm`` text (IDF syntax) into objects; no OpenStudio install needed.

    Objects end with ``;``, fields are comma separated, and each field's label is
    its trailing ``!- Label {unit}`` comment. Values never contain commas.
    """
    objects: list[OsmObject] = []
    current: list[tuple[str, str]] = (
        []
    )  # (value, label) per field line of the open object
    for line in text.splitlines():
        body, _, comment = line.partition("!-")
        if not body.strip():
            continue
        label = re.sub(r"\{[^}]*\}", "", comment).strip()
        # A line normally holds one field, but tolerate several (e.g. "a, b;").
        parts = re.split(r"([,;])", body)
        pieces = [(parts[i].strip(), parts[i + 1]) for i in range(0, len(parts) - 1, 2)]
        for value, sep in pieces:
            current.append((value, label))
            if sep == ";":
                type_name, rest = current[0][0], current[1:]
                objects.append(
                    OsmObject(type_name, tuple((lbl, val) for val, lbl in rest))
                )
                current = []
    return objects


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def check_model(
    spec: Mapping[str, Any] | None, osm_path: Path | None
) -> list[CheckResult]:
    """Deterministic checks on a saved ``.osm``.

    ``spec`` keys (all optional): ``counts`` ``{type: n}`` (exact), ``min_counts``
    ``{type: n}``, ``version_min`` (``"3.0.0"``), and ``fields`` -- a list of
    ``{"type", "field", "equals"|"contains", "match": "any"|"all"}`` (default ``any``)
    comparing a labeled field (case-insensitive) on objects of that type.
    """
    if not spec:
        return []
    if osm_path is None:
        return [
            CheckResult(
                "model:found", False, "no saved .osm in the session sandbox matched"
            )
        ]
    objects = parse_osm(osm_path.read_text(encoding="utf-8", errors="ignore"))
    by_type: dict[str, list[OsmObject]] = {}
    for obj in objects:
        by_type.setdefault(obj.type, []).append(obj)
    results = [CheckResult("model:found", True, osm_path.name)]

    for type_name, want in spec.get("counts", {}).items():
        got = len(by_type.get(type_name, []))
        results.append(
            CheckResult(f"model:count:{type_name}", got == want, f"{got} (want {want})")
        )
    for type_name, want in spec.get("min_counts", {}).items():
        got = len(by_type.get(type_name, []))
        results.append(
            CheckResult(
                f"model:min_count:{type_name}", got >= want, f"{got} (want >= {want})"
            )
        )
    if "version_min" in spec:
        version = next(
            (o.get("Version Identifier") for o in by_type.get("OS:Version", [])), None
        )
        ok = version is not None and _version_tuple(version) >= _version_tuple(
            spec["version_min"]
        )
        results.append(
            CheckResult(
                "model:version_min", ok, f"{version} (want >= {spec['version_min']})"
            )
        )
    for item in spec.get("fields", []):
        type_name, label, mode = item["type"], item["field"], item.get("match", "any")
        values = [
            v
            for v in (o.get(label) for o in by_type.get(type_name, []))
            if v is not None
        ]
        if "equals" in item:
            test, desc = (
                lambda v, wanted=item["equals"]: v.lower() == str(wanted).lower()
            ), f"== {item['equals']!r}"
        else:
            test, desc = (
                lambda v, wanted=item["contains"]: str(wanted).lower() in v.lower()
            ), f"contains {item['contains']!r}"
        hits = [v for v in values if test(v)]
        ok = bool(values) and (
            len(hits) == len(by_type.get(type_name, []))
            if mode == "all"
            else bool(hits)
        )
        results.append(
            CheckResult(
                f"model:field:{type_name}.{label} {desc} ({mode})",
                ok,
                f"{len(hits)}/{len(values)} match; saw {values[:3] or 'no objects'}",
            )
        )
    return results
