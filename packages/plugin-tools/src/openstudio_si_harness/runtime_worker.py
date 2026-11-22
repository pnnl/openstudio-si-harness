"""Stdlib-only metadata worker executed by the explicitly selected Python.

No harness installation is needed in that environment. Never import target packages
or emit environment values, raw errors or command output.
"""

import importlib.metadata
import json
import os
import shutil
import sys
import sysconfig
from pathlib import Path


def distribution_metadata(target, explicit_paths):
    """Read metadata without site initialization or .pth execution."""
    if explicit_paths is not None:
        paths = explicit_paths
        complete = True
        scope = "declared_metadata_paths"
    else:
        prefix = Path(os.path.abspath(sys.executable)).parent.parent
        cfg = prefix / "pyvenv.cfg"
        base = sysconfig.get_paths()
        venv = cfg.is_file()
        # Before Python 3.14, -S leaves sys.prefix at the base interpreter.
        # Derive venv directories from the executable/pyvenv.cfg, not sys.prefix.
        selected = (
            sysconfig.get_paths(
                scheme="posix_prefix",
                vars={"base": str(prefix), "platbase": str(prefix)},
            )
            if venv
            else base
        )
        paths = list(dict.fromkeys(selected[key] for key in ("purelib", "platlib")))
        include_system = venv and any(
            line.strip().lower().replace(" ", "") == "include-system-site-packages=true"
            for line in cfg.read_text().splitlines()
        )
        if include_system:
            paths.extend(
                path for path in (base["purelib"], base["platlib"]) if path not in paths
            )
        complete = (
            venv
            and not include_system
            and not any(list(Path(path).glob("*.pth")) for path in paths)
        )
        scope = "venv_metadata_paths" if venv else "base_metadata_paths"
    versions = sorted(
        {
            dist.version
            for dist in importlib.metadata.distributions(name=target, path=paths)
        }
    )
    return {
        "versions": versions,
        "search_complete": complete,
        "metadata_scope": scope,
        "metadata_paths": paths,
    }


def main():
    request = json.load(sys.stdin)
    kind, target = request["kind"], request["target"]
    if kind == "python-distribution":
        result = distribution_metadata(target, request.get("metadata_paths"))
    elif kind == "configuration":
        result = {"present": bool(os.environ.get(target))}
    elif kind == "command":
        result = {"executable": shutil.which(target, path=os.environ.get("PATH", ""))}
    else:
        raise ValueError("Unsupported worker operation")
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
