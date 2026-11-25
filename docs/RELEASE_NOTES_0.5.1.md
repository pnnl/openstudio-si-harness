# OpenStudio SI 0.5.1

Release candidate; publication and marketplace deployment are pending.

- Add a developer walkthrough for a one-skill OSM inventory plugin: read-only SDK
  inspection, fixture, manifest, evaluation case and release instructions.
- Add central polling of registered public GitHub releases, source/archive and
  ownership verification, scoped marketplace proposals and complete host-specific
  dependency validation. Existing 0.5.0 candidate packages remain supported.
- Preserve third-party packages and maintainer-owned root documentation during
  core/HVAC marketplace updates; refresh generated core guides under docs/core.
- Isolate flaky source failures, report fixed diagnostics and reject SemVer
  prereleases even when GitHub marks them stable.
- Add marketplace workflows for isolated PR publication, native Claude/Codex
  validation and immutable per-plugin tags after reviewed merge and validation.
- Keep LLM evaluations local/advisory. Laboratories need no marketplace write token
  and publish their plugin assets to their own GitHub releases, not PyPI.

Deployment requires the published 0.5.1 tools, central configuration and real plugin
enrollment. See [publishing instructions](PLUGIN_PUBLISHING.md). Core MCP contract
6.0, harness API 0.1.0, draft schema 0.1, SDK 3.11.0 and HVAC 0.2.0 are unchanged.
