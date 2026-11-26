# Build your first OpenStudio SI plugin: summarize an OSM

This walkthrough creates a new repository with **one skill and zero MCP servers**.
It reads an OSM and reports stories, spaces, thermal zones, air/plant loops and
zone HVAC counts. It never saves the model, sizes equipment, simulates or claims
code compliance. No core-plugin installation or weather file is required.

## 1. Bootstrap tools, then create the project environment

Run from the directory that will hold the new repository. First create a throwaway
bootstrap environment at `/tmp/osm-summary-tools`. After generating the project,
create its development environment at `osm-model-summary/.venv`:

```bash
python3.13 -m venv /tmp/osm-summary-tools
/tmp/osm-summary-tools/bin/python -m pip install "openstudio-si[plugin-dev]==0.5.0"
/tmp/osm-summary-tools/bin/openstudio-si plugin init ./osm-model-summary \
  --id osm-model-summary --name "OSM Model Summary" \
  --description "Read-only OSM inventory with evidence" \
  --owner my-lab --organization "My Laboratory" \
  --maintainer "Your Name" --license MIT
cd osm-model-summary
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "openstudio-si[plugin-dev]==0.5.0"
```

The named bootstrap environment keeps tooling out of the parent folder's `.venv`.
If the CLI is already available you can skip that bootstrap and invoke it directly.
The new project's environment remains inside its own repository. Select your
organization's approved license/holder.
The commands pin published 0.5.0 for authoring. After 0.5.1 is available on PyPI,
these installs can use `"openstudio-si[plugin-dev]>=0.5.1"`.
The base installation includes OpenStudio SDK 3.11.0; no OpenStudio CLI or MCP is
needed for this read-only plugin. Do not publish this demonstration as a second
`openstudio-si` PyPI package.

## 2. Replace the starter skill

Rename `plugin/skills/osm-model-summary` to `plugin/skills/summarize-osm`. Replace
its `SKILL.md` with:

```markdown
---
name: summarize-osm
description: Inspect a local OpenStudio OSM and report model inventory without editing or simulating it.
---

Use this skill when the user asks what an OSM contains or requests a model summary.

1. Identify the exact input .osm. If several files could be intended, ask which.
2. Use the Python environment selected for this plugin. Run the bundled
   `scripts/summarize_osm.py` with the model path as a quoted positional argument.
   Resolve the script relative to this SKILL.md; do not depend on a developer's
   checkout path or write a replacement script.
3. If the SDK is unavailable or loading fails, report the error and the required
   SDK/environment. Do not treat a missing dependency as proof the model is corrupt.
   Invalid inputs may produce native SDK diagnostics despite logger suppression;
   treat exit code 2 as failure and do not try to parse that output as summary JSON.
4. Read the JSON. Report the building name, object counts, spaces/zones and air/
   plant loop names. Explain warnings and retain m² units for floor areas.
5. State that inspection did not simulate, size, validate compliance or save the
   model. Do not infer absent HVAC solely from an empty air-loop list.

Do not edit or overwrite the input. Do not launch MCP servers or install packages
as part of the summary. Do not silently switch to another model or SDK version.
```

Create `plugin/skills/summarize-osm/scripts/summarize_osm.py` with this reviewed
operation. The script's JSON is evidence for the agent's answer:

```python
"""Summarize an OSM without editing, saving or simulating it."""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def summarize(path):
    import openstudio

    if openstudio.openStudioVersion() != "3.11.0":
        raise ValueError("This plugin requires OpenStudio SDK 3.11.0.")
    path = path.expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".osm":
        raise ValueError("Select an existing .osm file.")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    openstudio.Logger.instance().standardOutLogger().disable()
    translator = openstudio.osversion.VersionTranslator()
    loaded = translator.loadModel(openstudio.path(str(path)))
    if not loaded.is_initialized():
        raise ValueError("The selected SDK could not load this model.")
    model = loaded.get()
    spaces = sorted(model.getSpaces(), key=lambda item: item.nameString())
    zones = sorted(model.getThermalZones(), key=lambda item: item.nameString())
    air_loops = sorted(model.getAirLoopHVACs(), key=lambda item: item.nameString())
    unassigned = [space.nameString() for space in spaces if not space.thermalZone().is_initialized()]
    warnings = []
    if not spaces:
        warnings.append("The model contains no spaces.")
    if unassigned:
        warnings.append("Some spaces are not assigned to thermal zones.")
    if not air_loops:
        warnings.append("No air loops were found; zone HVAC equipment may still exist.")
    if hashlib.sha256(path.read_bytes()).hexdigest() != before:
        raise ValueError("The input changed during inspection; rerun on a stable file.")
    return {
        "format": "openstudio-si-osm-summary/0.1",
        "input_name": path.name,
        "input_sha256": before,
        "sdk_version": openstudio.openStudioVersion(),
        "building_name": model.getBuilding().nameString(),
        "counts": {
            "stories": len(model.getBuildingStorys()),
            "spaces": len(spaces),
            "thermal_zones": len(zones),
            "air_loops": len(air_loops),
            "plant_loops": len(model.getPlantLoops()),
            "zone_hvac_equipment": sum(len(zone.equipment()) for zone in zones),
        },
        "spaces": [
            {"name": space.nameString(), "floor_area_m2": space.floorArea(),
             "thermal_zone": space.thermalZone().get().nameString() if space.thermalZone().is_initialized() else None}
            for space in spaces
        ],
        "zones": [zone.nameString() for zone in zones],
        "air_loops": [
            {"name": loop.nameString(), "served_zones": sorted(zone.nameString() for zone in loop.thermalZones())}
            for loop in air_loops
        ],
        "plant_loops": sorted(loop.nameString() for loop in model.getPlantLoops()),
        "unassigned_spaces": unassigned,
        "warnings": warnings,
        "simulation_performed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    args = parser.parse_args()
    try:
        result = summarize(args.model)
    except ImportError:
        print("OpenStudio SDK unavailable: select the plugin's Python environment.", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

## 3. Declare identity, capabilities and prerequisites

Update both READMEs to describe the real summary capability and its Python
environment. Remove the starter's claims that it only explains a layout.
Keep the generated manifest's owner, license, version and evaluation path. Replace
its description, dependencies and capabilities with the following. Version 0.1.0
is your plugin release version, independent of tooling/core release versions.

```yaml
description: Read-only inventory of a local OpenStudio model.
dependencies:
  - id: openstudio-sdk
    kind: python-distribution
    target: openstudio
    version_spec: "==3.11.0"
    required: true
    probe: metadata
    install_hint: Select the Python environment containing OpenStudio SDK 3.11.0.
capabilities:
  - id: summarize-osm
    name: Summarize an OSM
    description: Report model object counts and HVAC loop inventory without editing.
    requires: [openstudio-sdk]
    effects: [read-data]
```

Do not add a core-plugin dependency: this capability is independent. Omit
`.mcp.json`. Remove any stale supplied `inventory` field after changing contents;
validation/export discover one skill and zero servers. Bundling a reviewed Python
script does not make this an MCP plugin.

## 4. Build a small redistributable fixture and verify directly

Run this from the repository root to create a fixture that contains one story,
two spaces, two thermal zones and no HVAC loops:

```python
from pathlib import Path
import openstudio

model = openstudio.model.Model()
model.getBuilding().setName("Summary Demo")
story = openstudio.model.BuildingStory(model)
story.setName("Story 1")
for number in (1, 2):
    space = openstudio.model.Space(model)
    space.setName(f"Space {number}")
    space.setBuildingStory(story)
    zone = openstudio.model.ThermalZone(model)
    zone.setName(f"Zone {number}")
    space.setThermalZone(zone)
path = Path("plugin/evals/fixtures/two-zone.osm")
path.parent.mkdir(parents=True, exist_ok=True)
model.save(openstudio.path(str(path)), True)
```

```bash
python plugin/skills/summarize-osm/scripts/summarize_osm.py \
  plugin/evals/fixtures/two-zone.osm
```

Verify counts against the known fixture, the input hash before/after, a model with
no spaces, and invalid/missing inputs. Empty geometry/zero floor area in this
fixture is intentional; these object counts are not proof of a simulation-ready
building. Never commit a customer's model or weather file as demonstration data.

## 5. Replace the demonstration evaluation cases

Remove the generated layout-demo case/reference and its separate demonstration
calibration case. Add cases for the two-zone
fixture, an empty model, unassigned spaces, an invalid input and a missing SDK.
For the first case, save `plugin/evals/cases/two-zone-summary.json`:

```json
{
  "schema_version": "0.1",
  "case_id": "two-zone-summary",
  "description": "Summarize the bundled inventory fixture without changing it.",
  "prompt": "Use summarize-osm to inspect the supplied OSM. Report counts and limitations without editing or simulating.",
  "inputs": ["evals/fixtures/two-zone.osm"],
  "required_skills": ["osm-model-summary:summarize-osm"],
  "expected": {
    "output": "One story, two spaces, two thermal zones, zero air loops and zero plant loops. No simulation or sizing was performed."
  },
  "judge": {
    "framework": "deepeval",
    "threshold": 0.8,
    "metrics": [{
      "id": "inventory-accuracy",
      "type": "geval",
      "evaluation_steps": [
        "Compare the reported counts with the expected output and the script evidence.",
        "Check that the answer distinguishes inventory from simulation readiness and avoids claiming that no air loops means no HVAC.",
        "Penalize invented sizing, energy use or compliance claims."
      ],
      "evaluation_params": ["input", "actual_output", "expected_output"],
      "weight": 1,
      "threshold": 0.8
    }]
  }
}
```

Use `expected.output` for literal facts, not a private answer-key file copied from
tool output. Do not add an expected saved-model check: this skill must not save
one. The direct script checks establish input immutability; an LLM score alone
cannot prove no writes occurred.

The template's `package.include: [evals]` ships this small fixture and case,
including its literal `expected.output`, in installed packages. They are public
demo data. The evaluation runner withholds case files from the agent during a run;
their presence in a release is separate from that holdout. Removing `evals` from
`package.include` omits the fixture, but case files are still selected by the
current export contract. Keep shipped evaluation resources small.

For local agent/judge evaluation, install `[eval]`, copy `eval-run.example.yaml`
to an ignored local config, select models/credentials and edit exported plugin
paths. Claude needs `allowed_tools: [Read, Glob, Grep, Skill, Bash]` to execute the
bundled script; select a Python environment reachable in the configured agent
runtime. Codex's read-only mode is sufficient. Follow PLUGIN_EVALUATION.md for
explicit environment forwarding. Scores are advisory; do not put model keys or
LLM runs in release CI.

## 6. Validate, export and load both hosts

```bash
openstudio-si plugin check-project .
openstudio-si plugin validate ./plugin
mkdir -p dist
openstudio-si plugin export ./plugin --host all --output ./dist/plugin \
  > dist/export-report.json
openstudio-si plugin check-hosts dist/export-report.json
```

The native-host check requires Claude Code and Codex. Confirm the inventory is
one skill/zero servers, then load the intended host export and request a summary
of the fixture. A metadata-ready export is not evidence that the host enabled it.
Repeat exports into a fresh output directory. To run agent evaluation:

```bash
openstudio-si eval run --plugin ./plugin --cases ./plugin/evals/cases \
  --config ./eval-run.local.yaml --output ./outputs/evals/run-001
openstudio-si eval report --run ./outputs/evals/run-001
```

## 7. Create the repository and publish a plugin release

Initialize Git in this project, add the intended files and push to your own GitHub
repository. Merge the generated development instructions into your working process.
Before tagging, run `plugin check-project . --require-tracked` locally; a fresh CI
checkout cannot recover files that Git ignored.

Configure CI with a reviewed full harness workflow commit SHA and an exact
published tooling version, then commit the resulting workflow/state files:

```bash
openstudio-si plugin configure-ci . \
  --workflow-ref <reviewed-40-character-harness-SHA> \
  --tooling-version <published-version-supporting-the-workflows>
```

Register the plugin ID, manifest owner and GitHub source repository with the
marketplace maintainer. After conformance CI passes, publish GitHub release
`v0.1.0` (matching the manifest). The reusable source workflow attaches the two
host archives plus `release.json`. The central publishing pipeline verifies them
against the registered repository and source commit, then proposes a scoped
marketplace PR. Maintainers review/merge it; release publication does not grant
an unreviewed plugin installation. See PLUGIN_PUBLISHING.md for deployment and
registry details. Your plugin needs no PyPI release or marketplace write token.
