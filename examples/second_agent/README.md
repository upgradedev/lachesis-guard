# Second agent on the same pattern

A database janitor: a different domain, a different tool set, the same
Lachesis triage. It exists to prove the reusable pattern in
[patterns/guardrails](../../patterns/guardrails/README.md) is a pattern
and not a one-off: copy `triage.py`, wire three lines, and a Strands
agent that can drop tables pauses for a person exactly like the refund
agent does.

- Tools: `run_query` (read) and `drop_table` (destructive), over an
  in-memory toy database.
- The opening call is a grounded read, which Decider can let proceed;
  the drop is the call that pauses.
- Offline proof: `tests/test_second_agent.py` drives the handler with
  scripted Decider answers and pins both outcomes.
- Live run (Decider server up, Bedrock credentials present):
  `python examples/second_agent/agent.py`.
