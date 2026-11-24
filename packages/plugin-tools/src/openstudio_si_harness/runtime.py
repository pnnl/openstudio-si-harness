"""Explicit selected-runtime probes, separate from metadata inventory."""

import hashlib
import json
import math
import os
import re
import signal
import sys
import tempfile
import time
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from .compatibility import satisfies, semver
from .discovery import read_yaml
from .mcp import resolve_server_command
from .mcp_worker import server_environment
from .process import process

VERSION_TEXT = re.compile(
    r"(?<![A-Za-z0-9_.+-])[vV]?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)(?![A-Za-z0-9_.+-])"
)
DASH_BUILD = re.compile(r"(\d+\.\d+\.\d+)-([0-9a-fA-F]{7,40})")


class RuntimeSelection:
    def __init__(self, profile_path, *, check=False):
        raw = Path(profile_path).read_bytes()
        self.profile = read_yaml(raw.decode("utf-8"))
        schema = json.loads(
            files("openstudio_si_harness")
            .joinpath("schemas/runtime-profile.schema.json")
            .read_text()
        )
        errors = list(Draft202012Validator(schema).iter_errors(self.profile))
        if errors:
            raise ValueError(
                "Invalid runtime profile fields: "
                + ", ".join(sorted({error.json_path for error in errors}))
            )
        self.digest = hashlib.sha256(raw).hexdigest()
        self.host = self.profile["host"]
        self.check = check
        self.bindings = {}
        for runtime in self.profile["runtimes"].values():
            if not all(
                Path(path).is_absolute()
                for path in [
                    *([runtime["python"]] if "python" in runtime else []),
                    runtime["cwd"],
                    *runtime["path"],
                    *runtime.get("metadata_paths", []),
                ]
            ):
                raise ValueError(
                    "Runtime Python, cwd and PATH entries must be absolute."
                )
            if any(os.pathsep in path or "\x00" in path for path in runtime["path"]):
                raise ValueError(
                    "Runtime PATH entries cannot contain separators or NULs."
                )
            if any(
                key.upper() == "PATH" or key.startswith("PYTHON")
                for key in runtime["env"]
            ):
                raise ValueError(
                    "Use path for PATH; PYTHON environment overrides are unsupported by isolated probes."
                )
        for binding in self.profile["bindings"]:
            key = (binding["plugin"], binding["dependency"])
            if (
                key in self.bindings
                or binding["runtime"] not in self.profile["runtimes"]
            ):
                raise ValueError("Duplicate runtime binding or unknown runtime ID.")
            self.bindings[key] = binding
        self.limits = {
            "timeout_seconds": 5,
            "total_seconds": 30,
            "max_output_bytes": 65536,
            **self.profile.get("limits", {}),
        }
        if not all(math.isfinite(value) for value in self.limits.values()):
            raise ValueError("Runtime limits must be finite.")
        self.deadline = None
        self.probes_executed = 0
        self.unused_bindings = []
        self.active_bindings = {}
        self.cache = {}

    def validate_bindings(self, entries):
        self.unused_bindings = []
        self.active_bindings = {}
        available = {
            (entry["id"], dep["id"]): dep
            for entry in entries
            if entry.get("host") == self.host and "_loaded" in entry
            for dep in entry["_loaded"]["manifest"]["dependencies"]
        }
        for key, binding in self.bindings.items():
            if key not in available:
                self.unused_bindings.append(
                    {
                        **binding,
                        "reason": "plugin_or_dependency_not_selected_or_invalid",
                    }
                )
                continue
            if available[key]["kind"] not in {
                "python-distribution",
                "configuration",
                "command",
                "mcp-server",
                "dataset",
            }:
                self.unused_bindings.append(
                    {**binding, "reason": "runtime_kind_not_supported"}
                )
                continue
            if (
                binding.get("allow_dataset_metadata")
                or binding.get("dataset_command")
                or binding.get("dataset_mcp")
            ) and available[key]["kind"] != "dataset":
                self.unused_bindings.append(
                    {**binding, "reason": "dataset_permission_not_applicable"}
                )
                continue
            if available[key]["kind"] == "dataset":
                if bool(binding.get("dataset_command")) == bool(
                    binding.get("dataset_mcp")
                ):
                    self.unused_bindings.append(
                        {**binding, "reason": "dataset_binding_ambiguous"}
                    )
                    continue
                reference = binding.get("dataset_mcp") or binding.get("dataset_command")
                command = available.get((key[0], reference))
                expected_kind = (
                    "mcp-server" if binding.get("dataset_mcp") else "command"
                )
                if not command or command["kind"] != expected_kind:
                    self.unused_bindings.append(
                        {**binding, "reason": "dataset_command_dependency_missing"}
                    )
                    continue
                linked_binding = self.bindings.get((key[0], reference))
                if (
                    binding.get("dataset_mcp")
                    and linked_binding
                    and linked_binding["runtime"] != binding["runtime"]
                ):
                    self.unused_bindings.append(
                        {**binding, "reason": "dataset_mcp_runtime_mismatch"}
                    )
                    continue
                binding = {**binding, "_dataset_command": command["target"]}
            if (
                binding.get("allow_mcp_initialize") or binding.get("required_tools")
            ) and available[key]["kind"] != "mcp-server":
                self.unused_bindings.append(
                    {**binding, "reason": "mcp_permission_not_applicable"}
                )
                continue
            if binding.get("required_tools") and available[key]["probe"] != "mcp-tools":
                self.unused_bindings.append(
                    {**binding, "reason": "tools_check_requires_mcp_tools"}
                )
                continue
            if (
                binding.get("allow_command_version")
                and available[key]["kind"] != "command"
            ):
                self.unused_bindings.append(
                    {**binding, "reason": "permission_not_applicable"}
                )
                continue
            self.active_bindings[key] = binding

    def probe(self, dependency, entry, *, requested=True, server=None, provider=None):
        key = (entry["id"], dependency["id"])
        if key in self.cache:
            return self.cache[key]
        binding = self.active_bindings.get(key)
        evidence = {
            "measurement_scope": "not_measured",
            "host": self.host,
            "profile_hash": self.digest,
        }

        def result(status, reason, **details):
            actions = {
                "dataset_cache_missing": "No cache was observed. BESD creates a missing cache on normal server startup; this read-only probe does not start it or prove the dataset unavailable.",
                "dataset_cache_provenance_unknown": "The cache has no supported build-source record. Review it or explicitly rebuild with the updated provider to establish provenance; doctor never rewrites an existing cache.",
                "dataset_cache_stale": "The cache was built with different packaged JSON. Review the selected database and explicitly rebuild it if appropriate; doctor does not replace it.",
                "dataset_cache_invalid": "Select a valid database or explicitly rebuild a reviewed cache; doctor does not repair it.",
                "dataset_cache_wal_present": "Checkpoint the database using its owner application before requesting a stable cache fingerprint.",
                "dataset_command_not_found": "Install the provider command on the selected PATH or forward its absolute OPENSTUDIO_SI_<DATASET>_COMMAND override.",
                "dataset_metadata_not_authorized": "Review the provider command and opt in with allow_dataset_metadata in this dependency's runtime binding.",
                "mcp_output_limit": "Increase limits.max_output_bytes in the runtime profile (maximum 4194304 bytes), or reduce the server inventory payload.",
                "runtime_output_limit": "Increase limits.max_output_bytes in the runtime profile (maximum 4194304 bytes).",
                "mcp_server_command_not_found": "Install the server command on the selected profile PATH or forward its absolute OPENSTUDIO_SI_<SERVER>_COMMAND override.",
                "mcp_command_override_invalid": "The forwarded MCP executable override must name an absolute executable path.",
                "mcp_protocol_error": "Check the selected executable and profile environment. Forward HOME for uvx/npx-style launchers and include /usr/bin and /bin for shell-wrapper utilities.",
            }
            return {
                "status": status,
                "reason": reason,
                "next_action": actions.get(
                    reason,
                    "Review the selected runtime profile and the dependency requirement; host enablement is checked separately.",
                ),
                "evidence": {**evidence, **details},
            }

        if not binding:
            return result("unknown", "runtime_binding_missing")
        evidence["runtime"] = binding["runtime"]
        if not self.check:
            return result("unknown", "runtime_check_not_requested")
        if not requested:
            return result("unknown", "runtime_outside_selection")
        if dependency["kind"] == "mcp-server" and not binding.get(
            "allow_mcp_initialize", False
        ):
            return result("unknown", "mcp_initialization_not_authorized")
        if dependency["kind"] == "dataset" and not binding.get(
            "allow_dataset_metadata", False
        ):
            return result("unknown", "dataset_metadata_not_authorized")
        if os.name != "posix":
            return result("unknown", "runtime_platform_unsupported")
        runtime = self.profile["runtimes"][binding["runtime"]]
        # Preserve venv interpreter symlinks; resolving them can select base Python.
        evidence["cwd"] = runtime["cwd"]
        command_probe = dependency["kind"] in {"mcp-server", "dataset"}
        if command_probe:
            evidence.update(
                client_python=sys.executable,
                server_launch_policy="desktop_command_resolution",
            )
        elif "python" not in runtime:
            return result("unknown", "runtime_python_not_selected")
        else:
            evidence["python"] = runtime["python"]
        if not Path(runtime["cwd"]).is_dir() or (
            not command_probe
            and (
                not Path(runtime["python"]).is_file()
                or not os.access(runtime["python"], os.X_OK)
            )
        ):
            return result("unavailable", "selected_runtime_unavailable")
        env = {"PATH": os.pathsep.join(runtime["path"])}
        for destination, source in runtime["env"].items():
            if source in os.environ:
                env[destination] = os.environ[source]
        evidence.update(
            environment_names=sorted(env),
            unresolved_environment_names=sorted(set(runtime["env"]) - env.keys()),
        )
        if dependency["kind"] == "dataset":
            linked_server = binding.get("dataset_mcp")
            declaration = (
                server if linked_server else {"command": binding["_dataset_command"]}
            )
            if declaration is None:
                return result("unknown", "mcp_declaration_missing")
            executable, origin, resolution_error = resolve_server_command(
                binding["_dataset_command"] if linked_server else dependency["target"],
                declaration,
                env,
                runtime["cwd"],
            )
            evidence.update(
                dataset_command=binding.get("dataset_command"),
                dataset_mcp=linked_server,
                provider=provider or entry["id"],
                resolution_source=origin,
                dataset_executable=executable,
                operation="--si-dataset-metadata",
            )
            if resolution_error:
                return result("unavailable", "dataset_command_not_found")
            try:
                env = server_environment(declaration, env)
            except KeyError as exc:
                return result(
                    "unavailable",
                    "mcp_environment_missing",
                    missing_environment_names=[exc.args[0]],
                )
            evidence["environment_names"] = sorted(env)
            dataset_arguments = declaration.get("args", [])
        if dependency["kind"] == "mcp-server":
            evidence.update(
                server=dependency["target"],
                provider=provider or entry["id"],
                home_present=bool(env.get("HOME")),
            )
            if server is None:
                return result("unknown", "mcp_declaration_missing")
            try:
                expanded = server_environment(server, env)
            except KeyError as exc:
                return result(
                    "unavailable",
                    "mcp_environment_missing",
                    missing_environment_names=[exc.args[0]],
                )
            evidence["home_present"] = bool(expanded.get("HOME"))
            executable, origin, resolution_error = resolve_server_command(
                dependency["target"], server, env, runtime["cwd"]
            )
            evidence["resolution_source"] = origin
            if resolution_error:
                return result("unavailable", resolution_error)
            evidence["server_executable"] = executable
        now = time.monotonic()
        if self.deadline is None:
            self.deadline = now + self.limits["total_seconds"]
        deadline = min(self.deadline, now + self.limits["timeout_seconds"])
        output_limit = self.profile.get("limits", {}).get(
            "max_output_bytes",
            (
                1048576
                if dependency["kind"] == "mcp-server"
                else self.limits["max_output_bytes"]
            ),
        )

        def execute(command, prompt=None, *, version_output=False):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "runtime_timeout"
            try:
                supervisor = str(
                    files("openstudio_si_harness").joinpath("runtime_supervisor.py")
                )
                with tempfile.TemporaryFile() as receipt:
                    captured = process(
                        [
                            sys.executable,
                            "-I",
                            "-S",
                            supervisor,
                            str(receipt.fileno()),
                            *command,
                        ],
                        runtime["cwd"],
                        env,
                        remaining,
                        output_limit,
                        prompt,
                        pass_fds=(receipt.fileno(),),
                    )
                    receipt.seek(0)
                    receipt_text = receipt.read(1024).decode("utf-8")
                self.probes_executed += 1
            except (OSError, ValueError):
                return None, "runtime_probe_failed"
            if captured["gaps"]:
                for gap, reason in (
                    ("host_timeout", "runtime_timeout"),
                    ("host_interrupted", "runtime_probe_interrupted"),
                    ("trace_limit_exceeded", "runtime_output_limit"),
                ):
                    if gap in captured["gaps"]:
                        return None, reason
                return None, "runtime_cleanup_failed"
            try:
                receipt_data = json.loads(receipt_text)
                completed = (
                    isinstance(receipt_data, dict)
                    and set(receipt_data) == {"returncode"}
                    and type(receipt_data["returncode"]) is int
                )
            except ValueError:
                completed = False
            if (
                captured["returncode"] != -signal.SIGKILL
                or not completed
                or receipt_data["returncode"]
            ):
                return None, "runtime_probe_failed"
            return (
                captured["stdout"]
                + ("\n" + captured["stderr"] if version_output else ""),
                None,
            )

        evidence["measurement_scope"] = "selected_runtime"
        if dependency["kind"] == "dataset":
            raw, error = execute(
                [executable, *dataset_arguments, "--si-dataset-metadata"]
            )
        elif dependency["kind"] == "mcp-server":
            worker = str(files("openstudio_si_harness").joinpath("mcp_worker.py"))
            raw, error = execute(
                [sys.executable, "-I", worker],
                json.dumps(
                    {
                        "server": {**server, "command": executable},
                        "probe": dependency["probe"],
                        "max_bytes": output_limit,
                        "timeout_seconds": max(0.05, deadline - time.monotonic() - 0.1),
                    }
                ),
            )
        else:
            worker = str(files("openstudio_si_harness").joinpath("runtime_worker.py"))
            raw, error = execute(
                [runtime["python"], "-I", "-S", worker],
                json.dumps(
                    {
                        "kind": dependency["kind"],
                        "target": dependency["target"],
                        "metadata_paths": runtime.get("metadata_paths"),
                    }
                ),
            )
        if error:
            answer = result("unknown", error)
        else:
            try:
                observed = json.loads(raw)
                if not isinstance(observed, dict):
                    raise TypeError("Invalid worker response")
                if dependency["kind"] == "dataset":
                    text_fields = [
                        observed.get("dataset"),
                        observed.get("version"),
                        *(
                            observed.get("package", {}).values()
                            if isinstance(observed.get("package"), dict)
                            else []
                        ),
                    ]
                    if any(
                        len(value) >= 4
                        and any(
                            isinstance(text, str) and value in text
                            for text in text_fields
                        )
                        for name, value in env.items()
                        if name != "PATH"
                    ):
                        answer = result(
                            "unknown", "dataset_response_contains_environment_value"
                        )
                    else:
                        answer = self._interpret_dataset(dependency, observed, result)
                elif dependency["kind"] == "mcp-server":
                    answer = self._interpret_mcp(dependency, binding, observed, result)
                else:
                    answer = self._interpret(
                        dependency, binding, observed, execute, result
                    )
            except (ValueError, TypeError, KeyError):
                answer = result("unknown", "runtime_response_invalid")
        # Never report raw streams or environment values, including a version
        # accidentally equal to an explicitly forwarded value.
        if "version" in answer["evidence"] and any(
            len(value) >= 4 and value in answer["evidence"]["version"]
            for name, value in env.items()
            if name != "PATH"
        ):
            answer = result("unknown", "runtime_version_contains_environment_value")
        if dependency["kind"] == "mcp-server":
            untrusted = [
                answer["evidence"].get(name)
                for name in ("server_name", "server_version", "protocol_version")
            ]
            untrusted.extend(answer["evidence"].get("tools") or [])
            if any(
                len(value) >= 4
                and any(isinstance(text, str) and value in text for text in untrusted)
                for name, value in env.items()
                if name != "PATH"
            ):
                answer = result("unknown", "mcp_response_contains_environment_value")
        self.cache[key] = answer
        return answer

    @staticmethod
    def _interpret_dataset(dependency, observed, result):
        schema = json.loads(
            files("openstudio_si_harness")
            .joinpath("schemas/dataset-metadata.schema.json")
            .read_text()
        )
        if not Draft202012Validator(schema).is_valid(observed):
            return result("unknown", "dataset_response_invalid")
        if observed["dataset"] != dependency["target"]:
            return result("unknown", "dataset_identity_mismatch")
        cache = observed["cache"]
        if (cache["state"] == "ready") != (cache["sha256"] is not None):
            return result("unknown", "dataset_response_invalid")
        evidence = {"dataset_metadata": observed}
        if observed["packaged_data"] is None:
            return result("unknown", "dataset_packaged_data_unreadable", **evidence)
        if not observed["package"]["version"]:
            return result("unknown", "dataset_package_version_unknown", **evidence)
        if cache["state"] != "ready":
            return result(
                ("unavailable" if cache["state"] == "invalid" else "unknown"),
                "dataset_cache_" + cache["state"],
                **evidence,
            )
        if not cache.get("source_data_sha256"):
            return result("unknown", "dataset_cache_provenance_unknown", **evidence)
        if cache["source_data_sha256"] != observed["packaged_data"]["sha256"]:
            return result("unavailable", "dataset_cache_stale", **evidence)
        if dependency.get("version_spec"):
            try:
                if not observed["version"]:
                    raise ValueError("Unversioned dataset")
                compatible = satisfies(observed["version"], dependency["version_spec"])
            except ValueError:
                return result("unknown", "dataset_version_unknown", **evidence)
            if not compatible:
                return result("unavailable", "dataset_version_conflict", **evidence)
        return result("ready", "dataset_identity_observed", **evidence)

    @staticmethod
    def _interpret_mcp(dependency, binding, observed, result):
        errors = {
            "mcp_sdk_unavailable",
            "mcp_output_limit",
            "mcp_pagination_limit",
            "mcp_timeout",
            "mcp_protocol_error",
        }
        if (
            set(observed) == {"ok", "reason"}
            and observed["ok"] is False
            and observed["reason"] in errors
        ):
            return result("unknown", observed["reason"])
        if (
            set(observed)
            != {
                "ok",
                "protocol_version",
                "server_name",
                "server_version",
                "tools_supported",
                "tools",
            }
            or observed["ok"] is not True
            or type(observed["tools_supported"]) is not bool
            or any(
                not isinstance(observed[name], str)
                or not 0 < len(observed[name]) <= 256
                for name in ("protocol_version", "server_name")
            )
            or not isinstance(observed["server_version"], str)
            or len(observed["server_version"]) > 256
        ):
            raise ValueError("Invalid MCP probe response")
        tools = observed["tools"]
        if dependency["probe"] == "mcp-tools":
            if (
                not isinstance(tools, list)
                or len(tools) > 1000
                or any(
                    not isinstance(name, str) or not 0 < len(name) <= 256
                    for name in tools
                )
                or len(set(tools)) != len(tools)
            ):
                raise ValueError("Invalid MCP tool inventory")
        elif tools is not None:
            raise ValueError("Unexpected tools in initialize-only probe")
        evidence = {
            name: observed[name]
            for name in (
                "protocol_version",
                "server_name",
                "server_version",
                "tools_supported",
                "tools",
            )
        }
        evidence["probe_connection"] = "initialized_then_closed"
        evidence["server_version"] = observed["server_version"] or None
        if dependency["probe"] == "mcp-tools":
            if not observed["tools_supported"]:
                return result("unavailable", "mcp_tools_not_supported", **evidence)
            missing = sorted(set(binding.get("required_tools", [])) - set(tools))
            if missing:
                return result(
                    "unavailable",
                    "mcp_required_tools_missing",
                    missing_tools=missing,
                    **evidence,
                )
        if dependency.get("version_spec"):
            try:
                compatible = satisfies(
                    observed["server_version"], dependency["version_spec"]
                )
            except ValueError:
                return result("unknown", "mcp_server_version_invalid", **evidence)
            if not compatible:
                return result("unavailable", "mcp_server_version_conflict", **evidence)
        return result(
            "ready",
            (
                "mcp_tools_available"
                if dependency["probe"] == "mcp-tools"
                else "mcp_initialized"
            ),
            **evidence,
        )

    @staticmethod
    def _interpret(dependency, binding, observed, execute, result):
        kind = dependency["kind"]
        if kind == "configuration":
            if set(observed) != {"present"} or type(observed["present"]) is not bool:
                raise ValueError("Invalid configuration evidence")
            present = observed["present"]
            return result(
                "ready" if present else "unavailable",
                "configuration_present" if present else "configuration_missing",
                present=present,
            )
        if kind == "python-distribution":
            if (
                set(observed)
                != {"versions", "search_complete", "metadata_scope", "metadata_paths"}
                or not isinstance(observed["versions"], list)
                or any(not isinstance(value, str) for value in observed["versions"])
                or type(observed["search_complete"]) is not bool
                or observed["metadata_scope"]
                not in {
                    "declared_metadata_paths",
                    "venv_metadata_paths",
                    "base_metadata_paths",
                }
                or not isinstance(observed["metadata_paths"], list)
                or any(not isinstance(path, str) for path in observed["metadata_paths"])
            ):
                raise ValueError("Invalid distribution evidence")
            metadata = {
                name: observed[name]
                for name in ("metadata_scope", "metadata_paths", "search_complete")
            }
            versions = observed["versions"]
            if not versions:
                return result(
                    "unavailable" if observed["search_complete"] else "unknown",
                    (
                        "distribution_missing"
                        if observed["search_complete"]
                        else "distribution_missing_isolated_scope"
                    ),
                    **metadata,
                )
            if len(versions) != 1:
                return result("unknown", "distribution_metadata_ambiguous", **metadata)
            try:
                actual = Version(versions[0])
            except InvalidVersion:
                return result("unknown", "distribution_version_invalid")
            compatible = not dependency.get("version_spec") or SpecifierSet(
                dependency["version_spec"]
            ).contains(actual, prereleases=True)
            if not compatible and not observed["search_complete"]:
                return result(
                    "unknown",
                    "distribution_version_isolated_scope",
                    version=versions[0],
                    **metadata,
                )
            return result(
                "ready" if compatible else "unavailable",
                (
                    "distribution_metadata"
                    if compatible
                    else "distribution_version_conflict"
                ),
                version=versions[0],
                **metadata,
            )
        if set(observed) != {"executable"} or not (
            observed["executable"] is None or isinstance(observed["executable"], str)
        ):
            raise ValueError("Invalid command evidence")
        executable = observed["executable"]
        if not executable:
            return result("unavailable", "command_not_found")
        version_required = dependency["probe"] == "command-version" or dependency.get(
            "version_spec"
        )
        if not version_required:
            return result("ready", "command_resolved")
        if not binding.get("allow_command_version", False):
            return result("unknown", "command_version_not_authorized")
        raw, error = execute([executable, "--version"], version_output=True)
        if error:
            return result("unknown", error)
        versions = set(VERSION_TEXT.findall(raw))
        if len(versions) != 1:
            return result("unknown", "command_version_ambiguous")
        version = versions.pop()
        dash_build = DASH_BUILD.fullmatch(version)
        version_format = binding.get("version_format")
        if version_format == "dash-build":
            if not dash_build:
                return result("unknown", "command_version_format_mismatch")
            version = "+".join(dash_build.groups())
        elif dash_build and version_format is None:
            return result("unknown", "command_version_format_required")
        semver(version)
        compatible = not dependency.get("version_spec") or satisfies(
            version, dependency["version_spec"]
        )
        return result(
            "ready" if compatible else "unavailable",
            "command_version" if compatible else "command_version_conflict",
            version=version,
            version_format=version_format or "semver",
        )
