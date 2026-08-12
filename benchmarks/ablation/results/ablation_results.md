# Component Ablation Results
**Embedder:** all-MiniLM-L6-v2 | **Queries:** 20 | **Corpus:** domain_corpus (111 chunks)

> **Interpretation caveat:** on this 111-chunk corpus every ablation
> configuration achieves identical (or near-identical) accuracy — the
> corpus is too small and too easy to discriminate the quality
> contribution of individual components. Only the LATENCY column is a
> meaningful comparison; the accuracy columns are a null result, not
> evidence that the ablated components add no value.

## Results

| Configuration | Room Acc | Locus Acc | KW Recall | Latency | Δ Room | Δ Locus | Δ KW |
|---|---|---|---|---|---|---|---|
| FULL | 1.0000 | 1.0000 | 0.9608 | 78.8ms | +0.0000 | +0.0000 | +0.0000 |
| NO_TFC | 1.0000 | 1.0000 | 0.9608 | 11.9ms | +0.0000 | +0.0000 | +0.0000 |
| NO_VM | 1.0000 | 1.0000 | 0.9608 | 11.9ms | +0.0000 | +0.0000 | +0.0000 |
| NO_GRAPH | 1.0000 | 1.0000 | 0.9608 | 12.0ms | +0.0000 | +0.0000 | +0.0000 |
| BM25_ONLY | 1.0000 | 1.0000 | 0.9483 | 0.8ms | +0.0000 | +0.0000 | -0.0125 |
| DENSE_ONLY | 1.0000 | 1.0000 | 0.9608 | 10.6ms | +0.0000 | +0.0000 | +0.0000 |