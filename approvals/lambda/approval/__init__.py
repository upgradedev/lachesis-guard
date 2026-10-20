"""The Approval bounded context, in three layers.

- ``domain``: the Run aggregate, its transition table and its domain
  events. Pure Python: no boto3, no clock, no I/O.
- ``uow``: the DynamoDB Unit of Work. One command is one
  TransactWriteItems: the aggregate write, conditioned on the status and
  version it was loaded at, plus one put per new event.
- ``service``: the application commands the Lambda entrypoints call
  (api.py, runner.py, sweeper.py).
"""
