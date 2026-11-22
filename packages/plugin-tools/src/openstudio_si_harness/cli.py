"""Command-line interface for plugin development."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

from . import __version__
from .ci import configure_ci
from .export import DESCRIPTOR, HOSTS, export_plugin, validate_export
from .generator import LICENSES, init_project
from .manifest import load_plugin
from .project_tools import check_hosts, check_project, package_release, prepare_hosts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="openstudio-si")
    commands = parser.add_subparsers(dest="command", required=True)
    plugin = commands.add_parser("plugin").add_subparsers(dest="action", required=True)
    validate = plugin.add_parser(
        "validate", help="Validate identity, dependencies, and content."
    )
    validate.add_argument("source", type=Path)
    doctor = plugin.add_parser(
        "doctor", help="Inspect plugin metadata readiness without starting services."
    )
    doctor.add_argument("--plugin-dir", type=Path, action="append", default=[])
    doctor.add_argument("--plugin-id")
    doctor.add_argument(
        "--host",
        choices=HOSTS,
        help="Select a host and bind explicitly supplied source plugins to it.",
    )
    doctor.add_argument("--capability")
    doctor.add_argument(
        "--runtime-profile",
        type=Path,
        help="Host-bound local runtime profile; no probes run without --check-runtime.",
    )
    doctor.add_argument(
        "--check-runtime",
        action="store_true",
        help="Run bounded probes in explicitly bound runtimes.",
    )
    export = plugin.add_parser("export", help="Export independent host packages.")
    export.add_argument("source", type=Path)
    export.add_argument("--host", choices=(*HOSTS, "all"), default="all")
    export.add_argument("--output", type=Path, required=True)
    initialize = plugin.add_parser(
        "init", help="Generate a local plugin development project."
    )
    initialize.add_argument("destination", type=Path)
    initialize.add_argument("--id", dest="identity", required=True)
    initialize.add_argument(
        "--license", dest="license_identifier", choices=LICENSES, required=True
    )
    initialize.add_argument("--copyright-holder")
    initialize.add_argument("--name")
    initialize.add_argument("--owner", default="individual")
    initialize.add_argument("--organization")
    initialize.add_argument("--maintainer", default="Plugin maintainer")
    initialize.add_argument("--contact")
    initialize.add_argument("--description")
    initialize.add_argument(
        "--with-mcp",
        action="store_true",
        help="Add an example MCP server alongside the starter skill.",
    )
    initialize.add_argument("--version", default="0.1.0")
    initialize.add_argument("--dry-run", action="store_true")
    check = plugin.add_parser(
        "check-project", help="Validate project cases and optionally git tracking."
    )
    check.add_argument("project", type=Path)
    check.add_argument(
        "--require-tracked",
        action="store_true",
        help="Run locally before tagging to find present untracked inputs; CI cannot recover files omitted from checkout.",
    )
    hosts = plugin.add_parser(
        "check-hosts", help="Validate exported packages with native hosts."
    )
    hosts.add_argument("report", type=Path)
    release = plugin.add_parser(
        "package-release", help="Build a marketplace candidate with provenance."
    )
    release.add_argument("project", type=Path)
    release.add_argument("--report", type=Path, required=True)
    release.add_argument("--tag", required=True)
    release.add_argument("--repository", required=True)
    release.add_argument("--source-sha", required=True)
    configure = plugin.add_parser(
        "configure-ci", help="Activate or upgrade pinned reusable workflows."
    )
    configure.add_argument("project", type=Path)
    configure.add_argument("--workflow-ref", required=True)
    configure.add_argument(
        "--tooling-version",
        default=__version__,
        help="Exact published openstudio-si version (default: installed version).",
    )
    configure.add_argument("--dry-run", action="store_true")
    prepare = plugin.add_parser(
        "prepare-hosts", help="Prepare native CLIs without npm lifecycle scripts."
    )
    prepare.add_argument("prefix", type=Path)
    prepare.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "doctor":
            from .readiness import doctor as diagnose

            result = diagnose(
                args.plugin_dir,
                plugin_id=args.plugin_id,
                capability=args.capability,
                host=args.host,
                runtime_profile=args.runtime_profile,
                check_runtime=args.check_runtime,
            )
        elif args.action == "init":
            result = init_project(
                args.destination,
                identity=args.identity,
                license_identifier=args.license_identifier,
                copyright_holder=args.copyright_holder,
                name=args.name,
                owner=args.owner,
                organization=args.organization,
                maintainer=args.maintainer,
                contact=args.contact,
                description=args.description,
                with_mcp=args.with_mcp,
                version=args.version,
                dry_run=args.dry_run,
            )
        elif args.action == "configure-ci":
            result = configure_ci(
                args.project,
                workflow_ref=args.workflow_ref,
                tooling_version=args.tooling_version,
                dry_run=args.dry_run,
            )
        elif args.action == "check-project":
            result = check_project(args.project, args.require_tracked)
        elif args.action == "prepare-hosts":
            result = prepare_hosts(args.prefix, args.output_dir)
        elif args.action == "check-hosts":
            result = check_hosts(args.report)
        elif args.action == "package-release":
            result = package_release(
                args.project,
                args.report,
                tag=args.tag,
                sha=args.source_sha,
                repository=args.repository,
            )
        elif args.action == "validate":
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
    except (OSError, ValueError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if args.action == "doctor":
        return 0 if result["status"] == "ready" else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
