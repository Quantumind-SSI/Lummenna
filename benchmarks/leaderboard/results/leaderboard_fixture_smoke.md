# Memory Framework Retrieval Leaderboard

## Read this first

> This leaderboard is published by the maintainer of Lummenna, which is one of the ranked systems. Results are not independently audited. Treat them as a reproducible starting point, not a neutral verdict.

> No standards body endorses this leaderboard. It is not an official ranking and does not claim to be one.

This measures **retrieval only**: adapters return evidence ids, which are
compared to dataset ground truth. No LLM judges anything, so no score
depends on which generator a framework is paired with.

## Evaluation set

- **Dataset:** `fixture:smoke` (file:smoke.json)
- **Fingerprint:** `5aee4e70ea9ecd2fe379b228f796db208324a7be12776617870a1b26374ccac2`
- **Items:** 12 | **Questions:** 8 (6 answerable, 2 abstention)
- **Protocol:** top_k=10, seed=42, 1000 bootstrap samples
- **Artifact SHA-256:** `443148d9bf46b21198b7221735b7fa256b2edb3560157ba522f34d77119c0043`

Two runs are comparable only if their dataset fingerprints match.

## Results

| Framework | Recall@1 | Recall@5 | Recall@10 | MRR | nDCG@10 | Abstention | Query ms |
|---|---|---|---|---|---|---|---|
| BM25 (baseline) (self-authored, unverified) | 0.583 [0.167, 0.917] | 0.833 [0.500, 1.000] | 0.833 [0.500, 1.000] | 0.708 [0.374, 1.000] | 0.738 [0.405, 1.000] | 0.000 [0.000, 0.000] | 0.02 |
| Recency window (baseline) (self-authored, unverified) | 0.167 [0.000, 0.500] | 0.500 [0.167, 0.833] | 0.833 [0.500, 1.000] | 0.298 [0.103, 0.589] | 0.427 [0.222, 0.679] | 0.000 [0.000, 0.000] | 0.00 |

Values are `mean [95% CI]`. **Overlapping intervals mean the difference is
not statistically resolved at this sample size.** Rank order alone is not
evidence of superiority.

## By question type

Aggregate scores hide the differences that matter. Memory systems tend to
separate on temporal reasoning and knowledge updates, not on simple recall.

| Framework | Category | Recall@10 | MRR | n |
|---|---|---|---|---|
| BM25 (baseline) | abstention | n/a | n/a | 2 |
| BM25 (baseline) | knowledge-update | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 1 |
| BM25 (baseline) | multi-session-recall | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1 |
| BM25 (baseline) | single-session-recall | 1.000 [1.000, 1.000] | 0.750 [0.250, 1.000] | 3 |
| BM25 (baseline) | temporal-reasoning | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1 |
| Recency window (baseline) | abstention | n/a | n/a | 2 |
| Recency window (baseline) | knowledge-update | 1.000 [1.000, 1.000] | 0.143 [0.143, 0.143] | 1 |
| Recency window (baseline) | multi-session-recall | 1.000 [1.000, 1.000] | 0.333 [0.333, 0.333] | 1 |
| Recency window (baseline) | single-session-recall | 1.000 [1.000, 1.000] | 0.437 [0.111, 1.000] | 3 |
| Recency window (baseline) | temporal-reasoning | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 1 |

## Provenance

- **Platform:** Darwin/arm64
- **Commit:** `35b59fb2f907858148c0678ba66584737b3263c6` (working tree dirty)
- **Packages:** datasets==5.0.1, lummenna==1.0.0, numpy==2.5.2, scipy==1.18.1, sqlite-vec==0.1.9, torch==2.13.0, transformers==4.57.6, usearch==2.26.1
- **Captured:** 2026-09-14T09:37:08+00:00

### Adapter provenance

- Authored by the leaderboard maintainer: BM25 (baseline), Recency window (baseline)
- Without independent verification: BM25 (baseline), Recency window (baseline)

Reproduce: `python -m benchmarks.leaderboard.run --help`

