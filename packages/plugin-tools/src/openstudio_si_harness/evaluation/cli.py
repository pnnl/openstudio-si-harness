"""Developer-run evaluation commands; deterministic CI never invokes models."""

import argparse
import json
import sys
from pathlib import Path

import yaml

from .contracts import check_endpoint, validate
from .runner import rejudge, report, run


def main(argv=None):
    parser = argparse.ArgumentParser(prog="openstudio-si eval")
    commands = parser.add_subparsers(dest="action", required=True)
    execute = commands.add_parser(
        "run", help="Run local agent cases and optional advisory judgments."
    )
    execute.add_argument("--plugin", required=True, type=Path)
    execute.add_argument("--cases", required=True, type=Path)
    execute.add_argument("--config", required=True, type=Path)
    execute.add_argument("--output", required=True, type=Path)
    judge = commands.add_parser(
        "judge", help="Append judgments to an already captured run."
    )
    judge.add_argument("--run", required=True, type=Path)
    judge.add_argument("--config", required=True, type=Path)
    view = commands.add_parser(
        "report", help="Regenerate reports from captured evidence and judgment history."
    )
    view.add_argument("--run", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "run":
            result = run(args.plugin, args.cases, args.config, args.output)
        elif args.action == "judge":
            value = yaml.safe_load(args.config.read_text())
            settings = value.get("judge", value)
            validate("evaluation-judge-config", settings)
            check_endpoint(settings)
            result = rejudge(args.run.resolve(), settings)
        else:
            result = report(args.run.resolve())
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return (
        2
        if result["status"] == "incomplete"
        else (1 if result["status"] == "failed" else 0)
    )
