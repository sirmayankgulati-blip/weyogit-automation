---
name: Python runtime packages
description: Python dependencies need the installable Python Tools module in this environment.
---

Use an installable `python-3.x` tools module before installing Python packages. The
base Python module is externally managed and does not include pip, so global
package installation is blocked by the immutable Nix environment.

**Why:** The first dependency install against the base Python failed with PEP 668
because it could not modify the Nix store; the Python Tools module installed and
provided a working package environment.

**How to apply:** For future Python automation, check available Python modules,
install a `python-3.x` tools module if needed, then install dependencies through
the package-management workflow.