"""Command-line interface for plugin development."""

import argparse
import json
from pathlib import Path
import sys

import yaml

from .export import HOSTS, DESCRIPTOR, export_plugin, validate_export
from .manifest import load_plugin


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="openstudio-si-harness")
    commands = parser.add_subparsers(dest="command", required=True)
    plugin = commands.add_parser("plugin").add_subparsers(dest="action", required=True)
    validate = plugin.add_parser(
        "validate", help="Validate identity, dependencies, and content."
    )
    validate.add_argument("source", type=Path)
    export = plugin.add_parser("export", help="Export independent host packages.")
    export.add_argument("source", type=Path)
    export.add_argument("--host", choices=(*HOSTS, "all"), default="all")
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "validate":
            result = (
                validate_export(args.source)
                if (args.source / DESCRIPTOR).exists()
                else load_plugin(args.source)
            )
            result = {
                "id": result["manifest"]["id"],
                "version": result["manifest"]["version"],
                "inventory": result["inventory"],
            }
        else:
            hosts = HOSTS if args.host == "all" else (args.host,)
            result = export_plugin(args.source, args.output, hosts)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
