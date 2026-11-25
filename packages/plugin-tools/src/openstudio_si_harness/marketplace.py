"""Verify public release candidates and update only their owned marketplace paths.

No plugin Python, commands, MCP servers or LLMs are executed here. Native host
conformance is a separate read-only workflow step before a PR can be published.
"""

import hashlib
import json
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .compatibility import satisfies, semver
from .content import excluded
from .discovery import read_json as strict_json
from .export import DESCRIPTOR, HOSTS, export_plugin, validate_export

RESERVED = {"openstudio-si", "openstudio-si-hvac"}
REGISTRY = ".openstudio-si/registry.json"
RECEIPTS = ".openstudio-si/releases"
CATALOGS = {
    "claude": ".claude-plugin/marketplace.json",
    "codex": ".agents/plugins/marketplace.json",
}
IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_FILES = 10000


class ReleaseError(ValueError):
    """A fixed, non-sensitive failure code for central release diagnostics."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def read_json(path):
    return strict_json(Path(path))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def relative(value):
    if not isinstance(value, str):
        raise ValueError("Release paths must be strings.")  # noqa: TRY004
    path = PurePosixPath(value)
    if (
        not value
        or value == "."
        or path.is_absolute()
        or path.as_posix() != value
        or any(p in {"..", ".git"} for p in path.parts)
        or "\\" in value
    ):
        raise ValueError("Unsafe release path.")
    return Path(value)


def load_registry(root):
    registry = read_json(owned(root, REGISTRY))
    if (
        not isinstance(registry, dict)
        or set(registry) != {"schema_version", "plugins"}
        or registry["schema_version"] != "0.1"
        or not isinstance(registry["plugins"], dict)
    ):
        raise ValueError("Invalid marketplace registry.")
    for identity, entry in registry["plugins"].items():
        if (
            not IDENTIFIER.fullmatch(identity)
            or identity in RESERVED
            or not isinstance(entry, dict)
            or not {"repository", "owner", "plugin_path"}.issubset(entry)
            or set(entry) - {"repository", "owner", "plugin_path", "enabled"}
            or type(entry.get("enabled", True)) is not bool
        ):
            raise ValueError("Invalid or reserved registry identity.")
        if (
            not isinstance(entry["repository"], str)
            or not REPOSITORY.fullmatch(entry["repository"])
            or entry["repository"].split("/")[1] in {".", ".."}
            or not isinstance(entry["owner"], str)
            or not IDENTIFIER.fullmatch(entry["owner"])
        ):
            raise ValueError(
                "Registry requires a GitHub repository and manifest owner ID."
            )
        relative(entry["plugin_path"])
    return registry


def package_path(root, host, identity):
    if not IDENTIFIER.fullmatch(identity):
        raise ValueError("Invalid plugin identity.")
    return owned(root, identity if host == "claude" else "plugins/" + identity)


def owned(root, name):
    path = Path(root)
    for part in relative(name).parts:
        path /= part
        if path.is_symlink():
            raise ValueError("Marketplace paths cannot traverse symlinks.")
    return path


def copy_marketplace(root, destination):
    shutil.copytree(
        root,
        destination,
        symlinks=True,
        ignore=lambda _path, names: [name for name in names if excluded(name)],
    )


def merge_entries(catalog, replacements):
    if not isinstance(catalog, dict) or not isinstance(catalog.get("plugins"), list):
        raise ValueError("Invalid marketplace catalog.")  # noqa: TRY004
    result = json.loads(json.dumps(catalog))
    identities = [entry.get("name") for entry in result["plugins"]]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate marketplace identities.")
    for entry in replacements:
        if entry["name"] in identities:
            result["plugins"][identities.index(entry["name"])] = entry
        else:
            result["plugins"].append(entry)
            identities.append(entry["name"])
    return result


def catalog_entry(manifest, host):
    identity = manifest["id"]
    if host == "claude":
        return {
            "name": identity,
            "description": manifest["description"],
            "version": manifest["version"],
            "source": "./" + identity,
            "category": "engineering",
        }
    return {
        "name": identity,
        "source": {"source": "local", "path": "./plugins/" + identity},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Engineering",
    }


def unpack_package(archive, destination, identity):
    """Extract bounded regular ZIP entries below a single verified plugin root."""
    archive, destination = Path(archive), Path(destination)
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Candidate archive exceeds the compressed size limit.")
    seen, total = set(), 0
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_FILES:
            raise ValueError("Candidate has too many archive entries.")
        for entry in entries:
            name = entry.filename.rstrip("/") if entry.is_dir() else entry.filename
            path = relative(name)
            if (
                not path.parts
                or path.parts[0] != identity
                or path in seen
                or entry.flag_bits & 1
            ):
                raise ValueError("Invalid candidate archive entry.")
            seen.add(path)
            mode = entry.external_attr >> 16
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR} or (
                entry.is_dir() and stat.S_IFMT(mode) == stat.S_IFREG
            ):
                raise ValueError("Archive links or special files are forbidden.")
            total += entry.file_size
            if total > MAX_EXPANDED:
                raise ValueError("Candidate exceeds the expanded size limit.")
            target = destination / path
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(0o755 if mode & 0o111 else 0o644)
    return destination / identity


def validate_record(record, identity, entry, source_sha):
    required = {
        "format",
        "id",
        "version",
        "repository",
        "source_sha",
        "tag",
        "tool_version",
        "evaluation",
        "destination",
        "packages",
    }
    if (
        not isinstance(record, dict)
        or set(record) != required
        or record["format"] != "openstudio-si-plugin-release/0.1"
    ):
        raise ValueError("Invalid plugin release record.")
    if any(
        not isinstance(record[name], str)
        for name in ("id", "version", "repository", "source_sha", "tag", "tool_version")
    ):
        raise ValueError("Release identity fields must be strings.")
    if semver(record["version"]).prerelease:
        raise ReleaseError(
            "prerelease_unsupported", "Only stable releases are supported."
        )
    semver(record["tool_version"])
    if (
        record["id"] != identity
        or record["repository"].lower() != entry["repository"].lower()
        or not SHA.fullmatch(source_sha)
        or record["source_sha"] != source_sha
        or record["tag"] not in {record["version"], "v" + record["version"]}
    ):
        raise ReleaseError(
            "tag_commit",
            "Release identity, tag or source commit does not match the registered source.",
        )
    if record["evaluation"] != {"status": "not_provided", "score_policy": "advisory"}:
        raise ValueError(
            "Unsupported evaluation attachment; no judge score is a release gate."
        )
    destination = record["destination"]
    if (
        not isinstance(destination, dict)
        or set(destination) != {"repository", "operation", "status"}
        or destination["repository"] != "pnnl/openstudio-si-plugins"
        or destination["operation"] != "plugin-scoped-pull-request"
        or destination["status"] not in {"importer-pending", "awaiting-import"}
    ):
        raise ValueError("Invalid candidate destination.")
    if not isinstance(record["packages"], dict) or set(record["packages"]) != set(
        HOSTS
    ):
        raise ValueError("Release must supply both host packages.")
    for host, package in record["packages"].items():
        expected = f"{identity}-{record['version']}-{host}.zip"
        if (
            not isinstance(package, dict)
            or set(package) != {"archive", "sha256", "descriptor_sha256"}
            or package["archive"] != expected
            or any(
                not isinstance(package[key], str)
                or not re.fullmatch(r"[0-9a-f]{64}", package[key])
                for key in ("sha256", "descriptor_sha256")
            )
        ):
            raise ValueError("Invalid archive identity or digest.")


def verify_release(
    record_path, assets, source_plugin, *, identity, entry, source_sha, output
):
    """Check assets against immutable source, never trusting uploaded hashes alone."""
    record = read_json(record_path)
    validate_record(record, identity, entry, source_sha)
    output = Path(output)
    if output.exists():
        raise ValueError("Verification output must be new.")
    with tempfile.TemporaryDirectory(prefix="si-release-check-") as temporary:
        stage = Path(temporary)
        expected = export_plugin(Path(source_plugin), stage / "expected")
        packages = {}
        for host in HOSTS:
            item = record["packages"][host]
            archive = Path(assets) / item["archive"]
            if archive.stat().st_size > MAX_ARCHIVE:
                raise ValueError("Candidate archive exceeds the compressed size limit.")
            if hashlib.sha256(archive.read_bytes()).hexdigest() != item["sha256"]:
                raise ReleaseError("archive_hash", "Release archive hash mismatch.")
            package = unpack_package(archive, stage / host, identity)
            if (
                hashlib.sha256((package / DESCRIPTOR).read_bytes()).hexdigest()
                != item["descriptor_sha256"]
            ):
                raise ReleaseError(
                    "descriptor_hash", "Release descriptor hash mismatch."
                )
            actual = validate_export(package)["manifest"]
            if (
                actual["id"] != identity
                or actual["version"] != record["version"]
                or actual["owner"]["id"] != entry["owner"]
            ):
                raise ReleaseError(
                    "owner", "Manifest identity/owner does not match registration."
                )
            observed = read_json(package / DESCRIPTOR)
            wanted = read_json(Path(expected["packages"][host]) / DESCRIPTOR)
            # Tool releases may differ while the stable exporter contract and
            # content remain identical. Every other field must match source.
            wanted["tool_version"] = record["tool_version"]
            if observed != wanted:
                raise ReleaseError(
                    "source_mismatch",
                    "Uploaded package does not match the immutable source export.",
                )
            for file in wanted["files"]:
                expected_file = Path(expected["packages"][host]) / file
                if bool((package / file).stat().st_mode & 0o111) != bool(
                    expected_file.stat().st_mode & 0o111
                ):
                    raise ValueError(
                        "Uploaded executable permissions differ from source."
                    )
            packages[host] = package
        output.mkdir(parents=True)
        for host in HOSTS:
            shutil.copytree(packages[host], output / host / identity)
    report = {
        "id": identity,
        "version": record["version"],
        "inventory": actual["inventory"],
        "packages": {host: str(output / host / identity) for host in HOSTS},
    }
    write_json(output / "export-report.json", report)
    write_json(
        output / "receipt.json",
        {
            "format": "openstudio-si-marketplace-release/0.1",
            "record_sha256": digest(record),
            "record": record,
        },
    )
    return report


def inventory(root):
    """Build the complete host-scoped static graph, including legacy core/HVAC."""
    root = Path(root)
    catalog = {}
    for host, location in CATALOGS.items():
        data = read_json(owned(root, location))
        if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
            raise ValueError("Missing marketplace plugin list.")  # noqa: TRY004
        for entry in data["plugins"]:
            identity = entry["name"]
            key = host, identity
            if key in catalog:
                raise ValueError("Duplicate marketplace plugin.")
            expected_source = (
                "./" + identity
                if host == "claude"
                else {"source": "local", "path": "./plugins/" + identity}
            )
            if entry["source"] != expected_source:
                raise ValueError(
                    "Marketplace source must be the plugin's owned local path."
                )
            package = package_path(root, host, identity)
            if (package / DESCRIPTOR).is_file():
                loaded = validate_export(package)
                manifest, servers = loaded["manifest"], loaded["mcp_servers"]
                if manifest["id"] != identity:
                    raise ValueError("Catalog identity differs from package.")
                dependencies = manifest["dependencies"]
                version = manifest["version"]
            elif identity in RESERVED:
                metadata = read_json(owned(package, f".{host}-plugin/plugin.json"))
                if metadata["name"] != identity:
                    raise ValueError("Legacy plugin identity mismatch.")
                version = metadata["version"]
                dependencies = [
                    {
                        "id": item["name"],
                        "kind": "plugin",
                        "target": item["name"],
                        "version_spec": item["version"],
                        "required": True,
                    }
                    for item in metadata.get("dependencies", [])
                ]
                descriptor = owned(package, "openstudio-si-plugin.json")
                if identity == "openstudio-si-hvac" and descriptor.is_file():
                    legacy = read_json(descriptor)
                    dependencies = [
                        {
                            "id": "core",
                            "kind": "plugin",
                            "target": "openstudio-si",
                            "version_spec": f">={legacy['core_min']} <{legacy['core_before']}",
                            "required": True,
                        }
                    ]
                config = owned(package, ".mcp.json")
                servers = (
                    read_json(config).get("mcpServers", {}) if config.exists() else {}
                )
                manifest = None
            else:
                raise ValueError(
                    "Unrecognized plugin package; generic exports need descriptors."
                )
            semver(version)
            if host == "claude" and entry.get("version") != version:
                raise ValueError("Catalog version differs from package.")
            catalog[key] = {
                "version": version,
                "dependencies": dependencies,
                "servers": servers,
                "manifest": manifest,
            }
    if {identity for host, identity in catalog if host == "claude"} != {
        identity for host, identity in catalog if host == "codex"
    }:
        raise ValueError("Host catalogs must contain the same plugin identities.")
    for host, identity in catalog:
        if (
            catalog[host, identity]["version"]
            != catalog["codex" if host == "claude" else "claude", identity]["version"]
        ):
            raise ValueError("Host plugin versions disagree.")
    return catalog


def validate_marketplace(root, *, check_receipts=True):
    root = Path(root)
    registry = load_registry(root)
    catalog = inventory(root)
    warnings = []
    for (host, identity), plugin in catalog.items():
        for dependency in plugin["dependencies"]:
            if dependency["kind"] != "plugin":
                continue
            provider = catalog.get((host, dependency["target"]))
            compatible = provider and (
                not dependency.get("version_spec")
                or satisfies(provider["version"], dependency["version_spec"])
            )
            if not compatible:
                if dependency["required"]:
                    raise ValueError(
                        f"Unsatisfied {host} dependency: {identity} -> {dependency['target']}"
                    )
                warnings.append(
                    {
                        "host": host,
                        "plugin": identity,
                        "dependency": dependency["target"],
                        "reason": "optional_dependency_unsatisfied",
                    }
                )
        for dependency in plugin["dependencies"]:
            if dependency["kind"] != "mcp-server" or not dependency.get("provider"):
                continue
            reference = next(
                d for d in plugin["dependencies"] if d["id"] == dependency["provider"]
            )
            provider = catalog.get((host, reference["target"]))
            if dependency["required"] and (
                not provider or dependency["target"] not in provider["servers"]
            ):
                raise ValueError(
                    "Required cross-plugin MCP server is not declared by its provider."
                )
        if identity not in RESERVED and check_receipts:
            if identity not in registry["plugins"]:
                raise ValueError("Plugin is not registered.")
            receipt = read_json(owned(root, RECEIPTS + "/" + identity + ".json"))
            record = receipt["record"]
            validate_record(
                record, identity, registry["plugins"][identity], record["source_sha"]
            )
            if (
                receipt.get("format") != "openstudio-si-marketplace-release/0.1"
                or receipt.get("record_sha256") != digest(record)
                or record["version"] != plugin["version"]
                or plugin["manifest"]["owner"]["id"]
                != registry["plugins"][identity]["owner"]
            ):
                raise ValueError(
                    "Release receipt does not match the current catalog/registration."
                )
            package = package_path(root, host, identity)
            if (
                hashlib.sha256((package / DESCRIPTOR).read_bytes()).hexdigest()
                != record["packages"][host]["descriptor_sha256"]
            ):
                raise ValueError("Installed package differs from its release receipt.")
    visited, active = set(), set()

    def visit(key):
        if key in active:
            raise ValueError("Required plugin dependency cycle detected.")
        if key in visited:
            return
        active.add(key)
        for dep in catalog[key]["dependencies"]:
            if dep["kind"] == "plugin" and dep["required"]:
                visit((key[0], dep["target"]))
        active.remove(key)
        visited.add(key)

    for key in catalog:
        visit(key)
    return {"plugins": len(catalog) // 2, "warnings": warnings}


def stage_release(marketplace, verified, output, *, base_sha):
    """Prepare a plugin-scoped overlay; no remote or git writes occur."""
    root, verified, output = Path(marketplace), Path(verified), Path(output)
    if output.exists() or not SHA.fullmatch(base_sha):
        raise ValueError("Use new staging output and a full marketplace commit SHA.")
    receipt = read_json(verified / "receipt.json")
    record = receipt["record"]
    identity = record["id"]
    registration = load_registry(root)["plugins"][identity]
    validate_record(record, identity, registration, record["source_sha"])
    current = owned(root, RECEIPTS + "/" + identity + ".json")
    if current.exists():
        previous = read_json(current)
        if previous["record"]["version"] == record["version"]:
            if previous != receipt:
                raise ValueError(
                    "Released versions are immutable; bump the plugin version."
                )
            return {"id": identity, "status": "unchanged"}
        if semver(previous["record"]["version"]) > semver(record["version"]):
            return {"id": identity, "status": "older_release"}
    with tempfile.TemporaryDirectory(prefix="si-catalog-proposal-") as temporary:
        proposal = Path(temporary) / "marketplace"
        copy_marketplace(root, proposal)
        for host in HOSTS:
            package = package_path(proposal, host, identity)
            if package.exists():
                shutil.rmtree(package)
            shutil.copytree(verified / host / identity, package)
            location = CATALOGS[host]
            merged = merge_entries(
                read_json(proposal / location),
                [catalog_entry(read_json(package / DESCRIPTOR)["manifest"], host)],
            )
            write_json(proposal / location, merged)
        write_json(proposal / RECEIPTS / (identity + ".json"), receipt)
        validation = validate_marketplace(proposal)
        output.mkdir(parents=True)
        overlay = output / "overlay"
        paths = [
            identity,
            "plugins/" + identity,
            *CATALOGS.values(),
            RECEIPTS + "/" + identity + ".json",
        ]
        for name in paths:
            source, target = proposal / name, overlay / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        plan = {
            "format": "openstudio-si-import-plan/0.1",
            "id": identity,
            "version": record["version"],
            "base_sha": base_sha,
            "record_sha256": receipt["record_sha256"],
            "paths": paths,
            "branch": f"automated/plugin/{identity}-v{record['version']}",
            "validation": validation,
            "native_hosts_verified": False,
            "files": {
                path.relative_to(overlay)
                .as_posix(): hashlib.sha256(path.read_bytes())
                .hexdigest()
                for path in overlay.rglob("*")
                if path.is_file()
            },
            "executables": [
                path.relative_to(overlay).as_posix()
                for path in overlay.rglob("*")
                if path.is_file() and path.stat().st_mode & 0o111
            ],
        }
        write_json(output / "plan.json", plan)
    return {**plan, "status": "candidate"}


def sync_core(generated, marketplace):
    """Preflight the complete proposal, then update only the reserved exporters."""
    generated, marketplace = Path(generated), Path(marketplace)
    inventory(generated)
    paths = [
        *CATALOGS.values(),
        "openstudio-si",
        "openstudio-si-hvac",
        "plugins/openstudio-si",
        "plugins/openstudio-si-hvac",
    ]
    for name in paths:
        owned(marketplace, name)
    with tempfile.TemporaryDirectory(prefix="si-core-proposal-") as temporary:
        proposal = Path(temporary) / "marketplace"
        copy_marketplace(marketplace, proposal)
        for host, location in CATALOGS.items():
            source = read_json(owned(generated, location))
            entries = [
                entry for entry in source["plugins"] if entry["name"] in RESERVED
            ]
            if {entry["name"] for entry in entries} != RESERVED:
                raise ValueError(
                    "Core export must contain core and HVAC for both hosts."
                )
            target = owned(proposal, location)
            merged = merge_entries(
                read_json(target) if target.exists() else {**source, "plugins": []},
                entries,
            )
            if "version" in source:
                merged["version"] = source["version"]
            for identity in RESERVED:
                source_package = package_path(generated, host, identity)
                if any(path.is_symlink() for path in source_package.rglob("*")):
                    raise ValueError("Generated core packages cannot contain symlinks.")
                package = package_path(proposal, host, identity)
                if package.exists():
                    shutil.rmtree(package)
                shutil.copytree(source_package, package)
            write_json(target, merged)
        # Older marketplaces can receive their first split export before the
        # registry workflow is deployed; do not write this synthetic registry.
        if not (proposal / REGISTRY).exists():
            write_json(proposal / REGISTRY, {"schema_version": "0.1", "plugins": {}})
        validation = validate_marketplace(proposal)
        for name in paths:
            source, target = owned(proposal, name), owned(marketplace, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
    for name in [
        "README.md",
        "INSTALL.claude.md",
        "INSTALL.codex.md",
    ]:
        if (generated / name).exists():
            # Root landing/install docs belong to marketplace maintainers.
            # Refresh core-owned copies and seed root files only on first setup.
            target = owned(marketplace, "docs/core/" + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(generated / name, target)
            if name == "README.md":
                target.write_text(
                    target.read_text().replace(
                        "](.generated.json)", "](../../.generated.json)"
                    )
                )
            root_doc = owned(marketplace, name)
            if not root_doc.exists():
                shutil.copy2(generated / name, root_doc)
    if (generated / ".generated.json").exists():
        shutil.copy2(
            generated / ".generated.json", owned(marketplace, ".generated.json")
        )
    return {"updated_plugins": sorted(RESERVED), "validation": validation}
