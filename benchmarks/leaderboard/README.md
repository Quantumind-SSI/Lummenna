# Memory Framework Retrieval Leaderboard

A reproducible harness for measuring how well memory frameworks retrieve the
right evidence from long, multi-session conversation histories.

## This is not an official ranking

It is published by the maintainer of Lummenna, which is one of the ranked
systems. No standards body endorses it. It is not independently audited. Every
artifact and every rendered report carries that disclosure, and the code that
emits it is derived from the run rather than hardcoded, so it cannot be quietly
dropped.

What it offers instead of authority is reproducibility: a pinned protocol,
dataset fingerprints, environment capture, and a `--help` you can run yourself.

## What is measured

**Retrieval, not generation.** Adapters return evidence ids, which are compared
to dataset ground truth. No LLM judges anything. Consequences:

- Results do not move when a judge model is swapped or upgraded.
- A framework cannot win by being paired with a stronger generator.
- What is isolated is the memory layer, which is the thing under test.

The cost of this choice: it does not measure answer quality. A system that
retrieves the right evidence and then summarises it badly scores well here. That
is a deliberate scope boundary, not an oversight.

## Metrics

| Metric | Meaning |
|---|---|
| Recall@k | Fraction of ground-truth evidence in the top k |
| MRR | Reciprocal rank of the first correct item |
| nDCG@10 | Rank-discounted gain, binary relevance |
| Abstention accuracy | Correctly returning **nothing** for unanswerable questions |
| Query latency | Per-question wall clock, measured inside the adapter |

Abstention is scored in its own column. Recall is undefined when the relevant
set is empty, so folding a made-up 0.0 or 1.0 into the recall average would
silently reward a different capability. Systems that always return their top-k
score 0.0 on abstention, which is the point: confidently retrieving irrelevant
evidence is what produces confidently hallucinated answers downstream.

Every number is `mean [95% bootstrap CI]` over 1000 resamples. **Overlapping
intervals mean the difference is not resolved.** Rank order alone is not
evidence.

## Datasets

| Dataset | What it tests | Source |
|---|---|---|
| LoCoMo | Very long multi-session dialogue, cross-session and temporal recall | `snap-research/locomo` |
| LongMemEval | Five question types incl. knowledge updates and abstention | `xiaowu0162/longmemeval` |
| `fixture` | Offline harness smoke test. **Not a benchmark.** | in-tree |

Public datasets are fetched from HuggingFace at run time. That network call
happens only in this developer-machine harness, never in the shipped package and
never on a path subject to `assert_sovereign`.

If a dataset cannot be fetched, the run exits non-zero and writes nothing. No
suite silently substitutes synthetic data for a named public dataset.

Fixture results are labelled `fixture:smoke` and are never comparable to
public-dataset results. With 8 questions the confidence intervals span most of
the unit interval, which the report states plainly.

## Baselines

Published in the same table, because most of what a memory framework claims is
its delta over trivial retrieval:

- **BM25** (Okapi, k1=1.5, b=0.75) over the flattened transcript. No sessions,
  no time, no memory model. The floor.
- **Recency window**: return the last k turns, ignore the question entirely.
  This is the sliding-context-window strategy, and it quantifies how much of a
  score comes from a dataset simply favouring recent content.

Both are implemented in `adapters/baselines.py` in full, so you can check they
are not strawmen.

## Running

```bash
pip install -e '.[benchmarks]'

# offline harness check, no downloads
python -m benchmarks.leaderboard.run --dataset fixture

# real evaluation
python -m benchmarks.leaderboard.run --dataset locomo --max-questions 200
python -m benchmarks.leaderboard.run --dataset longmemeval --max-questions 200

# baselines only, to check the harness is not shaped around Lummenna
python -m benchmarks.leaderboard.run --dataset locomo --no-lummenna
```

Lummenna's adapter requires a real ONNX embedder. Without one it reports
`NOT_RUN` rather than scoring mock vectors. Provision with
`LUMMENNA_AUTO_PROVISION_MODEL=1` (a one-time network fetch).

## Reproducing a published result

Two runs are comparable only when the **dataset fingerprint** matches. Both the
fingerprint and the artifact SHA-256 are printed in every report.

Each artifact records the platform, CPU count, RAM, Python version, relevant
package versions, the git commit, and whether the working tree was dirty. A
result from a dirty tree is not reproducible from any commit, and says so.

## Integrity checks

The runner re-asks a sample of questions three times: twice with ground truth
stripped, once with it present.

- Stripped calls agree with each other but differ from the third: **leakage**,
  the adapter read the answers. Flagged loudly.
- Stripped calls differ from each other: **non-deterministic**. Leakage is
  undecidable and reported as `null` rather than guessed as clean.

Lummenna itself trips the non-determinism check, because `_record_access`
reinforces items on retrieval. That is published rather than suppressed.

## Adding your framework

See `CONTRIBUTING.md`. Short version: we do not implement frameworks we do not
maintain, because we would get them wrong in our own favour. Write the adapter
or verify one, and the table will say who did which.
