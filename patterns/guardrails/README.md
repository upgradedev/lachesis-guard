# Reusable decision pattern: triage every tool call before it runs

A Strands `InterventionHandler` that asks Strands Decider 2B two typed questions
before every tool call, and turns the answers into Proceed, Guide, Confirm or
Deny with plain Python. Copy `triage.py` into your own agent; it needs only
`strands-agents` from PyPI and a running Decider server.

Lachesis scores every tool call for risk before it runs, so the agent pauses
for a person instead of cleaning up after it.

## The two questions

1. **Risk class** (choice): `benign`, `destructive_write`, `exfiltration`,
   `untrusted_to_action`, `confused_deputy`. The option names are read from the
   request, not baked into the model, so change them to fit your agent.
2. **Grounding** (yes/no): does every argument value trace back to something the
   user or the cited evidence actually provided?

Both are asked in one round trip: the state is encoded once and shared.

## The policy

First match wins. `decide` in `triage.py` owns this table, not the model.

| Decider says | The pattern returns |
|---|---|
| risk confidence below the threshold | `Confirm`: the agent loop pauses for a person |
| exfiltration, confidently | `Deny`: the call is refused |
| destructive write, untrusted input acted on, or confused deputy | `Confirm` |
| benign but not grounded | `Guide`: the model asks the user instead of guessing |
| benign, grounded, confident | `Proceed` |

**This pattern is never the gate.** Decider only measures. The gate is the
deterministic code in `triage.py`: two pre-checks that never call the model
(safe reads proceed, secret-shaped arguments pause), and the table above.
Measure the threshold on your own traffic before you trust it; 0.9 is the
model card's figure for short classification, not a measured value for you.

Pauses bind: a Confirm is cached under the call id and re-issued verbatim on
resume without calling Decider, so the human answer stays bound to the call.
Exact repeats of refused calls are denied outright. The model never sees a
score: Deny and Guide texts are neutral, and a denied pause reads "a person
declined this call".

## About a minute

Run the serve line in a second terminal; it stays in the foreground:

```sh
pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cpu
pip install strands-decider==0.1.0 transformers==5.19.0 strands-agents
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --device cpu --port 8099
python example.py
```

The tour drives four scripted tool calls through the handler and prints each
verdict: a Proceed, a Guide, a Confirm with the pause a person would see, and
a Deny. It takes about 40 seconds on CPU once the server is up (40.7 s on an
EC2 t3.xlarge, 40.2 s in CI run 38383159931), plus the one-off model download.
It needs no AWS account. A clean-container CI job runs the same pinned install
and fails unless all four verdicts fire.

The server needs a machine that fits the 2B model in fp32 on CPU: we run an
EC2 t3.xlarge (4 vCPU, 16 GB RAM); a t3.large (2 vCPU, 8 GB) OOM-killed while
loading the model.

With AWS credentials, `python example.py --agent` runs the same triage around
a small Bedrock agent instead. The model comes from `LACHESIS_EXAMPLE_MODEL`
and the region from the usual AWS variables; there are no other knobs.

Wire it into your agent:

```python
from strands import Agent
from triage import TriageHandler, run_with_human

agent = Agent(model=..., tools=[...], interventions=[TriageHandler()])
result = run_with_human(agent, "do the task", lambda i: input(f"{i.reason} (y/n): "))
```

`STRANDS_DECIDER_URL` points the pattern at a server that is not on
`http://127.0.0.1:8099`. If the server is down, the run stops: an agent that
cannot be triaged does not run unguarded.

## Make it yours

- Rename the risk classes in `RISK_CLASSES` and rewrite the criteria to match
  the calls your agent makes, and rename them in `CONFIRM_CLASSES`,
  `DENY_CLASSES` and `BENIGN_LABEL` in the same edit: importing `triage.py`
  fails loudly if the names disagree. The `true`/`false` descriptions on the
  grounding question carry most of its signal; write them carefully.
- Pick the threshold by measurement: run your own tool calls through, plot
  confidence against right and wrong, and set the line where a person should
  take over. Our own measurement is in the repo README under Threshold: 0.7,
  fit on 50 labeled tool calls.
- Keep `Confirm` for the calls a person must see and `Deny` for the ones that
  must never run. A triage that denies when unsure takes the human out of a
  decision the human should make.

## Provenance

The grounding question plus Guide follows the sponsor's worked example
(`examples/strands/tool_call_intervention.py` in strands-decider). The
five-class risk question, the Confirm and Deny split with the human pause, and
the threshold measured on our own traffic are this pattern's own.
