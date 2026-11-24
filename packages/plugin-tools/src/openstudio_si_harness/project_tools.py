"""Shared deterministic project, host and marketplace-candidate commands."""

import hashlib
import json
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile

from . import __version__
from .export import DESCRIPTOR, export_plugin, validate_export
from .manifest import load_plugin


def check_project(root: Path, require_tracked: bool = False) -> dict:
    root = root.resolve()
    plugin = load_plugin(root / "plugin")
    from .evaluation.contracts import load_cases

    cases = load_cases(root / "plugin" / plugin["manifest"]["evaluation"]["cases"], root / "plugin")
    if require_tracked:
        selected = [*plugin["files"], Path("openstudio-si-plugin.yaml")]
        if (root / "plugin/.mcp.json").exists():
            selected.append(Path(".mcp.json"))
        observed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True
        ).stdout
        tracked = set(observed.decode("utf-8", "surrogateescape").split("\0"))
        missing = [
            (Path("plugin") / path).as_posix()
            for path in selected
            if (Path("plugin") / path).as_posix() not in tracked
        ]
        if missing:
            raise ValueError(
                f"Exported source files must be tracked by git: {sorted(missing)}"
            )
    return {
        "id": plugin["manifest"]["id"],
        "version": plugin["manifest"]["version"],
        "cases": len(cases),
        "inventory": plugin["inventory"],
    }


def validate_codex(marketplace, plugins):
    process = subprocess.Popen(
        [
            "codex",
            "app-server",
            "--stdio",
            "-c",
            f"sqlite_home={json.dumps(str(marketplace / 'state'))}",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=marketplace,
    )
    messages = queue.Queue()
    errors = []

    def read_errors():
        for line in process.stderr:
            errors.append(line)

    threading.Thread(target=read_errors, daemon=True).start()

    def read():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass
        messages.put({"process_exited": True})

    threading.Thread(target=read, daemon=True).start()

    def rpc(identity, method, params):
        process.stdin.write(
            json.dumps(dict(id=identity, method=method, params=params)) + "\n"
        )
        process.stdin.flush()
        deadline = time.monotonic() + 30
        while True:
            try:
                response = messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise ValueError("Codex plugin loader timed out.") from exc
            if response.get("process_exited"):
                raise ValueError("Codex loader exited: " + "".join(errors)[-3000:])
            if response.get("id") == identity:
                if "error" in response:
                    raise ValueError(response["error"])
                return response["result"]

    try:
        rpc(
            1,
            "initialize",
            {
                "clientInfo": {"name": "si-plugin-validation", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        process.stdin.write('{"method":"initialized","params":{}}\n')
        process.stdin.flush()
        for index, (identity, expected) in enumerate(plugins.items(), 2):
            result = rpc(
                index,
                "plugin/read",
                {
                    "marketplacePath": str(
                        marketplace / ".agents/plugins/marketplace.json"
                    ),
                    "pluginName": identity,
                },
            )["plugin"]
            assert len(result["skills"]) == expected["skill_count"], result
            assert len(result["mcpServers"]) == expected["mcp_server_count"], result
            print(f"Codex loaded {identity}: {expected}", flush=True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def check_hosts(report_path: Path) -> dict:
    report = json.loads(report_path.read_text())
    if set(report["packages"]) != {"claude", "codex"}:
        raise ValueError("Supply both host packages.")
    for host, path in report["packages"].items():
        checked = validate_export(Path(path))
        if (
            checked["manifest"]["id"] != report["id"]
            or checked["inventory"] != report["inventory"]
        ):
            raise ValueError("Export report disagrees with package contents.")
    subprocess.run(
        ["claude", "plugin", "validate", report["packages"]["claude"]],
        check=True,
        timeout=60,
    )
    with tempfile.TemporaryDirectory(prefix="plugin-host-check-") as temporary:
        root = Path(temporary)
        shutil.copytree(report["packages"]["codex"], root / "plugins" / report["id"])
        location = root / ".agents/plugins/marketplace.json"
        location.parent.mkdir(parents=True)
        location.write_text(
            json.dumps(
                {
                    "name": "generated-plugin-check",
                    "plugins": [
                        {
                            "name": report["id"],
                            "source": {
                                "source": "local",
                                "path": "./plugins/" + report["id"],
                            },
                            "policy": {
                                "installation": "AVAILABLE",
                                "authentication": "ON_INSTALL",
                            },
                        }
                    ],
                }
            )
        )
        validate_codex(root, {report["id"]: report["inventory"]})
    return {"id": report["id"], "hosts": list(report["packages"])}


def package_release(
    root: Path, report: Path, *, tag: str, sha: str, repository: str
) -> dict:
    root = root.resolve()
    manifest = load_plugin(root / "plugin")["manifest"]
    if tag not in {manifest["version"], "v" + manifest["version"]}:
        raise ValueError("Release tag must match the manifest version.")
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise ValueError(
            "Publishing requires a repository identity and immutable source SHA."
        )
    exported = json.loads(report.read_text())
    if (
        exported["id"] != manifest["id"]
        or exported["version"] != manifest["version"]
        or set(exported["packages"]) != {"claude", "codex"}
    ):
        raise ValueError(
            "Export report must identify both hosts for this source version."
        )
    output = root / "dist/release"
    if output.exists():
        raise ValueError(
            "Release output already exists; inspect it before creating another build."
        )
    packages = {}
    for host, location in exported["packages"].items():
        package = Path(location)
        checked = validate_export(package)["manifest"]
        if checked != manifest:
            raise ValueError(
                "Exported manifest differs from the current source manifest."
            )
        packages[host] = package
    with tempfile.TemporaryDirectory(prefix="plugin-release-verify-") as temporary:
        expected = export_plugin(root / "plugin", Path(temporary) / "expected")
        for host, package in packages.items():
            actual_descriptor = json.loads((package / DESCRIPTOR).read_text())
            expected_descriptor = json.loads(
                (Path(expected["packages"][host]) / DESCRIPTOR).read_text()
            )
            if actual_descriptor != expected_descriptor:
                raise ValueError(
                    "Exported content differs from the current source; export again before releasing."
                )
    output.mkdir(parents=True)
    record = {
        "format": "openstudio-si-plugin-release/0.1",
        "id": manifest["id"],
        "version": manifest["version"],
        "repository": repository,
        "source_sha": sha,
        "tag": tag,
        "tool_version": __version__,
        "evaluation": {"status": "not_provided", "score_policy": "advisory"},
        "destination": {
            "repository": "pnnl/openstudio-si-plugins",
            "operation": "plugin-scoped-pull-request",
            "status": "importer-pending",
        },
        "packages": {},
    }
    for host, package in packages.items():
        archive = output / f"{manifest['id']}-{manifest['version']}-{host}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for file in sorted(package.rglob("*")):
                if file.is_file():
                    item = zipfile.ZipInfo(
                        manifest["id"] + "/" + file.relative_to(package).as_posix(),
                        date_time=(1980, 1, 1, 0, 0, 0),
                    )
                    item.create_system = 3
                    item.external_attr = (
                        0o100755 if file.stat().st_mode & 0o111 else 0o100644
                    ) << 16
                    item.compress_type = zipfile.ZIP_DEFLATED
                    bundle.writestr(item, file.read_bytes())
        record["packages"][host] = {
            "archive": archive.name,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "descriptor_sha256": hashlib.sha256(
                (package / DESCRIPTOR).read_bytes()
            ).hexdigest(),
        }
    (output / "release.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def prepare_hosts(prefix: Path, output_dir: Path) -> dict:
    """Use Claude's packaged native binary without executing npm install scripts."""
    import platform
    import shlex
    import sys

    architecture = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x64",
        "amd64": "x64",
    }.get(platform.machine().lower())
    if sys.platform not in {"darwin", "linux"} or architecture is None:
        raise ValueError(
            "Native host preparation currently supports macOS/Linux arm64 and x64."
        )
    root = prefix.resolve()
    package = (
        root
        / "node_modules/@anthropic-ai"
        / f"claude-code-{sys.platform}-{architecture}"
    )
    metadata = json.loads((package / "package.json").read_text())
    if metadata.get("version") != "2.1.309":
        raise ValueError(
            "Unexpected Claude native package version; update the central host policy first."
        )
    executable = (package / "claude").resolve()
    if (
        not executable.is_relative_to(root)
        or not executable.is_file()
        or not executable.stat().st_mode & 0o111
    ):
        raise ValueError("Expected an executable Claude binary inside the npm prefix.")
    output_dir.mkdir(parents=True, exist_ok=True)
    wrapper = output_dir / "claude"
    with wrapper.open("x", encoding="utf-8") as stream:
        stream.write("#!/bin/sh\nexec " + shlex.quote(str(executable)) + ' "$@"\n')
    wrapper.chmod(0o755)
    return {"bin": str(output_dir.resolve()), "claude": str(executable)}
