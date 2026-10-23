# Reusable decision pattern: check every tool call before it runs

A Strands `InterventionHandler` that checks every tool call before it runs.
Two deterministic pre-checks settle what they can. Strands Decider 2B scores
every other call with two typed questions, and plain Python turns the answers
into Proceed, Guide, Confirm or Deny. Copy `triage.py` into your own agent; it
needs only `strands-agents` from PyPI and a running Decider server.

The agent pauses for a person before a risky call runs, instead of someone
cleaning up after it.

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

**The model is never the gate.** Decider only measures. The gate is the
deterministic code in `triage.py`: two pre-checks that never call the model
(safe reads proceed, secret-shaped arguments pause), and the table above.
Measure the threshold on your own traffic before you trust it; 0.9 is the
model card's figure for short classification, not a measured value for you.

Pauses bind: a Confirm is cached under the call id and re-issued verbatim on
resume without calling Decider, so the human answer stays bound to the call.
Exact repeats of refused calls are denied outright. The model never sees a
score: Deny and Guide texts are neutral, and a denied pause reads "a person
declined this call".

## The tour, about two minutes from a clean install

Before you start:

- A machine that fits the 2B model in fp32 on CPU. We run an EC2 t3.xlarge
  (4 vCPU, 16 GB RAM); a t3.large (2 vCPU, 8 GB) was OOM-killed while loading
  the model.
- Python 3.11 in a fresh virtual environment, as in the CI job.
- Network access for the first serve: it downloads the model from Hugging
  Face once.
- No AWS account. The tour needs none.

Get the files and install the pinned versions:

```sh
git clone https://github.com/upgradedev/lachesis-guard
cd lachesis-guard/patterns/guardrails
pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cpu
pip install strands-decider==0.1.0 transformers==5.19.0 strands-agents==1.61.1
```

Start the server in a second terminal. It stays in the foreground:

```sh
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --device cpu --port 8099
```

Wait until it is ready: repeat this check until it prints a JSON line that
starts with `{"status":"ok"`. The CI job polls it every 5 seconds:

```sh
curl -sf http://127.0.0.1:8099/health
```

Then run the tour from `patterns/guardrails`:

```sh
python example.py
```

The tour drives four scripted tool calls through the handler and prints each
verdict: a Proceed, a Guide, a Confirm with the pause a person would see, and
a Deny. These are the verdict lines from CI run
[39273986426](https://github.com/upgradedev/lachesis-guard/actions/runs/39273986426)
(2026-10-19, commit b37e1fe), against the real model:

```
[1/4] search {'query': 'refund'} -> proceed (benign at 0.83, grounded=True)
[2/4] list_paths {'path': 'src'} -> guide (benign at 0.83, grounded=False)
[3/4] delete_file {'path': 'build/'} -> confirm (destructive_write at 0.87, grounded=True)
[4/4] send_email {'body': 'customer records attached', 'to': 'stranger@example.net'} -> deny (exfiltration at 0.84, grounded=True)
tour: proceed guide confirm deny in 37.6 s
```

The last line must read `tour: proceed guide confirm deny`; the seconds vary
by machine. In that run the whole job took about 2 minutes on a clean
container: install 38 s, model download and serve 40 s, tour 39 s. The
[pattern-tour](../../.github/workflows/pattern-tour.yml) workflow runs the
same pinned install and fails unless that closing line appears.

With AWS credentials and `LACHESIS_EXAMPLE_MODEL` set to a Bedrock model id,
`python example.py --agent` runs the same triage around a small Bedrock agent
instead. The region comes from the usual AWS variables; there are no other
knobs.

Wire it into your agent:

```python
from strands import Agent
from triage import TriageHandler, run_with_human

agent = Agent(model=..., tools=[...], interventions=[TriageHandler()])
result = run_with_human(agent, "do the task", lambda i: input(f"{i.reason} (y/n): "))
```

`STRANDS_DECIDER_URL` points the pattern at a server that is not on
`http://127.0.0.1:8099`. If the server is down, every call the pre-checks do
not settle pauses for a person with "the Decider did not answer, so a person
decides": nothing runs on a guess, and nothing runs without a person.

## Make it yours

- Rename the risk classes in `RISK_CLASSES` and rewrite the criteria to match
  the calls your agent makes, and rename them in `CONFIRM_CLASSES`,
  `DENY_CLASSES` and `BENIGN_LABEL` in the same edit: importing `triage.py`
  fails loudly if the names disagree. The `true`/`false` descriptions on the
  grounding question carry most of its signal; write them carefully.
- Pick the threshold by measurement: run your own tool calls through, plot
  confidence against right and wrong, and set the line where a person should
  take over. Our line, 0.70, is a chosen operating point on 50 labeled tool
  calls. At 0.70 all 12 accepted fit cases were right, but only 9 of them
  reach Decider under the shipped routing, one short of our rule's own
  minimum of 10. The repo README's Threshold section has the full
  calibration.
- Keep `Confirm` for the calls a person must see and `Deny` for the ones that
  must never run. A triage that denies when unsure takes the human out of a
  decision the human should make.

## Provenance

The grounding question plus Guide follows the sponsor's worked example
(`examples/strands/tool_call_intervention.py` in strands-decider). The
five-class risk question, the Confirm and Deny split with the human pause, and
the threshold measured on our own traffic are this pattern's own.
