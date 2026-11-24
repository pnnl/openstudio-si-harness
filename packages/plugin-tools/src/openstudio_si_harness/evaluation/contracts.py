"""Draft contracts and source provenance; no host or model calls."""

import hashlib
import json
import re
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse

import yaml
from jsonschema import Draft202012Validator


def validate(name, value):
    json.dumps(value, allow_nan=False)
    schema = json.loads(
        files("openstudio_si_harness")
        .joinpath(f"schemas/{name}.schema.json")
        .read_text()
    )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value), key=lambda e: str(e.path)
    )
    if errors:
        raise ValueError(f"{name}: {list(errors[0].path)}: {errors[0].message}")
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_hash(value):
    return digest(json.dumps(value, sort_keys=True, allow_nan=False).encode())


def inside(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError(f"Expected a relative evidence path: {relative}")
    root = root.resolve()
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"Evidence symlink is not allowed: {relative}")
    if (
        not candidate.resolve().is_relative_to(root.resolve())
        or not candidate.is_file()
    ):
        raise ValueError(f"Missing or escaping evidence: {relative}")
    return candidate


def load_cases(directory, plugin_root):
    records = []
    seen = set()
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text())
        for case in data if isinstance(data, list) else [data]:
            legacy = "schema_version" not in case
            case = {"schema_version": "0.1", "inputs": [], **case}
            validate("evaluation-case", case)
            if case["case_id"] in seen:
                raise ValueError(f'Duplicate case_id: {case["case_id"]}')
            seen.add(case["case_id"])
            if case.get("judge"):
                ids = [c["id"] for c in case["judge"]["metrics"]]
                if len(ids) != len(set(ids)):
                    raise ValueError("Duplicate G-Eval metric id.")
                validate_metrics(case)
            for relative in [
                *case["inputs"],
                case.get("expected", {}).get("reference"),
            ]:
                if relative:
                    inside(plugin_root, relative)
            reference = case.get("expected", {}).get("reference")
            if reference and reference in case["inputs"]:
                raise ValueError("Reviewed reference must not also be an agent input.")
            records.append({"spec": case, "legacy": legacy, "hash": json_hash(case)})
    if not records:
        raise ValueError("No evaluation cases found.")
    return records


def validate_metrics(case):
    """Reject unresolvable DeepEval fields and ambiguous score bands before calls."""
    expected = case.get("expected", {})
    available = {"input", "actual_output", "context", "tools_called", "expected_tools"}
    if expected.get("output") or expected.get("reference"):
        available.add("expected_output")
    if case.get("retrieval_context"):
        available.add("retrieval_context")
    for metric in case.get("judge", {}).get("metrics", []):
        if (
            metric.get("required_artifacts")
            and "context" not in metric["evaluation_params"]
        ):
            raise ValueError(
                "G-Eval required_artifacts needs context in evaluation_params."
            )
        missing = set(metric["evaluation_params"]) - available
        if missing:
            raise ValueError(
                "G-Eval parameters have no case evidence: " + ", ".join(sorted(missing))
            )
        used = set()
        for band in metric.get("rubric", []):
            start, end = band["score_range"]
            values = set(range(start, end + 1))
            if start > end or used & values:
                raise ValueError(
                    "G-Eval rubric score ranges must be ordered and non-overlapping."
                )
            used |= values


def load_config(path):
    value = validate("evaluation-run", yaml.safe_load(path.read_text()))
    if value["host"] == "codex" and "max_cost_usd" in value.get("limits", {}):
        raise ValueError(
            "Codex cannot enforce max_cost_usd; use request/session/time limits."
        )
    if value["host"] != "codex" and "sandbox" in value["agent"]:
        raise ValueError(
            "agent.sandbox applies only to Codex; use allowed_tools for Claude."
        )
    for settings in [value["agent"], value.get("judge", {})]:
        check_endpoint(settings)
    return value


def check_endpoint(settings):
    if "base_url" in settings:
        url = urlparse(settings["base_url"])
        if (
            url.username
            or url.password
            or url.query
            or url.fragment
            or not url.hostname
        ):
            raise ValueError(
                "Model base_url must not contain credentials, query or fragment."
            )
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError(
                "Model endpoints require HTTPS (HTTP allowed only on loopback)."
            )


def safe_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", value):
        raise ValueError("Unsafe case or arm identifier.")
    return value
