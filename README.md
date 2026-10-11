# lachesis-guard

**The model proposes, Lachesis triages before the tool runs, the deterministic gate decides, and a human confirms anything below the threshold.**

No latency number here until our own CPU run prints one: 115 ms is the organizer's RTX 3090 figure.

A reusable Strands `InterventionHandler` that asks Strands Decider 2B two typed questions before every
tool call, the risk class and whether the arguments are grounded in what the user actually said, and
turns the answers into Proceed, Guide, Confirm or Deny with plain Python that the code owns, not the
model.

Built for the Agent Experience Hackathon: Strands Decider on AWS Builder Center, pattern `#guardrails`.

## Status

Scaffolding. Product code lands here. Nothing below is claimed as working yet; this README is rewritten
with measured numbers as each piece lands.

## Licence

MIT. See `LICENSE`.
