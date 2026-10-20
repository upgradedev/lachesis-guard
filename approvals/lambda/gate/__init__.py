"""The merge gate (B7b): a pull-request check that needs no developer setup.

A GitHub Actions workflow in the repository (.github/workflows/
lachesis-gate.yml) names the pull request to POST /gate/pulls with a
GitHub OIDC token, and polls GET /gate/{id}. The gate reads the pull
request and its changed files from GitHub itself with its read-only
token, so the workflow never chooses what is scanned. Nothing is
installed or configured on a developer's machine.

- ``policy``: the request contract, the PullRequest built from GitHub's
  file list, the deterministic scan of each changed file (activity.scan,
  the same rules as the commit ingest), the verdict summary and the
  approver card. Pure Python: no boto3, no clock, no I/O.
- ``decider``: an optional, advisory Strands Decider 2B reading of the
  flagged changes, asked only when the inference component already has
  a copy running. It never invokes a component without a copy and
  never clears a flag.

The gate itself is the deterministic scan: a change it flags needs a
person, whatever Decider says. gate_api.py is the Lambda entrypoint. It
is not yet a required check, so it enforces nothing yet.
"""
