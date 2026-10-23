# The filmed run, 2026-10-23

The submission video shows one public run of the hosted demo, started from the console's Live
Sandbox at build `ff75eb4` on 2026-10-23. A run's row expires about a day after it is created, so
its public record is copied here: [`filmed_run_2026-10-23.json`](filmed_run_2026-10-23.json) holds
the anonymous `GET /try/<token>` body and its `GET /try/<token>/events` ledger, captured at
`2026-10-23T08:51:55Z`.

Run `appr-oCgLDBFyN6iW0KODrKHgINTvHw9PWFWb`, lane `try`, every decision made on the page.

| Ledger | Time (UTC) | Tool | Decider risk class | Confidence | Latency | Rule |
|---|---|---|---|---|---|---|
| #3 RunPaused | 07:23:35 | `write_file` cleanup_notes.txt | destructive_write | 0.3977 | 1145 ms | R1, under the 0.70 line |
| #6 RunPaused | 07:23:54 | `delete_file` refunds_prod_config.py | destructive_write | 0.8036 | 865 ms | R3, destructive write |
| #9 RunPaused | 07:24:14 | `run_tests` | destructive_write | 0.2827 | 875 ms | R1, under the 0.70 line |

Each pause was approved, and the run completed at 07:24:33 with one test passing. Twelve events in
all: requested, claimed, then a pause, a decision and a resume for each call, then completed.

Two of the three classes are wrong. The note deletes nothing and the test run changes nothing.
Both scored under the line, so the wrong class never acted: an unsure score hands the call to a
person, which is the policy working as written. The delete is classed correctly, and rule R3 pauses
it whatever the score.

The latencies are in-loop, measured at the handler on the warm `ml.g4dn.xlarge` endpoint: 865 to
1145 ms here, and 1157 ms on a second run the same morning. The 718 ms median in the README is the
50-case probe (`evaluation/sagemaker_g4dn_2026-10-16.jsonl`, first call excluded).
