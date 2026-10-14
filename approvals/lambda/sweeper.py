"""Sweeper entrypoint: EventBridge invokes this on a schedule; it resumes
expired PENDING approvals with "no" (see runner.sweep)."""

from __future__ import annotations

import runner


def handler(_event, _context):
    return runner.sweep()
