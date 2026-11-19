# OpenStudio SI plugin tools

This lightweight distribution validates plugin source projects and exports host
packages without installing the OpenStudio modeling runtime. Step 1 is under
review on `codex/plugin-tools-step-1`.

Build and install the wheel in a separate environment.

```bash
uv build --project packages/plugin-tools
python -m pip install packages/plugin-tools/dist/*.whl
openstudio-si-harness plugin validate ./plugin
openstudio-si-harness plugin export ./plugin --host all --output ./dist/plugin
openstudio-si-harness plugin validate ./dist/plugin/codex/my-plugin
```

Author identity and dependencies in `openstudio-si-plugin.yaml` using the
bundled `schemas/plugin.schema.json`. The manifest is draft schema `0.1`;
`core-interface` is reserved for a future defined readiness contract and is not
accepted. Skills live in `skills/<name>/SKILL.md`.
Counts are discovered. Repository metadata is optional. `HARNESS_API_VERSION`
identifies the supported manifest/API contract independently of tool releases.
Python distribution dependencies use PEP 440. Other ranges use full SemVer
comparators separated by spaces, such as `>=0.4.3 <0.5.0`. Supported operators
are `>=`, `<=`, `>`, `<`, `=` and bare exact versions. Commas, `==`, `!=`,
caret and wildcard forms are rejected. npm prerelease exclusion applies.
Version parsing uses `semantic-version>=2.10,<3`. Public comparator matching
implements npm precedence and prerelease admission explicitly, without the
library's npm-range matcher. The host check compares a generated 110-case matrix
with npm's parser. The legacy core/HVAC range remains stable-release-only.

Generic exports use `openstudio-si-export.json`. This envelope records the
normalized manifest, host, tool release, API version, source MCP declarations,
and SHA-256 hashes of every other exported file. Validation accepts source
projects and exported packages. It verifies hashes and regenerated host metadata.
Hashes establish package consistency. Release provenance and signatures belong
to the marketplace publishing step.

The IDs `openstudio-si` and `openstudio-si-hvac` remain reserved for the existing
exporters. Consolidating those exporters is outside the current plan. One neutral source example
lives in `examples/minimal`. It contains a single skill and no dependencies or
MCP server. Dependency/provider scenarios are generated privately by the host
check under `tests/`. The template generator will eventually supply the example
project directly, so its output becomes the maintained source of truth.

Default resources include skills, scripts, references, assets, agents,
contracts, bin, sdk, hooks, commands, README, license, notices, settings,
AGENTS.md and CLAUDE.md. Evaluation cases are included at their declared path.
Declare additional resource files or directories explicitly.

```yaml
package:
  include: [data, custom-runtime]
```

Unknown top-level files and directories require an include declaration. Development
folders such as tests and .github are excluded unless explicitly included.
The source-root build, dist and venv folders are excluded; nested resource
folders with those names are preserved. .venv, git metadata, node_modules, Python
caches, .DS_Store, .env and .env.* files are excluded at every depth. Common
credential files (id_rsa and other SSH private-key names, *.pem, *.key, *.p12,
*.pfx, .npmrc, .netrc, .envrc, .secrets and credentials.json) are also excluded at every depth.
This filename policy does not replace an author's review of bundled contents. Symlinks
in selected files or their parents are rejected. Excluded directories are
never traversed. Executable permissions and relative resource paths survive
export. The export report lists the package locations.

An external MCP dependency names its supplying plugin dependency by ID.
The later doctor integration will verify installation and readiness.

```yaml
dependencies:
  - id: core
    kind: plugin
    target: openstudio-si
    version_spec: ">=0.4.3 <0.5.0"
    required: true
    probe: metadata
  - id: core-mcp
    kind: mcp-server
    target: openstudio_si
    provider: core
    required: true
    probe: mcp-tools
```

Source `.mcp.json` supports stdio servers installed as executable commands.
Commands must be bare executable names. Arguments must be portable and must
not contain host-root substitutions, absolute paths or credential flags.
Environment values must be `${VARIABLE}` references. Literal values are
rejected so credentials cannot be copied through the environment declaration.
An author remains responsible for reviewing all bundled text and arguments.

```json
{"mcpServers":{"standards":{"command":"besd-mcp","args":[],"env":{"API_KEY":"${BESD_API_KEY}"}}}}
```

Both host packages receive a generated Unix `/bin/sh` launcher. It looks for
the executable on PATH, then in ~/.local/bin, ~/.cargo/bin, /opt/homebrew/bin
and /usr/local/bin. Set `OPENSTUDIO_SI_<SERVER>_COMMAND` in the desktop process
environment to an absolute executable path when using a custom installation.
The server name is uppercased and punctuation becomes underscores. Names that
collide after this mapping are rejected. Environment
references resolve inside the launcher without shell eval. Missing commands or
credentials fail with a setup message. MCP packages declare supported_platforms
as darwin/linux in the export descriptor; exporting or validating an MCP package
on another platform fails. Skill-only packages declare any. Native host loaders
may not enforce this OpenStudio SI descriptor, so unsupported hosts still need
the shared readiness check before use.
Windows launchers and bundled executable MCP servers need host bindings in a
later step. They are rejected as source declarations in this version.

Required plugin dependencies are also emitted in Claude metadata. Codex
receives the descriptor for the planned shared readiness integration.

Run implementation checks against an installed wheel, from outside the checkout.
No test modifies sys.path to import the source package.

```bash
python -I -m pytest /path/to/harness/packages/plugin-tools/tests --confcutdir=/path/to/harness/packages/plugin-tools/tests -q
python -I /path/to/harness/packages/plugin-tools/tests/validate_hosts.py /path/to/harness/packages/plugin-tools/examples/minimal
```

The second command checks the minimal example plus generated provider/consumer
fixtures. It requires Claude Code, Codex and npm's semver package. CI
pins these tools, validates Claude exports, and uses Codex's app-server
plugin/read loader to check skill and MCP discovery without an LLM session.
Generator, evaluation runner, LLM judge and marketplace submission are later steps.


The contributor supplies the plugin's own release version and dependency ranges.
The planned generator supplies schema/API defaults. `harness_api` refers to the
plugin authoring/packaging contract, independently of the tooling release or the
core plugin release. Declare a core plugin version dependency only when using
core capabilities. Probe names are draft declarations for the later doctor;
source validation alone does not establish runtime readiness.

Evaluation is a developer-run workflow. Generated CI/release workflows will run
deterministic conformance and native host checks without agent or judge model
calls, model credentials, or judge-score gates. Evaluation reports are optional
release attachments and scores remain advisory during calibration.


The released-core doctor CI gate requires repository variables
`OPENSTUDIO_SI_CORE_043_WHEEL_URL` and `OPENSTUDIO_SI_CORE_043_WHEEL_SHA256`.
They identify an immutable released core 0.4.3 wheel and its independently pinned
hash. The job installs that release, exports its core/HVAC packages, and compares
the released doctor's readiness/diagnostics before and after adding third-party
cache entries in isolated home directories. It covers core-only, cached HVAC and
explicit HVAC selection, and uploads a report. Missing baseline configuration
fails the gate. The current checkout is never substituted for the release.

```bash
python -I packages/plugin-tools/tests/check_released_core_doctor.py \
  --core-wheel /path/to/openstudio_si-0.4.3-py3-none-any.whl \
  --sha256 <published-wheel-sha256> \
  --example packages/plugin-tools/examples/minimal \
  --report /tmp/released-core-doctor.json
```

Run this in an environment containing the baseline wheel's runtime dependencies
and the new plugin tooling. An export containing host metadata must retain its
export descriptor; deleting that descriptor does not turn it into a source project.
