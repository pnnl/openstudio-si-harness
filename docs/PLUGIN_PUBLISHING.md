# Publishing independent OpenStudio SI plugins

The first pipeline uses **central polling of registered public GitHub releases**.
A laboratory publishes only to its own GitHub repository. The central marketplace
verifies the release and opens a plugin-scoped PR; a maintainer reviews/merges it.
After successful main-branch validation, immutable host dependency tags are created.
No lab marketplace write token, separate lab PyPI package, LLM key or judge score
is required. GitHub polling is scheduled every 15 minutes, subject to Actions
scheduling; maintainers can also dispatch a single-plugin check manually.

```text
Lab repository: source release -> read-only conformance -> candidate asset upload
Marketplace: registry -> public release poll -> source/archive/host/graph checks
            -> isolated write job -> scoped PR -> review/merge -> validated tags
```

## Deploy the central pipeline

These are deployment steps, not actions the local implementation performs.
OpenStudio SI 0.5.0 is already published; the new importer commands need the next
package release, prepared as **0.5.1**. Do not attempt to replace 0.5.0 on PyPI.

1. Review/merge the harness implementation and publish its 0.5.1 release through
   the existing PyPI workflow. Verify fresh installation loads `plugin
   marketplace-poll`, `validate-marketplace` and `sync-core-marketplace`.
2. In `pnnl/openstudio-si-plugins`, set repository variable
   `PLUGIN_TOOLING_VERSION=0.5.1` after that release is available. The workflow
   installs that exact published version and checks command support before use.
3. Configure **central-only** secret `PLUGINS_PR_TOKEN` using a narrowly scoped
   automation credential with contents and pull-request write access to this
   marketplace repository. A fine-grained PAT is sufficient for this first version.
   Do not distribute it to laboratories or put it in the registry. Use a credential
   whose PRs trigger normal pull-request CI; the default GITHUB_TOKEN suppresses
   follow-on workflow events. Credential rotation remains an operator task.
4. Review and merge the marketplace importer, validator/tag workflows and trusted
   stdlib publication helpers onto `main`. Enable Actions/schedules if disabled.
   Require the `Validate marketplace` check through the repository's branch
   protection/ruleset before allowing release PR merges.
5. Enroll one real source repository as below. Manually dispatch **Import registered
   plugin releases** after its candidate assets exist. Inspect the verification
   summary and resulting PR before merging the demonstration release.
6. Confirm **Validate marketplace** succeeds on the merge commit and **Tag validated
   plugin releases** creates `<plugin-id>--v<version>` at that exact commit. Install
   the plugin in both hosts and verify an update with a second plugin release.

This task prepares the workflows but does not set secrets/variables, merge code,
publish PyPI packages, create remote PRs or enable schedules. A live end-to-end
release is only complete after those deployment and installation checks.

## Register a source repository

Add an entry to `.openstudio-si/registry.json` in a reviewed marketplace PR:

```json
{
  "schema_version": "0.1",
  "plugins": {
    "osm-model-summary": {
      "repository": "YOUR-LAB/osm-model-summary",
      "owner": "my-lab",
      "plugin_path": "plugin",
      "enabled": true
    }
  }
}
```

Replace the repository/owner with real values; no demonstration repository is
pre-enrolled. The `owner` must match `manifest.owner.id`. A maintainer verifies the
organization's authority to use that ID/source before enrollment. Repository
identity is derived from GitHub at release time, not a required local manifest
field. IDs `openstudio-si` and `openstudio-si-hvac` remain reserved for the existing
exporters. Registry paths are relative plugin subdirectories; root-level `.` is
not supported by this initial importer.

`enabled: false` pauses future polling without uninstalling an already published
plugin. This also prevents a rejected release from being repeatedly proposed.
Changing ownership/source requires review; existing release receipts must remain
consistent with the approved mapping. Withdrawal/removal is a separate explicit
catalog change, not an automatic deletion of a lab's package.

### Unblock an incompatible core update

Pausing polling does **not** waive dependencies for a plugin still in either host
catalog. If a lab plugin requires core below 0.6, a core 0.6 export must stop.

1. Prefer a new lab release that has been tested against the new core and declares
   the correct range. Review the lab update and core update together when needed;
   full-catalog validation must pass for the proposed combined state.
2. If that update is unavailable, submit a reviewed withdrawal PR: set the registry
   entry's `enabled` to false; remove that ID's entries from both host catalogs;
   remove `<id>/`, `plugins/<id>/` and its current
   `.openstudio-si/releases/<id>.json` receipt. Review any dependent plugins too:
   update or withdraw them until the remaining graph validates. Git retains the
   withdrawn receipt/history; existing immutable release tags stay untouched.
3. Run `plugin validate-marketplace . --native-hosts`, merge the withdrawal, and
   rerun core publishing against the new base. Withdrawal stops future catalog
   discovery; it does not uninstall users' cached copies. Communicate the required
   core/plugin upgrade or removal to affected users.

Do not widen a range without testing or weaken a genuinely required dependency
to bypass the gate. Optional dependencies are appropriate only when the plugin
can actually operate without them.

## Laboratory workflow

1. Generate the project or adopt `plugin/` into an existing repository. The OSM
   walkthrough is in [PLUGIN_OSM_SUMMARY_TUTORIAL.md](PLUGIN_OSM_SUMMARY_TUTORIAL.md).
2. Supply reviewed skills, declared prerequisites and evaluation cases. Run local
   deterministic checks and agent/judge evaluations as appropriate. Scores remain
   advisory; CI does not run an LLM or simulation merely to publish a plugin.
3. Configure the generated thin workflows using an immutable reviewed harness SHA
   and an exact published tooling version. Existing 0.5.0 candidate records are
   accepted when their exporter format/content matches the current source contract.
4. Run `plugin check-project . --require-tracked` **locally before tagging**.
   Git cannot recover ignored files that never reached a fresh CI checkout.
5. Publish source GitHub release `v<manifest-version>`. The existing reusable
   conformance/upload jobs attach `release.json` and both host ZIP archives.
   Do not change the tag/commit, archive contents or release record for an already
   approved plugin version. A content change needs a new plugin version.
6. Central polling discovers the registered release and opens the release PR.
   Review its source SHA, ownership, counts, dependency results and native host
   checks. The lab does not authenticate a cross-repository dispatch.

The contributor workflow still works without registry enrollment: it stages
candidate assets, but an unregistered repository cannot obtain a central PR.
Initially only stable public releases are polled; private sources, pre-release
channels and release records containing evaluation attachments need a later
explicit contract. Authors may attach advisory reports separately for human review.

## What the importer verifies

- The manifest ID/owner/repository match the enrollment record; both host archives
  are present and the published tag resolves to the recorded immutable source SHA.
- Archive/descriptor/file hashes match, extraction stays under the owned plugin
  root, and links/special files, duplicate paths and oversized payloads are rejected.
- The plugin subtree fetched from that exact GitHub source commit re-exports to
  the same packages. Uploaded hashes alone are not trusted source provenance.
- Both native host readers accept the candidate. Their HOME and environment are
  isolated and GitHub/model credentials are not forwarded. MCP servers and agent
  sessions are not started for these checks.
- The proposed full catalog has matching host identities/versions, declared
  cross-plugin MCP providers, satisfiable required plugin ranges and no required
  dependency cycles. Optional dependency gaps are reported without blocking
  independent capabilities. Legacy core/HVAC versions participate in this static
  graph; this is not the generic doctor's runtime-readiness attestation.

Limits in this first version: each host ZIP is at most 32 MiB compressed and
128 MiB expanded, with at most 10,000 entries. The public source tarball is at most
64 MiB compressed; only the registered plugin subtree is extracted, bounded to
128 MiB/10,000 files. Credential filenames and ignored development paths are not
extracted. Model/dataset authors must still review what they intentionally ship.

## PR scope, concurrent releases and immutability

Each candidate proposes only its Claude package, Codex package, the corresponding
entries in both catalogs and `.openstudio-si/releases/<id>.json`. The writer
rechecks file hashes and registration, refuses unrelated catalog edits, and restores
verified executable flags lost in GitHub's artifact transport. It performs no
dependency installation and never imports or runs contributor code.

Branches use `automated/plugin/<id>-v<version>`. Repeated identical source releases
are idempotent. The same version with a different receipt is rejected. Existing
release branches are refreshed with an explicit force-with-lease only after their
receipt matches; different plugins never share an automation branch.

Polling runs are serialized. The writer checks that marketplace `main` still
matches the verified base. If it advanced, the run fails safely and the next poll
rebuilds proposals against the new catalog. Independent PRs may still need that
refresh after another PR merges. GitHub's merge checks must validate the actual
merged graph; automation does not bypass review or merge PRs automatically.

Core/HVAC publishing now calls `plugin sync-core-marketplace`, replacing only
reserved packages and their entries. It preserves registered lab packages,
registry/receipts and other catalog entries, and rejects a core update that would
break an existing required dependency. It no longer mirrors/deletes the entire
`plugins/`, `.agents` or `.claude-plugin` trees.

The marketplace's root `README.md` and `INSTALL.*.md` are maintainer-owned landing
pages and survive core sync unchanged. Refreshed generated core instructions live
under `docs/core/README.md` and `docs/core/INSTALL.*.md`; root files are seeded only
when absent. During deployment, review the root pages once and link to these core
instructions plus the host catalogs for current lab listings. `.generated.json`
continues to describe the core export and is refreshed by core publishing.

After successful validation of a main-branch push, tags are created as
`<plugin-id>--v<version>` at the validated SHA. Existing tags are retained only if
both host package trees match; conflicting content requires a version bump.
Tags are never force-updated. These tags are dependency resolution refs, not a
second PyPI release or an unreviewed source installation.

## Diagnose a run

- `no_stable_release`: no public stable source release was found.
- `awaiting_assets`: source CI has not uploaded all candidate assets; retry later.
- `disabled`: enrollment is paused.
- `unchanged`: the approved receipt is identical; no new PR is needed.
- `error`: inspect the structured failure stage, source record/tag, registry and
  conformance results. One failed source does not suppress healthy proposals.
- Writer base/version mismatch: rerun polling; never override immutability checks.

Error `reason` values are fixed codes, never raw response bodies or host stderr:

| Code | Check |
| --- | --- |
| `metadata_failed`, `invalid_metadata` | Retry transport failures; verify GitHub/proxy JSON is an object with a valid tag/assets. |
| `download_failed`, `download_limit` | Retry truncated transfers or reduce oversized assets. |
| `invalid_record`, `tag_commit` | Check release.json format, source SHA and matching immutable tag. |
| `prerelease_unsupported` | Publish a stable SemVer version; unticking GitHub's prerelease flag does not make an `-rc` version stable. |
| `archive_hash`, `descriptor_hash` | Uploaded bytes differ from the recorded digest; rebuild a new release. |
| `owner` | Match the package identity/owner to the reviewed registry entry. |
| `source_mismatch`, `source_verification_failed` | Compare the exact GitHub source archive with the exported working tree. |
| `immutable_version` | Changed approved content requires a new version. |
| `native_hosts_failed`, `catalog_validation_failed` | Reproduce host validation or inspect the complete dependency graph. |

GitHub's commit archive can differ from the working folder: `.gitattributes`
`export-ignore` omits files, `export-subst` rewrites marked contents, and Git LFS
files may be pointer text instead of the materialized bytes used locally. These
can produce `source_mismatch` or `source_verification_failed`. Inspect the archive
at the recorded SHA. Keep required plugin files as ordinary Git content without
archive transformations for this initial importer; release attachments alone
cannot substitute for reproducible source. Publish a new version after correcting
the source/export, rather than changing an approved tag.

Useful local commands (read-only except output staging):

```bash
openstudio-si plugin validate-marketplace /path/to/marketplace
openstudio-si plugin marketplace-poll --marketplace /path/to/marketplace \
  --base-sha <marketplace-commit-SHA> --plugin-id osm-model-summary \
  --output /tmp/osm-summary-proposal --native-hosts
python -I /path/to/marketplace/.github/scripts/publish_candidates.py \
  --marketplace /path/to/marketplace --candidates /tmp/osm-summary-proposal \
  --repository pnnl/openstudio-si-plugins --dry-run
```

Output directories must be new. Native commands need the centrally supported host
CLIs. Local dry runs do not push branches, open PRs or create tags. Keep actual
credentials out of command arguments and committed configuration files.
