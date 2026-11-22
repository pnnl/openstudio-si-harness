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
                    runtime["python"],
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
            }:
                self.unused_bindings.append(
                    {**binding, "reason": "runtime_kind_not_supported"}
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

    def probe(self, dependency, entry, *, requested=True):
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
            return {
                "status": status,
                "reason": reason,
                "next_action": "Review the selected runtime profile and the dependency requirement; host enablement is checked separately.",
                "evidence": {**evidence, **details},
            }

        if not binding:
            return result("unknown", "runtime_binding_missing")
        evidence["runtime"] = binding["runtime"]
        if not self.check:
            return result("unknown", "runtime_check_not_requested")
        if not requested:
            return result("unknown", "runtime_outside_selection")
        if os.name != "posix":
            return result("unknown", "runtime_platform_unsupported")
        runtime = self.profile["runtimes"][binding["runtime"]]
        # Preserve venv interpreter symlinks; resolving them can select base Python.
        evidence.update(python=runtime["python"], cwd=runtime["cwd"])
        if (
            not Path(runtime["python"]).is_file()
            or not os.access(runtime["python"], os.X_OK)
            or not Path(runtime["cwd"]).is_dir()
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
        now = time.monotonic()
        if self.deadline is None:
            self.deadline = now + self.limits["total_seconds"]
        deadline = min(self.deadline, now + self.limits["timeout_seconds"])

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
                        self.limits["max_output_bytes"],
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
                answer = self._interpret(dependency, binding, observed, execute, result)
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
        self.cache[key] = answer
        return answer

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
