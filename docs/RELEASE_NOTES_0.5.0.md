# OpenStudio SI 0.5.0

OpenStudio SI 0.5.0 separates core orchestration from specialized HVAC capabilities
and introduces tools for laboratories to develop, package, evaluate and diagnose
their own plugins for Claude Code and Codex.

## Core and HVAC Modeler

- OpenStudio SI core owns provider routing, workflow state, sessions, artifacts,
  simulation and results. The separately installable `openstudio-si-hvac` plugin
  owns the reviewed HVAC modeling workflows.
- HVAC Modeler 0.2.0 supports core `>=0.5.0 <0.6.0`. Its capabilities include VAV
  reheat, prototype CAV, packaged VAV, plant creation, topology inspection and
  air-side/plant equipment editing.
- Restored the `prototype_pvav_v1` profile and its native design-day sizing
  regression coverage from [PR #54](https://github.com/pnnl/openstudio-si-harness/pull/54).
  The profile uses gas central heat, approved two-speed DX cooling and electric
  terminal reheat. The original implementation was cherry-picked from `46e76f4`.
- Plugin descriptions, dependency checks, shared SDK consistency checks and
  versioned content fingerprints improve routing, diagnostics and release updates.

## Plugin development tools

Install the optional developer tools from the existing PyPI distribution:

```bash
python -m pip install "openstudio-si[plugin-dev]==0.5.0"
```

- `openstudio-si plugin init` generates a contributor project with a manifest,
  starter skill, optional MCP server, evaluation cases, development instructions
  and CI configuration stubs. Skills and MCP servers can coexist in one plugin.
- Draft manifests describe plugin identity, ownership, maintainers, dependencies
  and capabilities; skill/server counts are discovered from the contents.
- Validation and export commands produce independent Claude Code and Codex
  packages with content hashes, portable resource handling and credential-file
  exclusions.
- Project and native-host checks, pinned reusable workflows, and reproducible
  release candidates support contributor releases. Read-only build jobs are
  separated from upload-only jobs. Contributor plugin archives are GitHub release
  assets; they do not require a lab plugin package on PyPI.

## Local agent evaluation

```bash
python -m pip install "openstudio-si[eval]==0.5.0"
```

- Added developer-run DeepEval G-Eval with configurable criteria/evaluation steps
  and multiple judge suppliers, independently configured from the agent model.
- Captures host traces, model/artifact observations, execution limits and advisory
  scores, with support for repeat runs and rejudging.
- Adds environment scoping, private-reference protections and judge-injection
  controls. LLM judgments remain advisory and are not required in release CI.

## Plugin readiness

- `openstudio-si plugin doctor` reports host-scoped plugin inventories, descriptions,
  dependencies and capability prerequisites.
- Explicit runtime profiles enable bounded package, command, configuration and
  temporary MCP initialization/tool-inventory checks. Inventory remains passive;
  a temporary connection does not establish persistent host connectivity.
- Added a draft dataset identity contract and MCP-linked metadata bindings that
  reuse server arguments, environment mappings and executable overrides. Doctor
  can distinguish missing/unverified caches from caches whose recorded build
  source differs from the installed dataset.

## Upgrade and compatibility

- Install or enable HVAC Modeler alongside core, then reload the host. Updating
  core alone does not install its sibling plugin.
- Core 0.5.x exports for Claude Code forward the former core HVAC skill names. In
  Codex, use the corresponding skills supplied by HVAC Modeler.
- MCP interface contract **6.0**, OpenStudio SDK **3.11.0**, harness API **0.1.0**
  and draft plugin schemas **0.1** retain their independent versions.

## Maintenance and validation

- Organized tooling tests by authoring, evaluation and readiness, with shared
  fixtures and separate host/baseline conformance scripts.
- Removed retired review artifacts and consolidated implementation documentation.
- The corrected 0.5.0 candidate passed 293 fast release checks, 93 affected
  installed-wheel authoring/export/CI checks, paired marketplace export,
  wheel/sdist contents checks, Twine validation and both lockfile checks.
- Before the release-version correction, the complete reorganized tooling suite
  passed 391 checks against both the installed wheel and extracted source archive.
  Full slow/native and live-host CI gates remain separate from these local checks.

## Continuing work

The central importer for automatic third-party marketplace PR submission is not
included yet; contributor workflows currently stage release candidates. Independent
BESD plugin extraction, host enablement/legacy integration and real pilot
evaluations remain follow-up work. The BESD metadata/provenance provider changes
live in a separate repository and require their own release; publishing this
harness package does not publish that provider or a standards-data plugin.
