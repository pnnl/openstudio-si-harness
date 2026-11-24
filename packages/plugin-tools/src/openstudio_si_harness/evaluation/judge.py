"""Map bounded SI evidence to DeepEval and preserve native metric results."""

import importlib.util
import json
import math
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from fnmatch import fnmatch
from pathlib import Path

from .contracts import (
    check_endpoint,
    digest,
    inside,
    json_hash,
    validate,
    validate_metrics,
)
from .environment import NETWORK_ENV_NAMES
from .hosts import process

MAPPING_VERSION = "si-deepeval-mapping/0.2"


def verify_evidence(session, result):
    if json_hash(result["case"]) != result["case_hash"]:
        raise ValueError("Captured case hash changed.")
    for relative, expected in result["evidence_hashes"].items():
        path = inside(session, relative)
        if digest(path.read_bytes()) != expected:
            raise ValueError("Evidence changed since capture: " + relative)


def evidence_bundle(session, result, limit):
    verify_evidence(session, result)
    candidates = [{"id": "answer", "content": (session / "answer.txt").read_text()}]
    reference = session / "reference.txt"
    if reference.exists():
        candidates.append({"id": "reference", "content": reference.read_text()})
    candidates += json.loads((session / "trace.json").read_text())
    for path in sorted((session / "artifacts").glob("**/*")):
        if path.is_file():
            identity = "artifact:" + path.relative_to(session / "artifacts").as_posix()
            if path.suffix.lower() in {
                ".json",
                ".jsonl",
                ".txt",
                ".md",
                ".csv",
                ".osm",
                ".idf",
                ".err",
                ".eio",
                ".eso",
                ".xml",
                ".html",
                ".yaml",
                ".yml",
            }:
                content = path.read_text(errors="replace")
            else:
                content = {
                    "sha256": digest(path.read_bytes()),
                    "bytes": path.stat().st_size,
                    "extraction": "unsupported binary; contents not supplied",
                }
            candidates.append({"id": identity, "content": content})
    accepted = []
    gaps = []
    used = 0
    for item in candidates:
        text = json.dumps(item, ensure_ascii=False)
        if used + len(text) > limit:
            gaps.append("evidence_budget_omitted:" + item["id"])
            continue
        accepted.append(item)
        used += len(text)
    return accepted, gaps


def preflight(settings):
    validate("evaluation-judge-config", settings)
    check_endpoint(settings)
    if importlib.util.find_spec("deepeval") is None:
        raise ValueError(
            'Install evaluation support with pip install "openstudio-si[eval]"'
        )
    name = settings.get("api_key_env")
    if name and not os.environ.get(name):
        raise ValueError("Judge credential environment variable is missing: " + name)


def worker_stdio():
    """Private worker protocol: credentials arrive only via the child environment."""
    value = json.loads(sys.stdin.read())
    try:
        with redirect_stdout(sys.stderr):
            from .deepeval_backend import evaluate

            response = evaluate(
                value["settings"],
                value["request"],
                os.environ.pop("OPENSTUDIO_SI_EVAL_WORKER_KEY", None),
            )
        payload = json.dumps({"result": response}, allow_nan=False)
        if len(payload.encode()) > 1_000_000:
            raise ValueError("Judge adapter result exceeds byte limit.")
    except Exception as exc:  # noqa: BLE001
        payload = json.dumps({"error": str(exc)[:10000]})
    print(payload)


def invoke(settings, request, key, timeout):
    # Bootstrap the same tool package used by the controller, including editable installs.
    # No evaluation inputs are executable code or import paths.
    package_root = str(Path(__file__).resolve().parents[2])
    bootstrap = (
        "import sys;sys.path.insert(0,"
        + repr(package_root)
        + ");from openstudio_si_harness.evaluation.judge import worker_stdio;worker_stdio()"
    )
    # Pass only explicit credentials, never unrelated shell secrets or DeepEval globals.
    environment = {
        name: os.environ[name]
        for name in (
            "PATH",
            "LANG",
            "LC_ALL",
            "SYSTEMROOT",
            *NETWORK_ENV_NAMES,
        )
        if name in os.environ
    }
    environment.update(
        {
            "DEEPEVAL_TELEMETRY_OPT_OUT": "YES",
            "DEEPEVAL_DISABLE_DOTENV": "1",
            "DEEPEVAL_FILE_SYSTEM": "READ_ONLY",
        }
    )
    if key:
        environment["OPENSTUDIO_SI_EVAL_WORKER_KEY"] = key
    payload = json.dumps({"settings": settings, "request": request})
    with tempfile.TemporaryDirectory(prefix="si-judge-") as cwd:
        environment.update({"HOME": cwd, "XDG_CONFIG_HOME": cwd, "XDG_CACHE_HOME": cwd})
        captured = process(
            [sys.executable, "-I", "-c", bootstrap],
            cwd,
            environment,
            timeout,
            1_000_000,
            payload,
        )
    if "host_interrupted" in captured["gaps"]:
        raise KeyboardInterrupt
    if "host_timeout" in captured["gaps"]:
        raise ValueError("judge_timeout")
    if captured["gaps"] or captured["returncode"]:
        raise ValueError("Judge worker failed: " + captured["stderr"][-2000:])
    value = json.loads(captured["stdout"])
    if "error" in value:
        raise ValueError(value["error"])
    return value["result"]


def judge_session(session, result, settings, limits, redact=lambda value: value):
    started = time.monotonic()
    case = result["case"]
    rubric = case.get("judge")
    if not rubric or not settings:
        return {"status": "not_configured", "score_policy": "advisory"}
    if any(
        c.get("check") == "holdout_access" and c.get("passed") is False
        for c in result["checks"]
    ):
        return {
            "status": "skipped",
            "score_policy": "advisory",
            "reason": "holdout_access_violation",
        }
    if result["status"] == "incomplete" or result.get("holdout_pending"):
        return {
            "status": "incomplete",
            "score_policy": "advisory",
            "error": "agent_capture_incomplete",
        }
    validate("evaluation-judge-config", settings)
    validate_metrics(case)
    check_endpoint(settings)
    key = os.environ.get(settings.get("api_key_env", ""))
    if settings.get("api_key_env") and not key:
        return {
            "status": "incomplete",
            "score_policy": "advisory",
            "error": "Judge credential environment variable is missing: "
            + settings["api_key_env"],
        }
    evidence, gaps = evidence_bundle(
        session, result, limits.get("max_evidence_chars", 100000)
    )
    parameters = {
        name for metric in rubric["metrics"] for name in metric["evaluation_params"]
    }
    notes = []
    relevant = []
    for gap in gaps:
        identity = gap.removeprefix("evidence_budget_omitted:")
        needed = (
            (identity == "answer" and "actual_output" in parameters)
            or (identity == "reference" and "expected_output" in parameters)
            or (
                identity.startswith("trace:")
                and bool({"context", "tools_called"} & parameters)
            )
            or (identity.startswith("artifact:") and "context" in parameters)
        )
        (relevant if needed else notes).append(gap)
    gaps = relevant
    by_id = {item["id"]: item for item in evidence}
    artifacts = {
        "artifact:" + path.relative_to(session / "artifacts").as_posix()
        for path in (session / "artifacts").rglob("*")
        if path.is_file()
    }
    for item in evidence:
        if isinstance(item.get("content"), dict) and "extraction" in item["content"]:
            notes.append("artifact_extraction_unavailable:" + item["id"])
    for metric in rubric["metrics"]:
        for pattern in metric.get("required_artifacts", []):
            matches = [
                name
                for name in artifacts
                if fnmatch(name.removeprefix("artifact:"), pattern)
            ]
            if not matches or any(
                name not in by_id or isinstance(by_id[name]["content"], dict)
                for name in matches
            ):
                gaps.append(
                    "required_judge_artifact_unavailable:"
                    + metric["id"]
                    + ":"
                    + pattern
                )

    trace = [item for item in evidence if item["id"].startswith("trace:")]
    required = case.get("required_tools", [])
    expected = case.get("expected", {})
    test_case = {
        "input": case["prompt"],
        "actual_output": by_id.get("answer", {}).get("content", ""),
        "context": [
            *case.get("context", []),
            *[
                json.dumps(item)
                for item in evidence
                if item["id"] not in {"answer", "reference"}
            ],
        ],
        "tools_called": [
            {
                "name": (item.get("provider", "") + ":" if item.get("provider") else "")
                + item["name"],
                "input_parameters": (
                    item.get("arguments")
                    if isinstance(item.get("arguments"), dict)
                    else {"value": item.get("arguments")}
                ),
                "output": item.get("result"),
            }
            for item in trace
        ],
        "expected_tools": [
            {
                "name": (item.get("provider", "") + ":" if item.get("provider") else "")
                + item["tool"]
            }
            for item in required
        ],
    }
    reference = by_id.get("reference", {}).get("content")
    if expected.get("output") or reference:
        test_case["expected_output"] = "\n\n".join(
            part for part in [expected.get("output"), reference] if part
        )
    if case.get("retrieval_context"):
        test_case["retrieval_context"] = case["retrieval_context"]
    request = {
        "test_case": test_case,
        "metrics": [
            {
                "threshold": settings.get("geval", {}).get(
                    "threshold", rubric["threshold"]
                ),
                **item,
            }
            for item in rubric["metrics"]
        ],
    }
    record = {
        "score_policy": "advisory",
        "provider": settings["provider"],
        "model": settings["model"],
        "settings": settings,
        "mapping_version": MAPPING_VERSION,
        "framework": "deepeval",
        "request": request,
        "evidence_gaps": gaps,
        "notes": notes,
    }
    if gaps:
        return {
            **record,
            "status": "incomplete",
            "error": "required_judge_evidence_unavailable",
        }
    try:
        if len(json.dumps(request)) > 2_000_000:
            raise ValueError("Judge request exceeds byte limit.")
        response = invoke(
            settings,
            request,
            key,
            max(
                0.001,
                limits.get("judge_timeout_seconds", 120) - (time.monotonic() - started),
            ),
        )
        response = json.loads(redact(json.dumps(response, allow_nan=False)))
        metrics = response["metrics"]
        wanted = {item["id"] for item in rubric["metrics"]}
        observed = [item["id"] for item in metrics]
        if len(observed) != len(wanted) or set(observed) != wanted:
            raise ValueError("DeepEval must return exactly the configured metric IDs.")
        for item in metrics:
            score = item["score"]
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
                or not 0 <= score <= 1
            ):
                raise ValueError("DeepEval returned an invalid metric score.")
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                raise ValueError("DeepEval returned no metric reason.")
        weights = {item["id"]: item.get("weight", 1) for item in rubric["metrics"]}
        score = sum(weights[item["id"]] * item["score"] for item in metrics) / sum(
            weights.values()
        )
        return {
            **record,
            "status": "incomplete" if gaps else "scored",
            "framework_version": response.get("framework_version"),
            "metrics": metrics,
            "evaluation_model": response.get("evaluation_model"),
            "evidence_policy": response.get("evidence_policy"),
            "score": score,
            "threshold": rubric["threshold"],
            "above_threshold": score >= rubric["threshold"],
        }
    except Exception as exc:  # noqa: BLE001
        return {**record, "status": "incomplete", "error": redact(str(exc))}
