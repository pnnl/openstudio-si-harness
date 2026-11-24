"""Fresh case/arm/repeat workspaces, immutable evidence and explicit run outcomes."""

import json
import os
import re
import shutil
import tempfile
import time
import uuid
from fnmatch import fnmatch
from pathlib import Path

from .. import __version__
from ..compatibility import satisfies
from ..export import DESCRIPTOR, validate_export
from ..manifest import load_plugin
from .checks import ToolCall, check_expected, check_model, score_case
from .contracts import (
    digest,
    inside,
    json_hash,
    load_cases,
    load_config,
    safe_name,
    validate,
)
from .environment import NETWORK_ENV_NAMES
from .hosts import capture, normalize, process
from .isolation import (
    agent_view,
    external_model_paths,
    holdout_access,
    holdout_content,
    verify_source_export,
)
from .judge import judge_session, preflight, verify_evidence

DEFAULTS = {
    "case_timeout_seconds": 600,
    "judge_timeout_seconds": 120,
    "run_timeout_seconds": 7200,
    "max_sessions": 100,
    "max_trace_bytes": 8_000_000,
    "max_artifact_bytes": 25_000_000,
    "max_evidence_chars": 100000,
}


def redactor(names=()):
    names = (
        *names,
        *[name for name in NETWORK_ENV_NAMES if "@" in os.environ.get(name, "")],
    )
    secrets = sorted(
        {
            value
            for name, value in os.environ.items()
            if (name in names and value)
            or (
                len(value) >= 8
                and any(
                    word in name.upper()
                    for word in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")
                )
            )
        },
        key=len,
        reverse=True,
    )

    def redact(text):
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    redact.secrets = secrets
    return redact


def write(path, value, redact=lambda text: text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(json.dumps(value, indent=2, allow_nan=False)) + "\n")


def selected_packages(config, base):
    result = {}
    for relative in config["plugins"]:
        path = (base / relative).resolve()
        if not (path / DESCRIPTOR).is_file():
            raise ValueError(
                "Evaluation currently requires descriptor-verified generic host exports: "
                + str(path)
            )
        package = validate_export(path)
        descriptor = json.loads((path / DESCRIPTOR).read_text())
        if descriptor["host"] != config["host"]:
            raise ValueError("Selected package targets the wrong host.")
        identity = package["manifest"]["id"]
        if identity in result:
            raise ValueError("Duplicate plugin identity: " + identity)
        result[identity] = {
            "path": path,
            "manifest": package["manifest"],
            "descriptor_hash": digest((path / DESCRIPTOR).read_bytes()),
            "files": descriptor["files"],
        }
    return result


def dependency_check(packages):
    for package in packages.values():
        for dependency in package["manifest"]["dependencies"]:
            if dependency["kind"] == "plugin" and dependency["required"]:
                target = packages.get(dependency["target"])
                if not target or (
                    dependency.get("version_spec")
                    and not satisfies(
                        target["manifest"]["version"], dependency["version_spec"]
                    )
                ):
                    raise ValueError(
                        "Selected arm has an unsatisfied plugin dependency: "
                        + dependency["target"]
                    )


def artifact_snapshot(
    workspace, original, session, limit, redact, *, prefix="", model_only=False
):
    artifacts = {}
    gaps = []
    used = 0
    for index, path in enumerate(sorted(workspace.rglob("*"))):
        if index > 10000:
            gaps.append("artifact_file_limit")
            break
        relative = path.relative_to(workspace).as_posix()
        if model_only and (
            path.suffix.lower() not in {".osm", ".sql", ".idf", ".eso", ".err", ".eio"}
            or any(
                part
                in {"plugins", "marketplaces", "cache", "config", ".agents", ".claude"}
                for part in path.relative_to(workspace).parts
            )
        ):
            continue
        if path.is_symlink():
            gaps.append("artifact_symlink:" + relative)
            continue
        if not path.is_file():
            continue
        if any(
            part in {".git", ".codex", ".claude", ".env", "node_modules", "__pycache__"}
            for part in path.relative_to(workspace).parts
        ):
            continue
        if (
            path.name.startswith(".env")
            or path.name
            in {"credentials.json", "auth.json", "id_rsa", ".netrc", ".npmrc"}
            or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
        ):
            gaps.append("credential_file_omitted:" + relative)
            continue
        if path.stat().st_size + used > limit:
            gaps.append("artifact_budget_omitted:" + relative)
            continue
        data = path.read_bytes()
        if digest(data) == original.get(relative):
            continue
        used += len(data)
        # Known environment credentials are removed from any captured file, including binary payloads.
        for secret in redact.secrets:
            data = data.replace(secret.encode(), b"[REDACTED]")
        relative = prefix + relative
        target = session / "artifacts" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        artifacts[relative] = digest(data)
    return artifacts, gaps


def snapshot_outputs(workspace, private, original, session, limit, redact):
    artifacts, gaps = artifact_snapshot(workspace, original, session, limit, redact)
    home = private / "host-home"
    if home.exists():
        used = sum((session / "artifacts" / path).stat().st_size for path in artifacts)
        extra, home_gaps = artifact_snapshot(
            home,
            {},
            session,
            max(0, limit - used),
            redact,
            prefix="_host_home/",
            model_only=True,
        )
        artifacts.update(extra)
        gaps.extend(home_gaps)
    return artifacts, gaps


def split_gaps(values):
    notes = []
    blocking = []
    for value in values:
        if value.startswith(
            (
                "skill_activation_not_observable_in_codex_exec",
                "answer_reported_metrics_are_not_artifact_verified",
                "credential_file_omitted:",
            )
        ):
            notes.append(value)
        else:
            blocking.append(value)
    return blocking, notes


def observe(case, legacy, arm, host, normalized, artifacts, session):
    checks = []
    gaps = []
    keys = (
        "required_tools",
        "forbidden_tools",
        "required_skills",
        "forbidden_skills",
        "expected_tools_ordered",
    )
    routing = (
        {key: case[key] for key in keys if key in case}
        if not legacy or arm == "both"
        else {}
    )
    routing.update(case.get("arm_routing", {}).get(arm, {}))
    calls = [
        ToolCall(row["name"], row["provider"], row["arguments"])
        for row in normalized["trace"]
    ]
    if host == "codex" and (
        routing.get("required_skills") or routing.get("forbidden_skills")
    ):
        gaps.append("skill_activation_not_observable_in_codex_exec")
        for key in ("required_skills", "forbidden_skills"):
            for name in routing.pop(key, []):
                checks.append(
                    {
                        "check": key + ":" + name,
                        "passed": None,
                        "status": "not_observable",
                        "reason": "Codex exec does not expose skill activation.",
                    }
                )
    checks.extend(item.to_dict() for item in score_case(routing, calls))
    ordered = iter([call.name for call in calls])
    sequence = routing.get("expected_tools_ordered", [])
    if sequence:
        ok = all(any(seen == wanted for seen in ordered) for wanted in sequence)
        checks.append(
            {
                "check": "ordered_tools",
                "passed": ok,
                "reason": "Expected subsequence: " + str(sequence),
            }
        )
    checks.extend(
        item.to_dict()
        for item in check_expected(
            case.get("expected"), normalized["answer"], artifacts
        )
    )
    if case.get("expected", {}).get("metrics"):
        gaps.append("answer_reported_metrics_are_not_artifact_verified")
    original_hashes = set(json.loads((session / "inputs.json").read_text()).values())
    model_spec = case.get("expected", {}).get("model", {})
    selector = model_spec.get("path")
    model_paths = [
        session / "artifacts" / name
        for name in artifacts
        if name.endswith(".osm")
        and artifacts[name] not in original_hashes
        and (
            fnmatch(name, selector)
            if selector
            else not (
                {".openstudio_si_mcp_workspace", ".si-data"} & set(Path(name).parts)
            )
        )
    ]
    if case.get("expected", {}).get("model") and len(model_paths) > 1:
        gaps.append("ambiguous_output_model")
        model = None
    else:
        model = model_paths[0] if model_paths else None
    checks.extend(
        item.to_dict()
        for item in check_model(case.get("expected", {}).get("model"), model)
    )
    return checks, gaps


def run(plugin, cases, config_path, output):
    source = plugin.resolve()
    source_info = load_plugin(source)
    output = output.absolute()
    config = load_config(config_path)
    redact = redactor(
        [config["agent"].get("api_key_env"), config.get("judge", {}).get("api_key_env")]
    )
    limits = {**DEFAULTS, **config.get("limits", {})}
    agent_key = config["agent"].get(
        "api_key_env",
        "ANTHROPIC_API_KEY" if config["host"] == "claude" else "OPENAI_API_KEY",
    )
    if not os.environ.get(agent_key):
        raise ValueError(
            "Agent credential environment variable is missing: " + agent_key
        )
    if config.get("judge"):
        preflight(config["judge"])
    records = load_cases(cases.resolve(), source)
    packages = selected_packages(config, config_path.resolve().parent)
    verify_source_export(source, source_info, packages, config["host"])
    withheld_files = {
        path.resolve().relative_to(source).as_posix()
        for path in cases.resolve().glob("*.json")
        if path.resolve().is_relative_to(source)
    }
    withheld_files.update(
        record["spec"].get("expected", {}).get("reference")
        for record in records
        if record["spec"].get("expected", {}).get("reference")
    )
    holdouts = {}
    holdout_hashes = []
    holdout_bytes = 0
    for record in records:
        ref = record["spec"].get("expected", {}).get("reference")
        if ref and ref not in holdouts:
            path = inside(source, ref)
            holdout_bytes += path.stat().st_size
            if holdout_bytes > limits["max_artifact_bytes"]:
                raise ValueError(
                    "Reviewed references exceed the run holdout byte limit."
                )
            raw = path.read_bytes()
            holdout_hashes.append(digest(raw))
            holdouts[ref] = redact(raw.decode())
    default_arm = "both" if any(record["legacy"] for record in records) else "default"
    arms = config.get("arms", {default_arm: {"plugins": list(packages)}})
    config["arms"] = arms
    tasks = []
    for arm, selection in arms.items():
        safe_name(arm)
        if set(selection["plugins"]) - set(packages):
            raise ValueError("Arm references an unconfigured plugin.")
        dependency_check({key: packages[key] for key in selection["plugins"]})
        for record in records:
            allowed = record["spec"].get("arms")
            if allowed and arm not in allowed:
                continue
            for repeat in range(1, config.get("repeat", 1) + 1):
                tasks.append((arm, selection, record, repeat))
                if len(tasks) > limits["max_sessions"]:
                    raise ValueError("Selected sessions exceed max_sessions.")
    if not tasks:
        raise ValueError("No case/arm combinations selected.")
    if len(tasks) > limits["max_sessions"]:
        raise ValueError("Selected sessions exceed max_sessions.")
    if output.exists() or output.is_symlink():
        raise ValueError("Run output already exists; use a fresh output directory.")
    if output.resolve().is_relative_to(source):
        raise ValueError("Evaluation output must be outside plugin source.")
    output.mkdir(parents=True, mode=0o700)
    config_snapshot = json.loads(redact(json.dumps(config)))
    write(
        output / "run.json",
        {
            "schema_version": "0.1",
            "tool_version": __version__,
            "configuration": config_snapshot,
            "source_plugin": source_info["manifest"],
            "source_manifest_hash": digest(
                (source / "openstudio-si-plugin.yaml").read_bytes()
            ),
            "limits": limits,
            "sessions": [],
            "planned_sessions": [
                f'{t[2]["spec"]["case_id"]}--{t[0]}--{t[3]}' for t in tasks
            ],
            "result_hashes": {},
            "skipped_cases": [
                r["spec"]["case_id"]
                for r in records
                if not any(t[2] is r for t in tasks)
            ],
            "judge_score_policy": "advisory",
        },
        redact,
    )
    deadline = time.monotonic() + limits["run_timeout_seconds"]
    sessions = []
    pending = {}
    interrupted = False
    cost_remaining = limits.get("max_cost_usd")
    cost_unknown = False
    for arm, selection, record, repeat in tasks:
        case = record["spec"]
        identity = f'{safe_name(case["case_id"])}--{arm}--{repeat}'
        session = output / "sessions" / identity
        session.mkdir(parents=True)
        workspace_context = tempfile.TemporaryDirectory(prefix="si-eval-workspace-")
        workspace = Path(workspace_context.name).resolve()
        private_context = tempfile.TemporaryDirectory(prefix="si-eval-control-")
        private = Path(private_context.name).resolve()
        result = {
            "schema_version": "0.1",
            "session_id": identity,
            "case": case,
            "case_hash": record["hash"],
            "legacy_routing": record["legacy"],
            "arm": arm,
            "repeat": repeat,
            "host": config["host"],
            "agent": config["agent"],
            "plugins": [
                {
                    "id": name,
                    "version": packages[name]["manifest"]["version"],
                    "descriptor_hash": packages[name]["descriptor_hash"],
                    "file_hashes": packages[name]["files"],
                }
                for name in selection["plugins"]
            ],
            "host_version": "not_started",
            "status": "incomplete",
            "checks": [],
            "gaps": [],
            "notes": [],
            "warnings": [],
            "capabilities": {},
            "environment_names": [],
            "usage": {},
            "evidence_hashes": {},
            "judge": {"status": "not_configured", "score_policy": "advisory"},
        }
        original = {}
        artifacts_captured = False
        start = time.monotonic()
        agent_started = False
        try:
            if start >= deadline:
                raise ValueError("run_time_budget_exhausted")
            if cost_unknown:
                raise ValueError(
                    "agent_cost_budget_unobservable_after_previous_session"
                )
            if cost_remaining is not None and cost_remaining <= 0:
                raise ValueError("agent_cost_budget_exhausted")
            if (
                sum(inside(source, p).stat().st_size for p in case["inputs"])
                > limits["max_artifact_bytes"]
            ):
                raise ValueError("Staged inputs exceed byte limit.")
            for relative in case["inputs"]:
                data = inside(source, relative).read_bytes()
                target = workspace / "inputs" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                original[target.relative_to(workspace).as_posix()] = digest(data)
            write(session / "inputs.json", original)
            copied = []
            public_texts = [
                (workspace / relative).read_text(errors="replace")
                for relative in original
            ]
            for name in selection["plugins"]:
                package = packages[name]
                target = private / "plugins" / name
                receipt = agent_view(
                    package,
                    target,
                    withheld_files=(
                        withheld_files if name == source_info["manifest"]["id"] else ()
                    ),
                )
                for info in result["plugins"]:
                    if info["id"] == name:
                        info["agent_view_file_hashes"] = receipt["files"]
                        info["withheld_evaluation_files"] = receipt["withheld"]
                copied.append(target)
                public_texts.extend(
                    (target / relative).read_text(errors="replace")
                    for relative in receipt["files"]
                )
            prompt = (
                case["prompt"]
                .replace("{input_dir}", str(workspace / "inputs"))
                .replace("{sandbox}", str(workspace))
            )
            (session / "prompt.txt").write_text(redact(prompt))
            public_texts.append(prompt)
            case_deadline = min(start + limits["case_timeout_seconds"], deadline)
            budget = case_deadline - time.monotonic()
            if budget <= 0:
                raise ValueError("case_time_budget_exhausted_during_staging")
            capture_config = {
                **config,
                "limits": {
                    **limits,
                    **(
                        {"max_cost_usd": cost_remaining}
                        if cost_remaining is not None
                        else {}
                    ),
                },
            }
            prepared = capture(capture_config, workspace, copied, private, budget)
            result["capabilities"] = prepared["capabilities"]
            result["environment_names"] = prepared["environment_names"]
            result["host_version"] = prepared["version"]
            write(session / "host-inventory.json", prepared["inventory"], redact)
            remaining = case_deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("case_time_budget_exhausted_during_setup")
            agent_started = True
            execution = process(
                prepared["command"],
                workspace,
                prepared["env"],
                remaining,
                limits["max_trace_bytes"],
                prompt,
            )
            for name in ("stdout", "stderr"):
                (
                    session / (name + ".jsonl" if name == "stdout" else name + ".txt")
                ).write_text(redact(execution[name]))
            normalized = normalize(config["host"], redact(execution["stdout"]))
            # Normalize known server qualification forms without discarding the host identity.
            aliases = {}
            for name in selection["plugins"]:
                declared = json.loads(
                    (packages[name]["path"] / DESCRIPTOR).read_text()
                )["source_mcp_servers"]
                for server in declared:
                    aliases[server] = server
                    aliases["plugin_" + name + "_" + server] = server
            for row in normalized["trace"]:
                provider = row["provider"]
                row["host_provider"] = provider
                if provider in aliases:
                    row["provider"] = aliases[provider]
            if config["host"] == "claude":
                result["capabilities"]["observed_skills"] = normalized["skills"]
                result["capabilities"]["observed_slash_commands"] = normalized[
                    "slash_commands"
                ]
            if config["host"] == "claude" and normalized["tools"] is not None:
                result["capabilities"]["observed_host_tools"] = normalized["tools"]
                requested = {
                    rule.split("(", 1)[0]
                    for rule in result["capabilities"]["host_tools"]
                }
                if not requested <= set(normalized["tools"]):
                    normalized["gaps"].append(
                        "host_tool_profile_missing_requested_tools"
                    )
            write(session / "trace.json", normalized["trace"], redact)
            (session / "answer.txt").write_text(redact(normalized["answer"]))
            artifacts, artifact_gaps = snapshot_outputs(
                workspace,
                private,
                original,
                session,
                limits["max_artifact_bytes"],
                redact,
            )
            artifacts_captured = True
            result["checks"], check_gaps = observe(
                case,
                record["legacy"],
                arm,
                config["host"],
                normalized,
                artifacts,
                session,
            )
            result["gaps"], result["notes"] = split_gaps(
                [
                    *execution["gaps"],
                    *normalized["gaps"],
                    *artifact_gaps,
                    *check_gaps,
                ]
            )
            accesses = holdout_access(
                normalized["trace"],
                {name: packages[name] for name in selection["plugins"]},
                source,
                cases.resolve(),
                output,
                withheld_files,
            )
            matches = holdout_content(
                normalized["trace"],
                normalized["answer"],
                holdouts.values(),
                holdout_hashes,
                public_texts=map(redact, public_texts),
            )
            accesses.extend(match["location"] for match in matches if match["blocking"])
            advisory = [match["location"] for match in matches if not match["blocking"]]
            if advisory:
                result["warnings"].append(
                    {
                        "code": "reference_text_match",
                        "confidence": (
                            "low"
                            if advisory == ["answer:holdout_content"]
                            else "medium"
                        ),
                        "locations": advisory,
                        "message": "Reference text overlap warrants review; it is not proof of access and does not affect status or scoring.",
                    }
                )
            result["checks"].append(
                {
                    "check": "holdout_access",
                    "passed": not accesses,
                    "reason": (
                        "Holdout path or substantial reference content in "
                        + ", ".join(accesses)
                        if accesses
                        else "No evaluation material access observed."
                    ),
                }
            )
            for path in external_model_paths(
                normalized["trace"], workspace, private / "host-home"
            ):
                result["gaps"].append("artifact_outside_capture_roots:" + path)
            if config["host"] == "claude" and set(normalized["plugins"] or []) != set(
                selection["plugins"]
            ):
                result["gaps"].append("host_plugin_inventory_missing_or_mismatched")
            if execution["returncode"] or normalized["failed"]:
                result["gaps"].append("host_execution_failed")
            result["usage"] = {
                **normalized["usage"],
                "duration_seconds": execution["duration_seconds"],
                "cost_reporting": (
                    "host-reported" if config["host"] == "claude" else "unavailable"
                ),
            }
            result["status"] = (
                "incomplete"
                if result["gaps"]
                else (
                    "passed"
                    if all(c["passed"] is not False for c in result["checks"])
                    else "failed"
                )
            )
        except KeyboardInterrupt:
            interrupted = True
            result["gaps"].append("host_interrupted")
            write(session / "trace.json", [])
            (session / "answer.txt").write_text("")
        except Exception as exc:  # noqa: BLE001
            result["gaps"].append(redact(str(exc)))
            if not (session / "trace.json").exists():
                write(session / "trace.json", [])
            if not (session / "answer.txt").exists():
                (session / "answer.txt").write_text("")
        if agent_started and not artifacts_captured:
            try:
                _, extra_gaps = snapshot_outputs(
                    workspace,
                    private,
                    original,
                    session,
                    limits["max_artifact_bytes"],
                    redact,
                )
                blocking, notes = split_gaps(extra_gaps)
                result["gaps"].extend(blocking)
                result["notes"].extend(notes)
            except Exception as exc:  # noqa: BLE001
                result["gaps"].append("artifact_capture_failed: " + redact(str(exc)))
        if cost_remaining is not None and agent_started:
            amount = result["usage"].get("cost_usd")
            if (
                isinstance(amount, (int, float))
                and not isinstance(amount, bool)
                and amount >= 0
            ):
                cost_remaining = max(0, cost_remaining - amount)
            else:
                cost_unknown = True
        for relative in [
            "host-inventory.json",
            "inputs.json",
            "prompt.txt",
            "stdout.jsonl",
            "stderr.txt",
            "trace.json",
            "answer.txt",
            "reference.txt",
        ]:
            path = session / relative
            if path.exists():
                result["evidence_hashes"][relative] = digest(path.read_bytes())
        for path in (session / "artifacts").glob("**/*"):
            if path.is_file():
                result["evidence_hashes"][path.relative_to(session).as_posix()] = (
                    digest(path.read_bytes())
                )
        for temporary in ("host-home", "workspace", "plugins", "marketplace"):
            temporary_path = session / temporary
            if temporary_path.is_symlink():
                temporary_path.unlink()
            else:
                shutil.rmtree(temporary_path, ignore_errors=True)
            if temporary_path.exists():
                result["gaps"].append("private_state_cleanup_failed: " + temporary)
                result["status"] = "incomplete"
        for context, name in (
            (workspace_context, "workspace"),
            (private_context, "private_state"),
        ):
            try:
                context.cleanup()
            except OSError as exc:
                result["gaps"].append(name + "_cleanup_failed: " + redact(str(exc)))
                result["status"] = "incomplete"
        result = json.loads(redact(json.dumps(result)))
        # While any agent remains, persist only public task fields. Goldens stay in
        # controller memory, including inline expectations and case rubrics.
        pending[identity] = (
            result["case"],
            result["status"],
            case.get("expected", {}).get("reference"),
        )
        result["case"] = {
            key: result["case"][key]
            for key in ("schema_version", "case_id", "description", "prompt", "inputs")
        }
        result["case_hash"] = json_hash(result["case"])
        result["holdout_pending"] = True
        result["status"] = "incomplete"
        validate("evaluation-result", result)
        write(session / "result.json", result)
        sessions.append(identity)
        metadata = json.loads((output / "run.json").read_text())
        metadata["sessions"] = sessions
        metadata["result_hashes"][identity] = digest(
            (session / "result.json").read_bytes()
        )
        write(output / "run.json", metadata)
        if interrupted or "host_interrupted" in result["gaps"]:
            interrupted = True
            break
    # Every agent has exited and its private home has been removed before holdouts
    # are written. Interrupted runs finalize only the sessions actually captured.
    metadata = json.loads((output / "run.json").read_text())
    for identity, (full_case, status, reference) in pending.items():
        session = output / "sessions" / identity
        result = json.loads((session / "result.json").read_text())
        result.update(
            case=full_case,
            case_hash=json_hash(full_case),
            status=status,
            holdout_pending=False,
        )
        if reference:
            (session / "reference.txt").write_text(holdouts[reference])
            result["evidence_hashes"]["reference.txt"] = digest(
                (session / "reference.txt").read_bytes()
            )
        validate("evaluation-result", result)
        write(session / "result.json", result)
        metadata["result_hashes"][identity] = digest(
            (session / "result.json").read_bytes()
        )
    write(output / "run.json", metadata)
    if config.get("judge") and not interrupted:
        rejudge(
            output,
            config["judge"],
            {**limits, "run_timeout_seconds": max(0.001, deadline - time.monotonic())},
        )
    return report(output)


def rejudge(root, settings, limits=None):
    validate("evaluation-judge-config", settings)
    metadata = json.loads(inside(root, "run.json").read_text())
    limits = {**DEFAULTS, **metadata.get("limits", {}), **(limits or {})}
    history = (
        root
        / "judgments"
        / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    )
    if not history.resolve().is_relative_to(root.resolve()):
        raise ValueError("Escaping judgment history path.")
    history.mkdir(parents=True)
    redact = redactor([settings.get("api_key_env")])
    deadline = time.monotonic() + limits["run_timeout_seconds"]
    if len(metadata["sessions"]) > limits["max_sessions"]:
        raise ValueError("Rejudge session limit exceeded.")
    for identity in metadata["sessions"]:
        session = root / "sessions" / safe_session(identity)
        result = json.loads(
            inside(root, "sessions/" + identity + "/result.json").read_text()
        )
        validate("evaluation-result", result)
        try:
            if (
                digest((session / "result.json").read_bytes())
                != metadata["result_hashes"][identity]
            ):
                raise ValueError("Captured result changed.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("judge_run_time_budget_exhausted")
            value = judge_session(
                session,
                result,
                settings,
                {
                    **limits,
                    "judge_timeout_seconds": min(
                        limits["judge_timeout_seconds"], remaining
                    ),
                },
                redact,
            )
        except KeyboardInterrupt:
            write(
                history / (identity + ".json"),
                {
                    "status": "incomplete",
                    "score_policy": "advisory",
                    "error": "judge_interrupted",
                },
                redact,
            )
            break
        except Exception as exc:  # noqa: BLE001
            value = {
                "status": "incomplete",
                "score_policy": "advisory",
                "error": redact(str(exc)),
            }
        write(history / (identity + ".json"), value, redact)
    write(
        history / "index.json",
        {
            "sessions": metadata["sessions"],
            "judge": settings,
            "prompt_policy": "advisory",
        },
        redact,
    )
    metadata.setdefault("judgment_history", []).append(history.name)
    write(root / "run.json", metadata, redact)
    return report(root)


def safe_session(identity):
    if (
        not all(safe_name(part) for part in identity.split("--")[:-1])
        or not identity.split("--")[-1].isdigit()
    ):
        raise ValueError("Unsafe session id in run metadata.")
    return identity


def report(root):
    metadata = json.loads(inside(root, "run.json").read_text())
    rows = []
    histories = metadata.get("judgment_history", [])
    if any(
        not re.fullmatch(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{8}", name) for name in histories
    ):
        raise ValueError("Unsafe judgment history id.")
    latest = root / "judgments" / histories[-1] if histories else None
    if latest and not latest.resolve().is_relative_to(root.resolve()):
        raise ValueError("Escaping judgment history path.")
    for identity in metadata.get("planned_sessions", metadata["sessions"]):
        if identity not in metadata["sessions"]:
            rows.append(
                {
                    "session_id": identity,
                    "status": "incomplete",
                    "gaps": ["session_not_executed"],
                    "checks": [],
                    "usage": {},
                    "judge": {"status": "not_configured"},
                }
            )
            continue
        session = root / "sessions" / safe_session(identity)
        result = json.loads(
            inside(root, "sessions/" + identity + "/result.json").read_text()
        )
        if (
            digest((session / "result.json").read_bytes())
            != metadata["result_hashes"][identity]
        ):
            raise ValueError("Captured result changed.")
        validate("evaluation-result", result)
        verify_evidence(session, result)
        if latest:
            path = latest / (identity + ".json")
            judgment = (
                json.loads(path.read_text())
                if path.exists()
                else {
                    "status": "incomplete",
                    "score_policy": "advisory",
                    "error": "judge_session_not_executed",
                }
            )
        else:
            judgment = result["judge"]
        rows.append(
            {
                "session_id": identity,
                "status": result["status"],
                "gaps": result["gaps"],
                "notes": result.get("notes", []),
                "warnings": result.get("warnings", []),
                "capabilities": result.get("capabilities", {}),
                "checks": result["checks"],
                "usage": result["usage"],
                "judge": judgment,
            }
        )
    status = (
        "incomplete"
        if any(
            row["status"] == "incomplete" or row["judge"]["status"] == "incomplete"
            for row in rows
        )
        else ("failed" if any(row["status"] == "failed" for row in rows) else "passed")
    )
    value = {
        "schema_version": "0.1",
        "status": status,
        "score_policy": "advisory",
        "sessions": rows,
        "warning_count": sum(len(row.get("warnings", [])) for row in rows),
        "skipped_cases": metadata.get("skipped_cases", []),
    }
    write(root / "summary.json", value)
    lines = [
        "# Local evaluation report",
        "",
        f'Agent: {metadata["configuration"]["host"]} / {metadata["configuration"]["agent"]["model"]}',
        "",
        "Judge scores are advisory; observable checks remain independent. Unknown usage/cost is not estimated.",
        "",
        "| Session | Observations | Judge | SI weighted score (advisory) | Reference text warnings | Input tokens | Output tokens | Agent cost USD |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        cells = [
            row["session_id"],
            row["status"],
            row["judge"]["status"],
            row["judge"].get("score", "—"),
            ", ".join(w["confidence"] + " confidence" for w in row.get("warnings", []))
            or "—",
            row["usage"].get("input_tokens", "unknown"),
            row["usage"].get("output_tokens", "unknown"),
            row["usage"].get("cost_usd", "unknown"),
        ]
        lines.append("| " + " | ".join(map(str, cells)) + " |")
        for warning in row.get("warnings", []):
            lines.append(
                "\nReference text warning ("
                + warning["confidence"]
                + " confidence): "
                + warning["message"]
                + " Locations: "
                + ", ".join(warning["locations"])
            )
        for check in row["checks"]:
            if check["passed"] is False:
                lines.append(f'\nFailed check: {check["check"]}: {check["reason"]}')
        for gap in row["gaps"]:
            lines.append("\nBlocking evidence gap: " + gap)
        for note in row.get("notes", []):
            lines.append("\nObservation note: " + note)
        for note in row["judge"].get("notes", []):
            lines.append("\nJudge evidence note: " + note)
        if row.get("capabilities"):
            lines.append(
                "\nExecution capabilities: "
                + json.dumps(row["capabilities"], sort_keys=True)
            )
        for check in row["checks"]:
            if check.get("status") == "not_observable":
                lines.append("\nNot observable: " + check["check"])
    for row in rows:
        judgment = row["judge"]
        if judgment.get("framework_version"):
            lines.extend(
                [
                    "",
                    f'## G-Eval: {row["session_id"]}',
                    "",
                    f'DeepEval {judgment["framework_version"]}; judge {judgment["provider"]} / {judgment["model"]}. Scores are advisory.',
                    "",
                    "| Metric | DeepEval score | DeepEval threshold | DeepEval per-metric pass (advisory) |",
                    "| --- | --- | --- | --- |",
                ]
            )
            for metric in judgment.get("metrics", []):
                lines.append(
                    f'| {metric["id"]} | {metric["score"]} | {metric["threshold"]} | {metric["passed"]} |'
                )
    (root / "report.md").write_text("\n".join(lines) + "\n")
    return value
