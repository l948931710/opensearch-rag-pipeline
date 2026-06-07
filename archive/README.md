# archive/

Retired one-off / recovery scripts moved out of the repo root during a 2026-06-07
cleanup. None of these are imported by the application or referenced by the build
(`.dockerignore` already excludes them), so they are kept here for reference only
rather than deleted.

| File | What it is |
|------|------------|
| `recover.py` | One-off recovery helper script. |
| `recovered_pipeline_nodes.py` | A recovered snapshot of `pipeline_nodes.py` from an earlier incident. Not the live module — see `opensearch_pipeline/pipeline_nodes.py` for current code. |
| `scratch.py` | Ad-hoc scratch/experiment script. |
| `convert_report.js` | Node script that converted `work_report.md` into a rendered report (uses root `node_modules/`). |

`recover.py` and `scratch.py` remain git-tracked (moved with `git mv`);
`recovered_pipeline_nodes.py` and `convert_report.js` are gitignored.
