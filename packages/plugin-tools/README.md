# OpenStudio SI plugin tools

Developer tools ship inside the existing `openstudio-si` wheel. Install the
optional extra to add their Python dependencies; the OpenStudio SDK and base
runtime dependencies are included. The SDK is installed with `openstudio-si[plugin-dev]`, but generation and
packaging do not import it or require a native OpenStudio CLI. Budget roughly
370 MB for the environment; size varies with platform and dependencies. The
pinned OpenStudio 3.11.0 package currently has no Windows Python 3.14 wheel;
use a supported Python/platform combination (CI uses Linux/Python 3.13).
No separate PyPI project or GitHub tooling
asset release is needed. This change is under review on `codex/plugin-tools-step-2`;
it must ship in a new `openstudio-si` release before labs can install it from PyPI.

```bash
python -m pip install "openstudio-si[plugin-dev]"
openstudio-si plugin init ./my-plugin --id my-plugin --owner my-lab --maintainer "Plugin Maintainer" --license MIT
openstudio-si plugin validate ./my-plugin/plugin
openstudio-si plugin export ./my-plugin/plugin --host all --output ./my-plugin/dist/plugin
openstudio-si plugin validate ./my-plugin/dist/plugin/codex/my-plugin
```

For an unpublished checkout, build from the repository root and install that wheel:

```bash
python -m build --outdir /tmp/si-dist
python -m pip install "/tmp/si-dist/openstudio_si-<version>-py3-none-any.whl[plugin-dev]"
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
exporters. Consolidating those exporters is outside the current plan. The
packaged template is the maintained example source. CI generates skill and MCP
projects from the installed wheel and creates provider/consumer test fixtures.
No separate handwritten example folder is maintained.

`plugin init` creates one plugin project with a starter skill. `--with-mcp`
optionally adds an example server alongside that skill; it does not classify the
plugin. Add or remove skills and MCP declarations as the plugin develops, and
update its dependencies/capabilities accordingly. The manifest has no plugin-type
field. Contents and counts are discovered from files. This flag scaffolds a new
project; it does not modify an existing project. The command supplies
individual ownership defaults and accepts `--name`, `--owner`, `--organization`,
`--maintainer`, `--contact`, `--description` and `--version`. It requires a valid
third-party `--id`, supports `--dry-run` without creating the destination or its
parents, and refuses existing files and symlinks. Generation validates in a
temporary directory and publishes into a new or empty target. It never creates
a Git repository or requests account access.

The project includes a manifest, routing skill, explicitly selected license,
AGENTS.md, CLAUDE.md, draft evaluation assets, manual CI-configuration stubs,
Python build configuration and template/tooling provenance. Shared check/host/
release logic lives in the wheel's CLI; no implementation scripts are copied
into each lab repository. An MCP starter also contains a dependency-free read-only example
server and console command. Every evaluation resource is exported with the
plugin. Initial cases demonstrate the layout and must be replaced with real
domain cases. Agent/LLM evaluation and the `[eval]` extra arrive in step C;
example run configuration is explicitly draft and not executable yet.

The Python package build reads the canonical version from the YAML manifest
using Hatch's regex version source. Generated versions must satisfy SemVer and
PEP 440. The two release archives and release.json contain source identity and
hashes; stale exports and mismatched release tags fail before writing release
artifacts. Archive timestamps are fixed for repeatable builds. The configured release
workflow stages these candidate artifacts on the developer's GitHub release.
Marketplace PR submission requires the importer in step E and is not wired yet.

Tool releases use the existing `openstudio-si` PyPI publishing workflow and
share its version. The internal Python import remains `openstudio_si_harness`;
there is no separate `openstudio-si-harness` distribution. Lab plugins still
publish through scoped PRs to `pnnl/openstudio-si-plugins`, never PyPI.

Initial CI files are manual-only configuration stubs. After publishing a release
containing these tools and reviewing the shared workflows, activate or upgrade:

```bash
openstudio-si plugin configure-ci ./my-plugin \
  --workflow-ref <full-40-character-PNNL-commit-SHA> \
  --tooling-version <published-openstudio-si-version>
```

The version defaults to the installed `openstudio-si` version. Releases older
than 0.4.3 are rejected; CI checks that all required commands actually load
before installing host CLIs. CI installs
`openstudio-si[plugin-dev]==<version>` directly from PyPI with binary wheels in
a clean environment. Labs do not download a separate tool asset or supply a
custom lock. The tool version is pinned; transitive dependencies resolve within
its declared bounds for the platform/Python. This is not a hash-locked install.
The reusable workflow currently runs on Ubuntu 24.04/Python 3.13.

configure-ci records the exact stable release and workflow commit in
`.openstudio-si/ci.json`. It writes thin shared-workflow calls and refuses to
overwrite author-edited managed workflows. Its --dry-run option previews changes.
Use a new workflow ref/version to upgrade without regenerating the project.
There are no copied client implementations or imports of tooling internals.

The build workflow has contents: read, disables persisted checkout credentials,
and installs npm CLIs with --ignore-scripts. plugin prepare-hosts selects the
packaged native Claude binary without executing its lifecycle installer. The
separate upload-only job has contents: write but performs no checkout, dependency
installation or contributor-code execution. It stages candidate assets on the
lab's GitHub release for the intended pnnl/openstudio-si-plugins importer. Candidate
records explicitly state importer-pending; archive upload is not marketplace
publication. Central scoped PR submission remains step E. The existing full-tree
core publisher cannot be reused for independent lab updates.

Shared commands are plugin check-project PROJECT [--require-tracked],
plugin check-hosts EXPORT_REPORT and plugin package-release PROJECT --report ...
--tag ... --repository ... --source-sha .... No LLM calls or model credentials
are used. Run check-project --require-tracked locally before tagging: it detects
present-but-untracked/ignored export inputs. In a fresh CI checkout, absent ignored
files cannot be detected, so the same flag is only a consistency check. Generated
host metadata comes from those checked source inputs.

plugin init requires --license (MIT, BSD-2-Clause or BSD-3-Clause). There is no
implicitly approved license. --copyright-holder overrides the organization or
maintainer fallback, and the selected holder is written into both license files.
Contributors must confirm licensing with their own legal/IP office. Evaluation
case schema and run config remain draft 0.1 until the runner milestone. Existing
empty destinations keep their directory inode; population uses exclusive creates
rather than replacing a user's current working directory.

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
python -I /path/to/harness/packages/plugin-tools/tests/validate_hosts.py
```

The second command generates the starter with and without the MCP example, checks their exported host
packages and their own host-check scripts, and checks provider/consumer fixtures. It requires Claude Code, Codex and npm's semver package. CI
pins these tools, validates Claude exports, and uses Codex's app-server
plugin/read loader to check skill and MCP discovery without an LLM session.
The evaluation runner, LLM judge adapters, generic doctor and marketplace
submission remain later steps.


The contributor supplies the plugin's own release version and dependency ranges.
The generator supplies schema/API defaults. `harness_api` refers to the
plugin authoring/packaging contract, independently of the tooling release or the
core plugin release. Declare a core plugin version dependency only when using
core capabilities. Probe names are draft declarations for the later doctor;
source validation alone does not establish runtime readiness.

Evaluation is a developer-run workflow. Generated CI/release workflows will run
deterministic conformance and native host checks without agent or judge model
calls, model credentials, or judge-score gates. Evaluation reports are optional
release attachments and scores remain advisory during calibration.


The released-core doctor gate pins the published 0.4.2 wheel URL and SHA-256
inside `.github/workflows/plugin-tools.yml`. It creates a separate venv with
only that root wheel and its runtime dependencies, including its own package
metadata. Branch tools run only in the controller. The -I probe resolves both
module paths and version metadata inside the baseline venv; no sys.path checkout
injection is used. It compares default/matching-plugin doctor observations before
and after adding third-party cache entries. Release 0.4.2 predates the HVAC split.

```bash
python -I packages/plugin-tools/tests/check_released_core_doctor.py \
  --core-wheel /path/to/openstudio_si-0.4.2-py3-none-any.whl \
  --sha256 c81b9bae11269c4c09f4bb4c9b538934888e234abf5b5ac7d3b41d02d3c4caa0 \
  --report /tmp/released-core-doctor.json
```

The controller needs installed developer tools; the runner creates and installs
the isolated baseline environment itself (requires package-index access).
An export containing host metadata must retain its
export descriptor; deleting that descriptor does not turn it into a source project.


Repository-owned workflow contracts are tested by tests/test_plugin_tooling_workflows.py
in the harness checkout. Tool tests no longer read .github files.
CI also extracts the root sdist outside the checkout and runs its tool suite
against the installed wheel, so source-archive usability is verified explicitly.

The missing-extra hint handles absent jsonschema/packaging imports; it is not an
extra-installation flag. MCP already installs jsonschema transitively, so it
usually detects missing packaging. Generation/export do not import the SDK.
