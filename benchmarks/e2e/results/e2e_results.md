# End-to-End Memory Quality Benchmark Results

**Embedder:** all-MiniLM-L6-v2 | **Sessions:** 7 | **Query turns:** 28
**Bootstrap:** 1000 samples (95% CI)

> **Methodology disclosure:** grading is embedding-similarity
> self-evaluation — expected facts and retrieved chunks are encoded
> with the same embedder used for retrieval, and a recall hit is
> cosine similarity > 0.4. Expected facts are stored verbatim, so
> matched chunks score ~1.0 similarity by construction. This is a
> persistence smoke test, not an external quality comparison; there
> is no LLM judge and no comparison to other memory systems.

## Aggregate Metrics

| Metric | Mean | 95% CI |
|---|---|---|
| Recall@1 | 0.9643 | [0.8929, 1.0000] |
| Recall@5 | 1.0000 | [1.0000, 1.0000] |
| Recall@10 | 1.0000 | [1.0000, 1.0000] |
| Semantic Similarity | 1.0000 | [1.0000, 1.0000] |
| Avg Latency (ms) | 11.39 | [9.88, 13.11] |

## Per-Session Results

| Persona | Queries | Avg R@10 | Avg Similarity | Avg Latency |
|---|---|---|---|---|
| Security Consultant | 4 | 1.0000 | 1.0000 | 14.5ms |
| ML Engineer | 4 | 1.0000 | 1.0000 | 12.6ms |
| Medical Researcher | 4 | 1.0000 | 1.0000 | 14.0ms |
| QA Assistant | 4 | 1.0000 | 1.0000 | 10.1ms |
| Architecture Reviewer | 4 | 1.0000 | 1.0000 | 10.5ms |
| Climate Analyst | 4 | 1.0000 | 1.0000 | 7.6ms |
| Database Admin | 4 | 1.0000 | 1.0000 | 10.4ms |

## Per-Query Details

- **[PASS]** `Security Consultant` turn 3: _What cloud provider does AlphaCorp use for primary hosting and what is their main database setup?_ (R@10=1.00, sim=1.000, 16.53ms)
- **[PASS]** `Security Consultant` turn 3: _How many transactions per hour does BetaFinance process at peak?_ (R@10=1.00, sim=1.000, 16.11ms)
- **[PASS]** `Security Consultant` turn 4: _What was AlphaCorp CISO's deadline for fixing critical vulnerabilities and how many were IAM-related_ (R@10=1.00, sim=1.000, 13.08ms)
- **[PASS]** `Security Consultant` turn 4: _What issues did BetaFinance's 2024 audit find regarding their secrets management?_ (R@10=1.00, sim=1.000, 12.41ms)
- **[PASS]** `ML Engineer` turn 3: _What is the validation accuracy of the latest model and how does it compare to the previous version?_ (R@10=1.00, sim=1.000, 23.37ms)
- **[PASS]** `ML Engineer` turn 3: _What hyperparameter values did Optuna select as optimal for this training run?_ (R@10=1.00, sim=1.000, 12.23ms)
- **[PASS]** `ML Engineer` turn 4: _What is the INT8 model size after quantization and what was the accuracy impact?_ (R@10=1.00, sim=1.000, 6.64ms)
- **[PASS]** `ML Engineer` turn 4: _How is data drift monitored and what is the current alert threshold?_ (R@10=1.00, sim=1.000, 8.28ms)
- **[PASS]** `Medical Researcher` turn 3: _What was the primary endpoint of the TRILUMINATE trial and was it met?_ (R@10=1.00, sim=1.000, 8.01ms)
- **[PASS]** `Medical Researcher` turn 3: _For which cancer type did KEYNOTE-024 establish pembrolizumab as first-line therapy and what was the_ (R@10=1.00, sim=1.000, 14.42ms)
- **[PASS]** `Medical Researcher` turn 4: _What quality-of-life metric was used in TRILUMINATE and what was the between-group difference?_ (R@10=1.00, sim=1.000, 20.37ms)
- **[PASS]** `Medical Researcher` turn 4: _What genomic biomarker was validated by KEYNOTE-158 for tissue-agnostic pembrolizumab approval?_ (R@10=1.00, sim=1.000, 13.05ms)
- **[PASS]** `QA Assistant` turn 3: _What key features were added in Python 3.13 and when was it released?_ (R@10=1.00, sim=1.000, 8.17ms)
- **[PASS]** `QA Assistant` turn 3: _Who discovered the xz utils backdoor and what was the immediate symptom they noticed?_ (R@10=1.00, sim=1.000, 12.08ms)
- **[PASS]** `QA Assistant` turn 4: _What new database features did Django 5.1 introduce?_ (R@10=1.00, sim=1.000, 13.96ms)
- **[PASS]** `QA Assistant` turn 4: _What was the root cause of the CrowdStrike outage and how many devices were affected?_ (R@10=1.00, sim=1.000, 6.05ms)
- **[PASS]** `Architecture Reviewer` turn 3: _What payment providers does the payment gateway support and how does it ensure safe retries?_ (R@10=1.00, sim=1.000, 8.28ms)
- **[PASS]** `Architecture Reviewer` turn 3: _What is the total pipeline time from commit to production deployment?_ (R@10=1.00, sim=1.000, 9.91ms)
- **[PASS]** `Architecture Reviewer` turn 4: _What notification channels does notify-hub support and what are the priority tiers?_ (R@10=1.00, sim=1.000, 6.46ms)
- **[PASS]** `Architecture Reviewer` turn 4: _What scaling mechanisms are used and what is the cold start time?_ (R@10=1.00, sim=1.000, 17.44ms)
- **[PASS]** `Climate Analyst` turn 3: _What is EcoCoat's Scope 1 emissions for 2023 and what is their main source?_ (R@10=1.00, sim=1.000, 7.97ms)
- **[PASS]** `Climate Analyst` turn 3: _How many solar farms does SolarGrid operate and what is their total capacity?_ (R@10=1.00, sim=1.000, 7.26ms)
- **[PASS]** `Climate Analyst` turn 4: _What are EcoCoat's targeted emission reductions by 2026 and through which initiatives?_ (R@10=1.00, sim=1.000, 6.9ms)
- **[PASS]** `Climate Analyst` turn 4: _What battery storage does the Antelope Valley project include and what is its duration?_ (R@10=1.00, sim=1.000, 8.42ms)
- **[PASS]** `Database Admin` turn 3: _What replication feature did PostgreSQL 16 add and what backup feature did PostgreSQL 17 add?_ (R@10=1.00, sim=1.000, 13.28ms)
- **[PASS]** `Database Admin` turn 3: _What new expiration capability did Redis 7.4 introduce?_ (R@10=1.00, sim=1.000, 8.71ms)
- **[PASS]** `Database Admin` turn 4: _What are the key PostgreSQL configuration values for the production cluster and what is the RPO?_ (R@10=1.00, sim=1.000, 6.99ms)
- **[PASS]** `Database Admin` turn 4: _What is the Redis migration strategy for the 7.4 upgrade and what is the rollback plan?_ (R@10=1.00, sim=1.000, 12.65ms)