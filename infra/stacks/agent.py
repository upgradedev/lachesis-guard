from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct


class AgentStack(cdk.Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, foundation, decider, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.foundation = foundation
        self.decider = decider
        # Built in its own step (A6); see STATE.md Phase 2.
