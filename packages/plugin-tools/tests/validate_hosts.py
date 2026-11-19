"""Read fresh wheel exports with the real hosts, without running an agent.

Run with Python from the installed-wheel test environment. Both CLIs must be
installed. Codex has no standalone plugin validator, so use its app-server
plugin/read loader and check the discovered skills and server names.
"""

import argparse
import json
from pathlib import Path
import queue
import shutil

import yaml
import subprocess
import tempfile
import threading

from openstudio_si_harness.export import export_plugin
from openstudio_si_harness.compatibility import satisfies


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
        while True:
            response = messages.get(timeout=30)
            if response.get("process_exited"):
                raise RuntimeError("Codex loader exited: " + "".join(errors)[-3000:])
            if response.get("id") == identity:
                if "error" in response:
                    raise RuntimeError(response["error"])
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


def dependency_fixtures(example: Path, destination: Path) -> list[Path]:
    """Generate private provider/consumer scenarios from the neutral example."""
    sources = []
    for variant in ("service-provider", "consumer"):
        source = destination / variant
        shutil.copytree(example, source)
        path = source / "openstudio-si-plugin.yaml"
        data = yaml.safe_load(path.read_text())
        data["id"] = f"fixture-{variant}"
        data["name"] = f"Fixture {variant}"
        skill = source / "skills/example-task"
        skill.rename(source / "skills" / f"{variant}-task")
        skill_file = source / "skills" / f"{variant}-task/SKILL.md"
        skill_file.write_text(
            skill_file.read_text().replace(
                "name: example-task", f"name: {variant}-task"
            )
        )
        if variant == "service-provider":
            (source / ".mcp.json").write_text(
                json.dumps(
                    {"mcpServers": {"example_service": {"command": "example-service"}}}
                )
            )
            data["dependencies"] = [
                dict(
                    id="service",
                    kind="mcp-server",
                    target="example_service",
                    required=True,
                    probe="mcp-tools",
                )
            ]
            data["capabilities"][0]["requires"] = ["service"]
        else:
            data["dependencies"] = [
                dict(
                    id="provider",
                    kind="plugin",
                    target="fixture-service-provider",
                    version_spec=">=0.1.0 <0.2.0",
                    required=True,
                    probe="metadata",
                ),
                dict(
                    id="service",
                    kind="mcp-server",
                    target="example_service",
                    provider="provider",
                    required=True,
                    probe="mcp-tools",
                ),
            ]
            data["capabilities"][0]["requires"] = ["provider", "service"]
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        sources.append(source)
    return sources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "example", type=Path, help="The minimal plugin source directory."
    )
    args = parser.parse_args()
    versions = [
        "0.4.3",
        "0.4.3+b.1",
        "0.4.4",
        "0.5.0",
        "0.5.0-rc.1",
        "0.5.0-rc.2",
        "0.5.1-rc.1",
        "0.4.3-rc.1",
        "1.0.0",
        "0.4.10",
    ]
    ranges = [
        ">=0.4.3 <0.5.0",
        ">=0.5.0-rc.1 <0.6.0",
        "<0.5.0",
        ">0.4.3",
        "<=0.5.0-rc.1",
        "=0.4.3",
        "0.4.3",
        ">=0.4.3-rc.0 <0.4.4",
        ">=0.4.3+b.1",
        " >=0.4.3  <0.5.0 ",
        ">=0.4.3 <0.5.0 >=0.4.4",
    ]
    vectors = [(version, expression) for version in versions for expression in ranges]
    assert len(vectors) == 110
    actual = subprocess.run(
        [
            "node",
            "-e",
            'const s=require("semver"); console.log(JSON.stringify(JSON.parse(process.argv[1]).map(([v,r])=>s.satisfies(v,r))))',
            json.dumps(vectors),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    expected = json.loads(actual.stdout)
    mismatches = [
        (v, r, wanted, satisfies(v, r))
        for (v, r), wanted in zip(vectors, expected)
        if wanted != satisfies(v, r)
    ]
    assert not mismatches, mismatches
    print("npm range parity: 110 cases passed", flush=True)

    with tempfile.TemporaryDirectory(prefix="si-host-validation-") as stage:
        root = Path(stage)
        entries = []
        plugins = {}
        example = args.example.resolve()
        sources = [example, *dependency_fixtures(example, root / "fixtures")]
        for source in sources:
            report = export_plugin(source, root / "exports" / source.name)
            identity = report["id"]
            plugins[identity] = report["inventory"]
            subprocess.run(
                ["claude", "plugin", "validate", report["packages"]["claude"]],
                check=True,
                timeout=60,
            )
            entries.append(
                {
                    "name": identity,
                    "source": {
                        "source": "local",
                        "path": f"./exports/{source.name}/codex/{identity}",
                    },
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                }
            )
        location = root / ".agents/plugins/marketplace.json"
        location.parent.mkdir(parents=True)
        location.write_text(
            json.dumps({"name": "si-wheel-validation", "plugins": entries})
        )
        validate_codex(root, plugins)


if __name__ == "__main__":
    main()
