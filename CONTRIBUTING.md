# Contributing

Thanks for helping improve Cytoscape Agent Skill.

## Before opening an issue

Search existing issues. Include OS/architecture, Python version, Cytoscape and Java versions, install mode, exact command, exit code, and the JSON response. Remove credentials, private file paths, and sensitive network data.

## Changes

Keep calculations inside Cytoscape. Do not guess command names or parameters; derive them from the running engine. Preserve the JSON stdout contract, guarded failure behavior, integrity checks, and safe config-writing defaults. Add or update focused tests for behavioral changes.

## Validate

```bash
python -m unittest discover -s tests -v
python tools/package.py --verify
```

If an engine is available, also run the relevant end-to-end workflow and record the actual Cytoscape version. Never report a historical audit as a fresh run.

## Pull requests

Describe the user-visible change, safety implications, commands run, and any platform not checked. Keep unrelated runtime files, downloaded engines, credentials, and private datasets out of the patch.
