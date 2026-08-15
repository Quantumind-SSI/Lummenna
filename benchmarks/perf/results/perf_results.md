# Performance Benchmark Results

## Query Latency & Footprint

| Corpus Size | Ingestion (s) | DB Size (MB) | RAM (MB) | Latency p50 (ms) | Latency p95 (ms) | Latency p99 (ms) |
|---|---|---|---|---|---|---|
| 1,000 | 1.25 | 4.56 | 1145.4 | 15.76 | 17.6 | 18.65 |
| 10,000 | 34.6 | 43.63 | 1312.0 | 22.88 | 25.28 | 27.89 |
| 50,000 | 666.25 | 216.05 | 1730.83 | 51.35 | 79.41 | 94.36 |

## Write Amplification

| Corpus Size | Updates | Naive Time (ms) | Batch Time (ms) | Speedup |
|---|---|---|---|---|
| 1,000 | 1000 | 2.06 | 17.02 | 0.12x |
| 10,000 | 1000 | 2.08 | 54.05 | 0.04x |
| 50,000 | 1000 | 2.33 | 135.77 | 0.02x |
