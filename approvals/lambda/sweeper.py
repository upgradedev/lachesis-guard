"""Sweeper entrypoint: EventBridge invokes this on a schedule; it resumes
expired PENDING approvals with "no", closes stale STARTING and RESUMING
rows, and re-dispatches the resume of a decided run (see runner.sweep)."""

from __future__ import annotations

import runner


def handler(_event, context):
    return runner.sweep(context)
