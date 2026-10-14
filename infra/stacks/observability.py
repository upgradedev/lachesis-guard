from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct


class ObservabilityStack(cdk.Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, foundation, decider, agent, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.foundation = foundation
        self.decider = decider
        self.agent = agent
        # Built in its own step (B3); see STATE.md Phase 2.
