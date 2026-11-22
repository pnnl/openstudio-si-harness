"""Read-only draft readiness: host-scoped metadata, without executing plugin code."""

import sys
from pathlib import Path

import yaml

from . import HARNESS_API_VERSION
from .compatibility import satisfies
from .discovery import read_json
from .export import DESCRIPTOR, HOSTS
from .manifest import load_plugin


def aggregate(states):
    states = list(states)
    return (
        "unavailable"
        if "unavailable" in states
        else "unknown" if "unknown" in states else "ready"
    )


def doctor(
    plugin_dirs=(),
    *,
    plugin_id=None,
    capability=None,
    host=None,
    runtime_profile=None,
    check_runtime=False,
):
    """Assess host-scoped source/export metadata, never terminal runtime readiness."""
    if host is not None and host not in HOSTS:
        raise ValueError("Unsupported host selection.")
    if capability and not plugin_id:
        raise ValueError("--capability requires --plugin-id.")
    if check_runtime and not runtime_profile:
        raise ValueError("--check-runtime requires --runtime-profile.")
    runtime_selection = None
    if runtime_profile:
        from .runtime import RuntimeSelection

        runtime_selection = RuntimeSelection(runtime_profile, check=check_runtime)
        if host and host != runtime_selection.host:
            raise ValueError("Runtime profile host disagrees with --host.")
        host = runtime_selection.host
    explicit = bool(plugin_dirs)
    roots = {Path(path).expanduser().resolve(): None for path in plugin_dirs}
    if not explicit:
        for cache_host in HOSTS:
            if host and cache_host != host:
                continue
            cache = Path.home() / f".{cache_host}/plugins/cache"
            if cache.is_dir():
                for path in sorted(cache.rglob(DESCRIPTOR)):
                    roots[path.parent.resolve()] = cache_host
    entries = []
    catalog = {}
    for root, cache_host in roots.items():
        try:
            loaded = load_plugin(root)
            package_host = (
                read_json(root / DESCRIPTOR)["host"]
                if (root / DESCRIPTOR).exists()
                else host or "source"
            )
            if cache_host and package_host != cache_host:
                raise ValueError("Descriptor host disagrees with cache host.")
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
            # Contain malformed entry failures; never echo embedded manifest values.
            entries.append(
                {
                    "path": str(root),
                    "host": cache_host or host,
                    "status": "unknown",
                    "reason": "invalid_or_unreadable_metadata",
                    "next_action": "Run plugin validate on this directory and repair its metadata, host placement or hashes.",
                }
            )
            continue
        if host and package_host != host:
            continue
        manifest = loaded["manifest"]
        entry = {
            "id": manifest["id"],
            "host": package_host,
            "version": manifest["version"],
            "name": manifest["name"],
            "description": manifest["description"],
            "owner": manifest["owner"],
            "inventory": loaded["inventory"],
            "path": str(root),
            "presence": (
                "cached"
                if not explicit
                else "exported" if (root / DESCRIPTOR).exists() else "source"
            ),
            "integrity": (
                "hashes_verified"
                if (root / DESCRIPTOR).exists()
                else "source_validated"
            ),
            "host_enabled": "unknown",
            "service_connected": "unknown",
            "_loaded": loaded,
        }
        entries.append(entry)
        catalog.setdefault((package_host, manifest["id"]), []).append(entry)
    invalid_entries = [entry for entry in entries if "_loaded" not in entry]
    if runtime_selection:
        runtime_selection.validate_bindings(entries)
    requested_runtime = set()

    def key(entry):
        return entry["host"], entry["id"]

    def candidates_evidence(candidates):
        return [
            {field: candidate[field] for field in ("path", "host", "version")}
            for candidate in candidates
        ]

    def finding(state, reason, action, **evidence):
        return {
            "status": state,
            "reason": reason,
            "next_action": action,
            "evidence": evidence,
        }

    def provider_candidates(dependency, entry):
        return catalog.get((entry["host"], dependency["target"]), [])

    def plugin_dependency(dependency, entry, stack):
        if dependency["target"] in {"openstudio-si", "openstudio-si-hvac"}:
            return finding(
                "unknown",
                (
                    "legacy_core_not_ingested"
                    if dependency["target"] == "openstudio-si"
                    else "legacy_hvac_not_ingested"
                ),
                "Run openstudio-si doctor for the existing core/HVAC contract; generic doctor cannot verify this dependency yet.",
                host=entry["host"],
                required=dependency.get("version_spec"),
            )
        candidates = provider_candidates(dependency, entry)
        if not candidates:
            other_hosts = [
                candidate
                for (candidate_host, identity), values in catalog.items()
                if identity == dependency["target"] and candidate_host != entry["host"]
                for candidate in values
            ]
            return finding(
                "unavailable" if explicit else "unknown",
                (
                    "provider_host_mismatch"
                    if other_hosts
                    else "plugin_not_selected" if explicit else "plugin_not_discovered"
                ),
                dependency.get(
                    "install_hint",
                    "Select a provider for this host with --plugin-dir; bind source plugins with --host. Cache absence does not prove it is uninstalled.",
                ),
                host=entry["host"],
                candidates=candidates_evidence(other_hosts),
            )
        if len(candidates) != 1:
            return finding(
                "unknown",
                "ambiguous_plugin_candidates",
                "Select one version for this host using --plugin-dir.",
                candidates=candidates_evidence(candidates),
            )
        provider = candidates[0]
        if dependency.get("version_spec") and not satisfies(
            provider["version"], dependency["version_spec"]
        ):
            return finding(
                "unavailable",
                "plugin_version_conflict",
                dependency.get(
                    "install_hint", "Select a compatible plugin release for this host."
                ),
                actual=provider["version"],
                required=dependency["version_spec"],
                host=entry["host"],
            )
        return finding(
            plugin_state(provider, stack),
            "plugin_metadata",
            "Resolve the provider's required dependencies; verify host enablement separately.",
            version=provider["version"],
            path=provider["path"],
            host=entry["host"],
        )

    def probe(dependency, entry, stack):
        kind, target = dependency["kind"], dependency["target"]
        if kind == "plugin":
            return plugin_dependency(dependency, entry, stack)
        if kind in {"python-distribution", "configuration", "command"}:
            if runtime_selection:
                if len(catalog[key(entry)]) != 1:
                    return finding(
                        "unknown",
                        "runtime_plugin_ambiguous",
                        "Select one plugin version before checking runtime bindings.",
                        measurement_scope="not_measured",
                    )
                return runtime_selection.probe(
                    dependency,
                    entry,
                    requested=(*key(entry), dependency["id"]) in requested_runtime,
                )
            return finding(
                "unknown",
                "runtime_not_selected",
                "Select and verify the host/MCP runtime in the next readiness stage; terminal state is not host evidence.",
                measurement_scope="not_measured",
                runtime="unselected",
                host=entry["host"],
            )
        if kind == "mcp-server" and dependency.get("provider"):
            dependencies = entry["_loaded"]["manifest"]["dependencies"]
            provider_spec = next(
                d for d in dependencies if d["id"] == dependency["provider"]
            )
            provider_check = plugin_dependency(provider_spec, entry, stack)
            if provider_check["status"] != "ready":
                return provider_check
            provider = provider_candidates(provider_spec, entry)[0]
            if target not in provider["_loaded"]["mcp_servers"]:
                return finding(
                    "unavailable",
                    "provider_mcp_not_declared",
                    "Select a provider release declaring this MCP connection for this host.",
                )
        return finding(
            "unknown",
            "runtime_probe_not_performed",
            "Live MCP/dataset probes need the next reviewed runtime binding; no service was started.",
            measurement_scope="not_measured",
            host=entry["host"],
        )

    required_states = {}

    def plugin_state(entry, stack):
        identity = key(entry)
        if identity in stack:
            return "unavailable"
        if identity not in required_states:
            required_states[identity] = (
                "unknown"
                if len(catalog[identity]) != 1
                else aggregate(
                    probe(dep, entry, (*stack, identity))["status"]
                    for dep in entry["_loaded"]["manifest"]["dependencies"]
                    if dep["required"]
                )
            )
        return required_states[identity]

    # Report detected cycles in the declared host-scoped graph. Optional edges
    # are diagnostic; required readiness above follows required edges only.
    cycles, visited = set(), set()

    def visit(identity, stack):
        if identity in stack:
            loop = stack[stack.index(identity) :]
            canonical = min(loop[i:] + loop[:i] for i in range(len(loop)))
            cycles.add((*canonical, canonical[0]))
            return
        if identity in visited:
            return
        candidates = catalog.get(identity, [])
        if len(candidates) == 1:
            for dep in candidates[0]["_loaded"]["manifest"]["dependencies"]:
                if dep["kind"] == "plugin":
                    visit((identity[0], dep["target"]), (*stack, identity))
        visited.add(identity)

    for identity in catalog:
        visit(identity, ())

    def request_runtime(entry, dependency_ids):
        for dep in entry["_loaded"]["manifest"]["dependencies"]:
            token = (*key(entry), dep["id"])
            if dep["id"] not in dependency_ids or token in requested_runtime:
                continue
            requested_runtime.add(token)
            if dep["kind"] == "plugin":
                providers = provider_candidates(dep, entry)
                if len(providers) == 1:
                    provider = providers[0]
                    request_runtime(
                        provider,
                        {
                            d["id"]
                            for d in provider["_loaded"]["manifest"]["dependencies"]
                            if d["required"]
                        },
                    )
            elif dep["kind"] == "mcp-server" and dep.get("provider"):
                request_runtime(entry, {dep["provider"]})

    if runtime_selection and check_runtime:
        selected_entries = [
            entry
            for entry in entries
            if "_loaded" in entry and (not plugin_id or entry["id"] == plugin_id)
        ]
        if plugin_id and not selected_entries and not invalid_entries:
            raise ValueError(
                "Selected plugin ID was not found in validated metadata for this host."
            )
        selected_caps = [
            cap
            for entry in selected_entries
            for cap in entry["_loaded"]["manifest"]["capabilities"]
            if not capability or cap["id"] == capability
        ]
        if (
            capability
            and not selected_caps
            and (selected_entries or not invalid_entries)
        ):
            raise ValueError("Selected capability ID was not found.")
        for entry in selected_entries:
            if len(catalog[key(entry)]) != 1:
                continue
            manifest = entry["_loaded"]["manifest"]
            required = {d["id"] for d in manifest["dependencies"] if d["required"]}
            required.update(
                name
                for cap in manifest["capabilities"]
                if not capability or cap["id"] == capability
                for name in cap["requires"]
            )
            request_runtime(entry, required)
    for entry in entries:
        if "_loaded" not in entry:
            continue
        manifest = entry["_loaded"]["manifest"]
        dependencies = {
            dep["id"]: {
                "id": dep["id"],
                "kind": dep["kind"],
                "target": dep["target"],
                "required": dep["required"],
                **probe(dep, entry, (key(entry),) if dep["required"] else ()),
            }
            for dep in manifest["dependencies"]
        }
        entry["dependencies"] = list(dependencies.values())
        baseline = {dep["id"] for dep in manifest["dependencies"] if dep["required"]}
        entry["capabilities"] = [
            {
                **cap,
                "status": aggregate(
                    dependencies[name]["status"]
                    for name in baseline | set(cap["requires"])
                ),
                "blocking_dependencies": sorted(
                    name
                    for name in baseline | set(cap["requires"])
                    if dependencies[name]["status"] != "ready"
                ),
            }
            for cap in manifest["capabilities"]
        ]
        entry["status"] = aggregate(cap["status"] for cap in entry["capabilities"])
        if len(catalog[key(entry)]) > 1:
            entry.update(
                status="unknown",
                reason="ambiguous_plugin_candidates",
                candidates=candidates_evidence(catalog[key(entry)]),
                next_action="Select one version for this host using --plugin-dir.",
            )
            for cap in entry["capabilities"]:
                cap["status"] = "unknown"
    for entry in entries:
        entry.pop("_loaded", None)
    selected = [
        entry for entry in entries if not plugin_id or entry.get("id") == plugin_id
    ]
    if plugin_id and not selected:
        if invalid_entries:
            selected = invalid_entries
        else:
            raise ValueError(
                "Selected plugin ID was not found in validated metadata for this host."
            )
    states = [entry["status"] for entry in selected]
    if capability:
        capabilities = [
            cap
            for entry in selected
            for cap in entry.get("capabilities", [])
            if cap["id"] == capability
        ]
        if not capabilities:
            if all("id" not in entry for entry in selected):
                states = ["unknown"]
            else:
                raise ValueError("Selected capability ID was not found.")
        else:
            states = [cap["status"] for cap in capabilities]
    return {
        "schema_version": "0.1",
        "scope": (
            "selected_runtime"
            if runtime_selection and runtime_selection.probes_executed
            else "metadata_only"
        ),
        "runtime_selection": (
            {
                "profile_hash": runtime_selection.digest,
                "host": host,
                "checks_requested": check_runtime,
                "probes_executed": runtime_selection.probes_executed,
                "unused_bindings": runtime_selection.unused_bindings,
            }
            if runtime_selection
            else None
        ),
        "harness_api_version": HARNESS_API_VERSION,
        "doctor_process": {
            "python": sys.executable,
            "role": "metadata_controller_only",
        },
        "status": aggregate(states) if states else "unknown",
        "selection": {"plugin_id": plugin_id, "capability": capability, "host": host},
        "plugins": selected,
        "cycles": [
            {"host": cycle[0][0], "plugins": [identity[1] for identity in cycle]}
            for cycle in sorted(cycles)
        ],
        "notes": [
            "Metadata readiness does not establish host enablement, runtime connectivity, or engineering correctness.",
            "Legacy core/HVAC descriptors are not generic manifests; run openstudio-si doctor for their existing contract.",
        ],
    }
