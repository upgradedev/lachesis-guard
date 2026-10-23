"""The Activity bounded context (B7a): what happened in the real projects.

One Activity is one commit on the default branch of a registered
repository, as the read-only GitHub ingest saw it. The layers:

- ``registry``: the committed project registry (projects.json).
- ``domain``: the Activity value, the actor attribution (human,
  agent-assisted, bot) and the Co-Authored-By trailer mapping. Pure
  Python: no boto3, no clock, no I/O.
- ``scan``: the deterministic change scan. It is a deterministic scan,
  labelled as such everywhere it is shown; it is never a Decider score.
- ``github``: the read-only GitHub REST client (urllib, no new
  dependency), with pagination and rate-limit handling.
- ``store`` and ``bus``: the DynamoDB table lachesis-activity and the
  ActivityObserved events on lachesis-events.
- ``service``: the ingest application service the ingest Lambda calls.
- ``read``: the read model GET /projects and the activity routes answer
  with, including the private-repository redaction of the anonymous lane.

Privacy: no commit author address is read into an Activity. A trailer is
mapped to an agent name and dropped; only a commit's subject line is
kept, with anything address-shaped removed, and a subject holding
secret-shaped text is replaced by a fixed marker.
"""
