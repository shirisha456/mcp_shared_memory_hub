# Retrieval evaluation

Strategy: **full-text with any-term fallback (ts_rank_cd + importance, recency, type priors)**  
Corpus: 200 memories  
Queries: 34 (31 answerable)  
Cutoff: k=10

| Metric | Value | Target |
|---|---|---|
| nDCG@10 | 0.802 | higher is better |
| Recall@10 | 0.817 | higher is better |
| Precision@10 | 0.691 | higher is better |
| MRR | 0.823 | secondary |
| **Stale inclusion rate** | **0.000** | **exactly 0** |
| Empty for unanswerable | 0.667 | 1.0 |

Means are over answerable queries only. A query with nothing to find cannot be ranked well or badly, so including it would just dilute the score with zeros.

Stale inclusion is computed over *all* queries, including the cross-project traps, and is a correctness metric rather than a quality one - it is never traded against nDCG.

## How this got here

Each row is a retrieval strategy, measured against the same corpus and
the same judgments. The judgments were written before any of them ran.

| Strategy | nDCG@10 | Recall@10 | Precision@10 | Stale |
|---|---|---|---|---|
| full-text, all terms required (ts_rank_cd + priors) | 0.478 | 0.468 | 0.484 | 0.000 |
| full-text, any-term fallback when the strict query finds nothing | 0.802 | 0.817 | 0.691 | 0.000 |
| hybrid: full-text + pgvector, fused by RRF, distance <= 0.35 | 0.853 | 0.828 | 0.671 | 0.000 |

**full-text, all terms required (ts_rank_cd + priors)** - PostgreSQL joins bare query terms with AND, so a natural-language question needs every word present. 'migration rules' and 'connection pool size' returned nothing at all.

**full-text, any-term fallback when the strict query finds nothing** - Precision is preserved for queries that match strictly; the widening only happens when the alternative is returning nothing. Stale inclusion stayed at exactly zero, which is the useful part: loosening the match did not loosen the correctness guarantee, because suppression is structural rather than a ranking effect.

**hybrid: full-text + pgvector, fused by RRF, distance <= 0.35** - Beats full text on ranking and recall. The threshold is the interesting part: without one, nDCG reached 0.881 but precision collapsed to 0.113 and every unanswerable query returned ten results, because approximate nearest neighbour search returns the k closest vectors whether or not anything is close. 0.35 was chosen by sweeping against this corpus - see docs/eval/threshold-sweep.md. Hybrid is worse at recognising an unanswerable question (0.667 -> 0.333), which is the honest cost. Measured with BAAI/bge-small-en-v1.5 locally; CI runs the lexical evaluation only.

## Weakest queries

Where the current strategy does worst. These are the cases a later retriever has to improve, and the reason the baseline is recorded before that retriever is built.

| Query | nDCG | Recall |
|---|---|---|
| `q13` 'deadlock prevention' | 0.000 | 0.000 |
| `q22` 'jwt' | 0.000 | 0.000 |
| `q31` 'what is being worked on right now' | 0.000 | 0.000 |
| `q29` 'how is the server deployed' | 0.174 | 0.500 |
| `q11` 'how are concurrent updates handled' | 0.562 | 0.667 |
