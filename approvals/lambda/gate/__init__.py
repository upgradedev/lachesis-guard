"""The merge gate (B7b): a pull-request check that needs no developer setup.

A GitHub Actions workflow in the repository (.github/workflows/
lachesis-gate.yml) sends the pull request's changed files to POST
/gate/pulls with a GitHub OIDC token, and polls GET /gate/{id}. Nothing
is installed or configured on a developer's machine.

- ``policy``: the request contract (caps, validation), the deterministic
  scan of each changed file (activity.scan, the same rules as the
  commit ingest), the verdict summary and the approver card. Pure
  Python: no boto3, no clock, no I/O.
- ``decider``: an optional, advisory Strands Decider 2B reading of the
  flagged changes, asked only when the inference component already has
  a copy running. It never wakes the GPU and never clears a flag.

The gate itself is the deterministic scan: a change it flags needs a
person, whatever Decider says. gate_api.py is the Lambda entrypoint.
"""
