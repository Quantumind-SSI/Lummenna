# Contributing a framework adapter

## Why we do not implement your framework for you

The usual way a vendor benchmark goes wrong is that the vendor implements every
competitor, gets some of them subtly wrong, and publishes a table where they
win. The ranked vendors object that their system was misconfigured, and they are
usually right. The table then proves nothing.

So this harness does not ship adapters for frameworks we do not maintain. We
ship the protocol, our own adapter, and two baselines you can read in full. If
you maintain a memory framework and want it measured, you write the adapter, or
you review one someone else wrote.

Every result publishes `authored_by` and `verified_by`. An adapter we wrote for
someone else's system is labelled `self-authored` and `unverified` in the
rendered table, which is a visible discount on the claim.

## What your adapter must do

Implement `benchmarks/leaderboard/adapter.py::MemoryAdapter`. Six methods:

| Method | Contract |
|---|---|
| `info()` | Return `AdapterInfo`. Must not raise. |
| `available()` | `(True, "")` or `(False, reason)`. The reason is published, so name the missing dependency or credential. |
| `reset()` | Discard all state. Called before ingest. |
| `ingest(items)` | Store every item. Called once, timed as a whole. |
| `retrieve(question, top_k)` | Return up to `top_k` **dataset item ids**, best first, plus your own latency measurement. |
| `teardown()` | Release resources. Must be safe after a failed ingest. |

The single hard requirement: `retrieve` returns the exact `item_id` strings from
the dataset. Ground truth references those ids. An internal id, an offset, or a
paraphrase scores zero.

## Rules the runner enforces

**You may not read `question.answer_item_ids`.** The runner re-asks a sample of
questions three times: twice with the answers stripped, once with them present.
If the two stripped calls agree with each other but disagree with the third,
your adapter read the ground truth and the run is flagged.

**Non-determinism is measured, not punished.** If the two stripped calls
disagree, your system is stateful under repeated queries. That is a legitimate
design (Lummenna reinforces items on access and trips this itself), but it is
published, because it affects whether an independent rerun reproduces your
score.

**Ingest and query are timed separately.** Precompute all you like; it shows up
in your ingest column rather than nobody's.

**Failure is recorded, never scored.** An adapter that raises produces
`ERROR` with a traceback. One that cannot run produces `NOT_RUN` with your
stated reason. Neither is converted into a zero, because a zero asserts your
system performed badly, and that would be a false claim.

## Configuration honesty

Put the real configuration in `AdapterInfo.config_summary`, including any
non-default setting. If you tuned something for this benchmark, say so there.
A tuned result that discloses its tuning is useful. One that hides it is not.

## Submitting

1. Add `benchmarks/leaderboard/adapters/<framework>_adapter.py`.
2. Register it in `benchmarks/leaderboard/run.py::build_adapters`.
3. Add your framework to the `benchmarks` optional dependencies if it needs a
   package.
4. Verify offline first: `python -m benchmarks.leaderboard.run --dataset fixture`
5. Open a PR stating who authored the adapter and who, if anyone, verified it.

## Verifying someone else's adapter

If you maintain a framework and an adapter for it already exists, review it and
say so in a PR setting `verified_by`. That single field moves your row from
`unverified` to verified in the published table, and it is the cheapest
available improvement to the credibility of the whole leaderboard.

## Disputing a result

Open an issue with the artifact SHA-256 and dataset fingerprint from the report
you are disputing. Both are printed in every run. If your objection is that the
adapter misconfigures your system, a PR fixing the adapter is the fastest route,
and we will rerun.
