# Plugin tooling tests

Tests are grouped by behavior, not by review round:

- `authoring/`: manifests, resource selection, host exports, generator and CI setup.
- `evaluation/`: agent sessions, deterministic observations, G-Eval, judge controls
  and private-reference matching/leak handling.
- `readiness/`: inventory/dependency resolution, isolated runtime probes, native
  MCP sessions/launch parity, and dataset identity/provenance.
- `support/`: shared plugin builders and isolated-runtime fixtures. The root
  conftest registers these fixtures once.

The host and released-core conformance entry points live in `../checks/` rather
than among the pytest cases. Repository workflow contracts remain in the root
`tests/test_plugin_tooling_workflows.py` suite because they require `.github/`.

Build the root distribution and install its wheel with `[plugin-dev,eval]` in a
clean environment. Run the suite outside the checkout to exercise installed code:

```bash
cd /tmp
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
DEEPEVAL_TELEMETRY_OPT_OUT=YES DEEPEVAL_DISABLE_DOTENV=1 \
/path/to/venv/bin/python -I -m pytest \
  /path/to/harness/packages/plugin-tools/tests \
  --confcutdir=/path/to/harness/packages/plugin-tools/tests -q --tb=short
```

Append `authoring`, `evaluation` or `readiness` to the test path for a focused run.
Use `--collect-only -q` to inspect collection. LLM/host session tests use local
fixtures; they do not spend model credits. Native host loader checks are separate
and require the pinned host CLIs. The released-core gate uses its own pinned
0.4.2 wheel/environment; it must not inherit the checkout's package metadata.
