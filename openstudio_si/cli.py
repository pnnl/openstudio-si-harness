"""Dispatch plugin development without starting the modeling runtime."""

import sys


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "plugin":
        try:
            from openstudio_si_harness.cli import main as plugin_main
        except ModuleNotFoundError as exc:
            if exc.name not in {"jsonschema", "packaging"}:
                raise
            print(
                'Install plugin tools with: pip install "openstudio-si[plugin-dev]"',
                file=sys.stderr,
            )
            return 1
        return plugin_main(arguments)
    from cli import main as runtime_main

    return runtime_main(arguments)
