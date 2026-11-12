"""Vercel entry point for the Weekly Review Pulse portal.

Vercel's Python runtime serves the module-level ``handler`` class. The pipeline
itself still runs in GitHub Actions; this function only serves the portal pages
and calls the GitHub API to trigger and poll the workflow.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pulse.web_portal import PortalHandler  # noqa: E402


class handler(PortalHandler):  # noqa: N801 - Vercel requires this exact name
    pass
