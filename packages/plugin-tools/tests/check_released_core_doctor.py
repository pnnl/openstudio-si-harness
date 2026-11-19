"""Compare the actual 0.4.3 doctor before/after adding third-party cache entries.

The baseline wheel and its independently pinned SHA-256 are mandatory. Never
substitute the current checkout or claim success when the release is unavailable.
Run with dependencies of that released wheel installed in this interpreter.
"""

import argparse
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import zipfile
from email.parser import Parser

from openstudio_si_harness.export import export_plugin

PROBE = r"""
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

baseline, workspace, generic = map(Path, sys.argv[1:4])
sys.path.insert(0, str(baseline))
import cli
import harness.plugins
assert Path(cli.__file__).resolve().is_relative_to(baseline)
assert Path(harness.plugins.__file__).resolve().is_relative_to(baseline)
assert metadata.version("openstudio-si") == "0.4.3"
os.environ.pop("CODEX_HOME", None)
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ["OPENSTUDIO_SI_DATA_DIR"] = str(workspace / "data")
home = workspace / "home"
home.mkdir()
with patch.object(Path, "home", return_value=home):
    paired = workspace / "paired"
    assert cli.main(["export", "marketplace", "--output-dir", str(paired),
                     "--workspace-root", str(baseline), "--runtime-mode", "marketplace"]) == 0
    hvac = paired / "openstudio-si-hvac"
    assert hvac.is_dir()
    for host, export_root in ((".claude", paired), (".codex", paired / "plugins")):
        cache = home / host / "plugins/cache/baseline/openstudio-si/0.4.3"
        shutil.copytree(export_root / "openstudio-si", cache)
    observations = {}
    for setup in ("core-only", "with-hvac"):
        if setup == "with-hvac":
            for host, export_root in ((".claude", paired), (".codex", paired / "plugins")):
                cache = home / host / "plugins/cache/baseline/openstudio-si-hvac/0.1.0"
                shutil.copytree(export_root / "openstudio-si-hvac", cache)
        def observe():
            result = {}
            modes = [("cached", None)]
            if setup == "with-hvac":
                modes.append(("explicit-hvac", hvac))
            for name, package in modes:
                payload = cli._doctor_payload(plugin_version="0.4.3", plugin_dir=package)
                result[name] = {key: payload[key] for key in
                                ("version", "core_ready", "mcp_ready", "ready", "plugins", "diagnostics")}
            return result
        before = observe()
        for host, export_host in ((".claude", "claude"), (".codex", "codex")):
            package = next((generic / export_host).iterdir())
            shutil.copytree(package, home / host / "plugins/cache/third-party" / package.name / "0.1.0")
        after = observe()
        assert before == after, json.dumps({"setup": setup, "before": before, "after": after}, indent=2)
        observations[setup] = {"before": before, "after": after}
        for host in (".claude", ".codex"):
            shutil.rmtree(home / host / "plugins/cache/third-party")
    (workspace / "observations.json").write_text(json.dumps(observations, indent=2))
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-wheel", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--example", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    actual = hashlib.sha256(args.core_wheel.read_bytes()).hexdigest()
    if actual != args.sha256:
        raise ValueError("Released-core wheel does not match the pinned SHA-256.")
    with tempfile.TemporaryDirectory(prefix="si-released-doctor-") as stage:
        root = Path(stage)
        baseline = root / "baseline"
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
                or metadata["Version"] != "0.4.3"
            ):
                raise ValueError(
                    "Baseline must be the released openstudio-si 0.4.3 wheel."
                )
            for name in names:
                if Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name:
                    raise ValueError("Unsafe baseline wheel member.")
            archive.extractall(baseline)
        generic = root / "generic"
        export_plugin(args.example, generic)
        workspace = root / "workspace"
        workspace.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                PROBE,
                str(baseline),
                str(workspace),
                str(generic),
            ],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=240,
        )
        report = {
            "baseline": "openstudio-si==0.4.3",
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
        print(f"Released core 0.4.3 doctor unchanged; pinned wheel {actual}")


if __name__ == "__main__":
    main()
