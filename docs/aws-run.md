# Running the agent on AWS

The hackathon asks for a working agent on AWS. The judged deployment is
the hosted stack (README, "What runs where": AgentCore Runtime plus a
SageMaker Decider endpoint). The submission video shows one run of that
hosted stack, and its record is in
[FILMED_RUN_2026-10-23.md](evidence/FILMED_RUN_2026-10-23.md).

This page documents an earlier run on one EC2 box, not in the video.
The agent and Decider ran on that box, and the person answered at its
terminal. Amazon Bedrock in eu-west-1 supplied the generative model. It is
still the shortest way to run the whole loop yourself on AWS.

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

A second take ran on the same box later that day, and its log is also
kept with the owner. The figures below come from that take. The read
proceeded. `run_tests` paused at 0.50 and later at 0.69, both below the
0.70 threshold, and the person approved it each time. The refund write
came back benign at 0.80 but ungrounded, so Lachesis returned Guide. The
staging-key write was stopped by the deterministic secret scan before
Decider was asked, then refused by the person at the terminal.

The agent reported the Guide and the refused key write. It suggested
reading the key from an environment variable and did not work around
either stop. The first proof drew the same Guide on the same write, at
0.75. Across both box takes and the filmed hosted run, that refund write
is the only call that drew a Guide. The filmed run drew none.

In that second take, on CPU, a single in-loop triage on a long history
took up to 91 s. On the warm hosted GPU the filmed run's triages took 865 to
1145 ms each, measured at the handler.

To reproduce: give a small EC2 box the role above, serve the pinned model
on port 8099, and run `bash scripts/box_demo.sh` in a terminal.
