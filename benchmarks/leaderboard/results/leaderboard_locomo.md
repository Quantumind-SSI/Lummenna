# Memory Framework Retrieval Leaderboard

## Read this first

> This leaderboard is published by the maintainer of Lummenna, which is one of the ranked systems. Results are not independently audited. Treat them as a reproducible starting point, not a neutral verdict.

> No standards body endorses this leaderboard. It is not an official ranking and does not claim to be one.

This measures **retrieval only**: adapters return evidence ids, which are
compared to dataset ground truth. No LLM judges anything, so no score
depends on which generator a framework is paired with.

## Evaluation set

- **Dataset:** `locomo` (https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json)
- **Fingerprint:** `bc9db8b022d981a8e0c5bcf34c4f8e9d51569ffc52dfe655b98fc1d7c22be7e3`
- **Items:** 5882 | **Questions:** 100 (87 answerable, 13 abstention)
- **Protocol:** top_k=10, seed=42, 1000 bootstrap samples
- **Artifact SHA-256:** `f153e785d9e5f729ed2735af6a739fd8aef700999237fbee179dee5e25644752`

Two runs are comparable only if their dataset fingerprints match.

## Results

| Framework | Recall@1 | Recall@5 | Recall@10 | MRR | nDCG@10 | Abstention | Query ms |
|---|---|---|---|---|---|---|---|
| Lummenna (self-authored, unverified) | 0.069 [0.023, 0.126] | 0.203 [0.125, 0.282] | 0.332 [0.240, 0.429] | 0.155 [0.097, 0.216] | 0.186 [0.126, 0.245] | 0.000 [0.000, 0.000] | 34.92 |
| BM25 (baseline) (self-authored, unverified) | 0.086 [0.040, 0.138] | 0.212 [0.132, 0.287] | 0.258 [0.170, 0.345] | 0.171 [0.109, 0.239] | 0.177 [0.116, 0.239] | 0.000 [0.000, 0.000] | 12.48 |
| Recency window (baseline) (self-authored, unverified) | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.00 |

Values are `mean [95% CI]`. **Overlapping intervals mean the difference is
not statistically resolved at this sample size.** Rank order alone is not
evidence of superiority.

## By question type

Aggregate scores hide the differences that matter. Memory systems tend to
separate on temporal reasoning and knowledge updates, not on simple recall.

| Framework | Category | Recall@10 | MRR | n |
|---|---|---|---|---|
| BM25 (baseline) | locomo-cat-1 | 0.040 [0.000, 0.105] | 0.070 [0.000, 0.193] | 19 |
| BM25 (baseline) | locomo-cat-2 | 0.377 [0.184, 0.561] | 0.309 [0.135, 0.502] | 19 |
| BM25 (baseline) | locomo-cat-3 | 0.125 [0.000, 0.375] | 0.083 [0.000, 0.250] | 4 |
| BM25 (baseline) | locomo-cat-4 | 0.311 [0.189, 0.444] | 0.163 [0.081, 0.256] | 45 |
| BM25 (baseline) | locomo-cat-5 | n/a | n/a | 13 |
| Recency window (baseline) | locomo-cat-1 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 19 |
| Recency window (baseline) | locomo-cat-2 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 19 |
| Recency window (baseline) | locomo-cat-3 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 4 |
| Recency window (baseline) | locomo-cat-4 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 45 |
| Recency window (baseline) | locomo-cat-5 | n/a | n/a | 13 |
| Lummenna | locomo-cat-1 | 0.089 [0.011, 0.179] | 0.078 [0.006, 0.192] | 19 |
| Lummenna | locomo-cat-2 | 0.693 [0.482, 0.895] | 0.266 [0.141, 0.405] | 19 |
| Lummenna | locomo-cat-3 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 4 |
| Lummenna | locomo-cat-4 | 0.311 [0.189, 0.456] | 0.154 [0.074, 0.245] | 45 |
| Lummenna | locomo-cat-5 | n/a | n/a | 13 |

## Provenance

- **Platform:** Darwin/arm64
- **Commit:** `d03defa0f112f6e67d64da878f49d944b987c353`
- **Packages:** datasets==5.0.1, lummenna==1.0.0, numpy==2.5.2, scipy==1.18.1, sqlite-vec==0.1.9, torch==2.13.0, transformers==4.57.6, usearch==2.26.1
- **Captured:** 2026-09-14T17:35:22+00:00

### Adapter provenance

- Authored by the leaderboard maintainer: BM25 (baseline), Lummenna, Recency window (baseline)
- Without independent verification: BM25 (baseline), Lummenna, Recency window (baseline)

Reproduce: `python -m benchmarks.leaderboard.run --help`

