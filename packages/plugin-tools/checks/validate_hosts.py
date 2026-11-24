"""Read fresh wheel exports with the real hosts, without running an agent.

Run with Python from the installed-wheel test environment. Both CLIs must be
installed. Codex has no standalone plugin validator, so use its app-server
plugin/read loader and check the discovered skills and server names.
"""

import argparse
import json
from pathlib import Path
import shutil

import yaml
import subprocess
import tempfile

from openstudio_si_harness.export import export_plugin
from openstudio_si_harness.compatibility import satisfies
from openstudio_si_harness.generator import init_project
from openstudio_si_harness.manifest import load_plugin
from openstudio_si_harness.project_tools import validate_codex
import sys


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
        skill = next((source / "skills").iterdir())
        skill.rename(source / "skills" / f"{variant}-task")
        skill_file = source / "skills" / f"{variant}-task/SKILL.md"
        skill_file.write_text(
            skill_file.read_text().replace(
                f"name: {skill.name}", f"name: {variant}-task"
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
        "example",
        nargs="?",
        type=Path,
        help="Optional source; otherwise generate a starter with and without an MCP example.",
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
        generated = []
        if args.example is None:
            for with_mcp in (False, True):
                project = root / (
                    "generated-with-mcp" if with_mcp else "generated-basic"
                )
                identity = "example-mcp-plugin" if with_mcp else "example-plugin"
                init_project(
                    project,
                    identity=identity,
                    with_mcp=with_mcp,
                    license_identifier="BSD-3-Clause",
                )
                generated.append(project)
            example = generated[0] / "plugin"
            sources = [project / "plugin" for project in generated]
        else:
            example = args.example.resolve()
            sources = [example]
        sources.extend(dependency_fixtures(example, root / "fixtures"))
        reports = {}
        for source in sources:
            source_id = load_plugin(source)["manifest"]["id"]
            report = export_plugin(source, root / "exports" / source_id)
            reports[source.resolve()] = report
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
                        "path": f"./exports/{identity}/codex/{identity}",
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
        for project in generated:
            report_file = root / (project.name + "-export.json")
            report_file.write_text(json.dumps(reports[(project / "plugin").resolve()]))
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "openstudio_si_harness.cli",
                    "plugin",
                    "check-hosts",
                    str(report_file),
                ],
                check=True,
                timeout=90,
            )


if __name__ == "__main__":
    main()
