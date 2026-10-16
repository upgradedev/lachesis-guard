# Running the agent on AWS

The hackathon asks for a working agent on AWS. The judged deployment is
the hosted stack (README, "What runs where": AgentCore Runtime plus a
SageMaker Decider endpoint). This page documents the earlier all-on-one-
EC2-box run, which is how the filmed demo was produced and remains the
simplest way to reproduce it locally on AWS.

## Where each part runs

- **The agent** (`scripts/demo_agent.py`, a Strands agent with the Lachesis
  `InterventionHandler`) runs on the EC2 box, from the repo's own `.venv`.
  The same script also runs on a laptop unchanged.
- **The generative model** is Amazon Bedrock in eu-west-1
  (`eu.anthropic.claude-sonnet-5-5`). The box reaches it with an instance
  role and nothing else: no access keys exist on the box, and the launcher
  refuses to run if static AWS keys appear in its environment.
- **Strands Decider 2B** (pinned
  `StrandsAgents/strands-decider-2B-qwen3.5-v1-2610`) serves on the same
  box at `127.0.0.1:8099`. The agent talks to it over localhost; no tunnel
  and no open port. On this box the model runs on CPU, which is why a
  triage takes seconds (the GPU figures live in
  [GPU_2026-10-13.md](evidence/GPU_2026-10-13.md)).
- **The human** answers the pause at the box's terminal. Session state is
  kept next to the run's workdir, outside the agent's file jail, so a
  paused run survives process death exactly as it does on a laptop.

## The instance role

Role `lachesis-demo-box`, attached to the box as an
instance profile. Its only policy allows `bedrock:InvokeModel` and
`bedrock:InvokeModelWithResponseStream` on:

- `arn:aws:bedrock:eu-west-1:<account-id>:inference-profile/eu.anthropic.claude-sonnet-5-5`
- `arn:aws:bedrock:eu-*::foundation-model/anthropic.claude-sonnet-5-5`

The second resource is not optional: a cross-region inference profile
routes to the foundation model in several EU regions, and Bedrock checks
the routed model too. The role still names exactly one model, in the EU
only. Nothing else is allowed, and the box holds no other credentials.

## The run

Launcher: `scripts/box_demo.sh`. It clones or updates this repo, builds
`.venv` if missing (the box's system botocore predates the agent's
streaming calls), checks the server answers with the pinned checkpoint,
and runs the demo. First proved on 2026-10-13 on instance
i-0c25a473fb2216b3f (t3.xlarge, eu-west-1), log kept with the owner.

What the log shows: the read proceeded; `run_tests` paused at confidence
0.50 (below the 0.70 threshold) and was approved; the refund write was
guided back as ungrounded; and the staging-key write was stopped by the
deterministic secret scan before Decider was asked, then refused by the
person at the terminal. The agent reported both stops, suggested reading
the key from an environment variable, and did not work around either one.

To reproduce: give a small EC2 box the role above, serve the pinned model
on port 8099, and run `bash scripts/box_demo.sh` in a terminal.
