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
| any answer below the threshold | `Confirm`: the agent loop pauses for a person |
| exfiltration, confidently | `Deny`: the call is refused |
| destructive write, untrusted input acted on, or confused deputy | `Confirm` |
| benign but not grounded | `Guide`: the model asks the user instead of guessing |
| benign, grounded, confident | `Proceed` |

**This pattern is never the gate.** The gate behind it is deterministic.
Measure the threshold on your own traffic before you trust it; 0.9 is the
model card's figure for short classification, not a measured value for you.

## Five minutes

1. `pip install strands-agents`
2. `strands-decider serve <model> --device cpu --port 8099`
3. `python example.py`, with AWS credentials for Bedrock. The eager agent tries
   to shred a note; the triage pauses it, you answer `y` or `n`, and the log at
   the end shows every proposed call with its risk class and confidence.
4. Wire it into your agent:

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

- Rename the risk classes and rewrite the criteria in `RISK_CLASSES` to match
  the calls your agent makes. The `true`/`false` descriptions on the grounding
  question carry most of its signal; write them carefully.
- Pick the threshold by measurement: run your own tool calls through, plot
  confidence against right and wrong, and set the line where a person should
  take over. Our own measurement lands in the repo README when it runs.
- Keep `Confirm` for the calls a person must see and `Deny` for the ones that
  must never run. A triage that denies when unsure takes the human out of a
  decision the human should make.

## Provenance

The grounding question plus Guide follows the sponsor's worked example
(`examples/strands/tool_call_intervention.py` in strands-decider). The
five-class risk question, the Confirm and Deny split with the human pause, and
the threshold measured on our own traffic are this pattern's own.
