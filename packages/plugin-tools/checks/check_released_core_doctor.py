"""Compare the actual 0.4.2 doctor before/after adding third-party cache entries.

The baseline wheel and its independently pinned SHA-256 are mandatory. Never
substitute the current checkout or claim success when the release is unavailable.
The controller uses current tooling. The probe installs the baseline wheel and
its dependencies into a fresh venv; it never imports the checkout.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import venv
import zipfile
from email.parser import Parser
from pathlib import Path

from openstudio_si_harness.export import export_plugin
from openstudio_si_harness.generator import init_project

PROBE = r"""
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

workspace, generic = map(Path, sys.argv[1:3])
import cli
import openstudio_si_mcp
baseline = Path(cli.__file__).resolve().parent
assert baseline.is_relative_to(Path(sys.prefix).resolve())
assert Path(openstudio_si_mcp.__file__).resolve().is_relative_to(baseline)
distributions = [d for d in metadata.distributions()
                 if d.metadata["Name"].replace("_", "-") == "openstudio-si"]
assert len(distributions) == 1
assert metadata.version("openstudio-si") == "0.4.2"
from openstudio_si_mcp.compatibility import package_version
assert package_version() == "0.4.2"
os.environ.pop("CODEX_HOME", None)
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ["OPENSTUDIO_SI_DATA_DIR"] = str(workspace / "data")
home = workspace / "home"
(workspace / "data/workspace").mkdir(parents=True)
home.mkdir()
with patch.object(Path, "home", return_value=home):
    paired = workspace / "paired"
    assert cli.main(["export", "marketplace", "--output-dir", str(paired),
                     "--workspace-root", str(baseline), "--runtime-mode", "marketplace"]) == 0
    # 0.4.2 predates the core/HVAC split: exercise only its shipped core API.
    for host, export_root in ((".claude", paired), (".codex", paired / "plugins")):
        cache = home / host / "plugins/cache/baseline/openstudio-si/0.4.2"
        shutil.copytree(export_root / "openstudio-si", cache)
    def observe():
        result = {}
        for name, options in (("default", {}), ("matching-plugin", {
                "plugin_version": "0.4.2", "plugin_contract_version": "6.0"})):
            payload = cli._doctor_payload(**options)
            assert payload["version"] == "0.4.2"
            result[name] = {key: payload[key] for key in (
                "version", "core_ready", "mcp_ready", "ready", "plugin_ready",
                "simulation_ready", "plugin_compatibility", "diagnostics")}
        return result
    before = observe()
    assert before["matching-plugin"]["mcp_ready"], json.dumps(before, indent=2)
    for host, export_host in ((".claude", "claude"), (".codex", "codex")):
        package = next((generic / export_host).iterdir())
        shutil.copytree(package, home / host / "plugins/cache/third-party" / package.name / "0.1.0")
    after = observe()
    assert before == after, json.dumps({"before": before, "after": after}, indent=2)
    observations = {
        "metadata_version": metadata.version("openstudio-si"),
        "package_version": package_version(),
        "baseline_prefix": sys.prefix,
        "cli_module": cli.__file__,
        "runtime_module": openstudio_si_mcp.__file__,
        "before": before, "after": after,
    }
    (workspace / "observations.json").write_text(json.dumps(observations, indent=2))
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-wheel", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--example", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    actual = hashlib.sha256(args.core_wheel.read_bytes()).hexdigest()
    if actual != args.sha256:
        raise ValueError("Released-core wheel does not match the pinned SHA-256.")
    with tempfile.TemporaryDirectory(prefix="si-released-doctor-") as stage:
        root = Path(stage)
        with zipfile.ZipFile(args.core_wheel) as archive:
            names = archive.namelist()
            metadata_files = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_files) != 1:
                raise ValueError("Expected one distribution in the baseline wheel.")
            metadata = Parser().parsestr(archive.read(metadata_files[0]).decode())
            if (
                metadata["Name"].replace("_", "-") != "openstudio-si"
                or metadata["Version"] != "0.4.2"
            ):
                raise ValueError(
                    "Baseline must be the released openstudio-si 0.4.2 wheel."
                )
            for name in names:
                if Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name:
                    raise ValueError("Unsafe baseline wheel member.")
        generic = root / "generic"
        example = args.example
        if example is None:
            init_project(
                root / "generated",
                identity="example-plugin",
                license_identifier="BSD-3-Clause",
            )
            example = root / "generated/plugin"
        export_plugin(example, generic)
        workspace = root / "workspace"
        workspace.mkdir()
        environment = root / "baseline-env"
        venv.EnvBuilder(
            with_pip=True, system_site_packages=False, symlinks=os.name != "nt"
        ).create(environment)
        baseline_python = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        subprocess.run(
            [
                str(baseline_python),
                "-I",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--index-url",
                "https://pypi.org/simple",
                "--only-binary=:all:",
                str(args.core_wheel.resolve()),
            ],
            check=True,
            timeout=300,
        )
        probe_env = dict(os.environ)
        # Native subprocess probes must resolve the baseline console script too.
        probe_env["PATH"] = (
            str(baseline_python.parent) + os.pathsep + probe_env.get("PATH", "")
        )
        result = subprocess.run(
            [
                str(baseline_python),
                "-I",
                "-c",
                PROBE,
                str(workspace),
                str(generic),
            ],
            env=probe_env,
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=240,
        )
        report = {
            "baseline": "openstudio-si==0.4.2",
            "sha256": actual,
            "passed": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        observations = workspace / "observations.json"
        if observations.exists():
            report["observations"] = json.loads(observations.read_text())
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        if result.returncode:
            raise RuntimeError(
                f"Released-core doctor regression failed; see {args.report}"
            )
        print(f"Released core 0.4.2 doctor unchanged; pinned wheel {actual}")


if __name__ == "__main__":
    main()
