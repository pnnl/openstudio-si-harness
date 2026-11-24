"""Shared isolated Python fixture and selected-runtime profile helpers."""

import json
import os
import subprocess
import venv
from pathlib import Path

import pytest
from openstudio_si_harness.readiness import doctor


@pytest.fixture(scope="module")
def selected_python(tmp_path_factory):
    root = tmp_path_factory.mktemp("selected-python")
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    python = root / "bin/python"
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    site = Path(result.stdout.strip())
    metadata = site / "si_runtime_fixture-7.2.1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: si-runtime-fixture\nVersion: 7.2.1\n"
    )
    (site / "si_runtime_fixture.py").write_text(
        "raise RuntimeError('Target package must never be imported')\n"
    )
    return python


def profile(
    tmp_path,
    selected_python,
    dependencies,
    *,
    env=None,
    allow_version=False,
    limits=None,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    data = {
        "schema_version": "0.1",
        "host": "codex",
        "runtimes": {
            "selected": {
                "python": str(selected_python),
                "cwd": str(tmp_path),
                "path": [str(bin_dir)],
                "env": env or {},
            }
        },
        "bindings": [
            {
                "plugin": "consumer",
                "dependency": d["id"],
                "runtime": "selected",
                **(
                    {"allow_command_version": True}
                    if allow_version and d["kind"] == "command"
                    else {}
                ),
            }
            for d in dependencies
        ],
    }
    if limits:
        data["limits"] = limits
    path = tmp_path / "runtime.local.json"
    path.write_text(json.dumps(data))
    return path


def probe(root, path, **kwargs):
    return doctor([root], runtime_profile=path, check_runtime=True, **kwargs)


def tool(tmp_path, selected_python, body):
    path = tmp_path / "bin/version-tool"
    path.write_text(f"#!{selected_python}\n" + body + "\n")
    path.chmod(0o755)
    return path
