"""Dispatch plugin development without starting the modeling runtime."""

import sys


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"plugin", "eval"}:
        try:
            if arguments[0] == "eval":
                from openstudio_si_harness.evaluation.cli import main as eval_main

                return eval_main(arguments[1:])
            from openstudio_si_harness.cli import main as plugin_main
        except ModuleNotFoundError as exc:
            if exc.name not in {"jsonschema", "packaging"}:
                raise
            print(
                'Install developer tools with: pip install "openstudio-si['
                + ("eval" if arguments[0] == "eval" else "plugin-dev")
                + ']"',
                file=sys.stderr,
            )
            return 1
        return plugin_main(arguments)
    from cli import main as runtime_main

    return runtime_main(arguments)
