# Choosing the semantic distance threshold

The value of `MAX_COSINE_DISTANCE` in `memhub/retrieval/semantic.py` was measured,
not chosen by taste. This is the sweep it came from.

## Why a threshold is needed at all

Approximate nearest neighbour search returns the *k* closest vectors whether or
not anything is actually close. Asked about Kubernetes over a corpus that never
mentions it, the index returns the ten least-unrelated memories it has, with
every appearance of confidence.

The first hybrid implementation had no threshold. It beat full text on ranking
and destroyed everything else:

| | nDCG@10 | Recall@10 | Precision@10 | Empty for unanswerable |
|---|---|---|---|---|
| full text (baseline) | 0.803 | 0.817 | 0.691 | 0.667 |
| hybrid, no threshold | 0.881 | 0.876 | **0.113** | **0.000** |

Precision collapsed by a factor of six, and *every* unanswerable query started
returning ten results. For a system whose next milestone is spending a token
budget, that is not a cosmetic problem: an irrelevant result does not merely add
noise, it displaces something useful.

Reporting "hybrid improves nDCG from 0.803 to 0.881" and stopping there would
have been true and badly misleading.

## The sweep

Same 200-memory corpus, same 34 graded queries, `BAAI/bge-small-en-v1.5`.

| max cosine distance | nDCG@10 | Recall@10 | Precision@10 | Stale | Empty for unanswerable |
|---|---|---|---|---|---|
| 1.00 (no threshold) | 0.881 | 0.876 | 0.113 | 0.000 | 0.000 |
| 0.60 | 0.881 | 0.876 | 0.113 | 0.000 | 0.000 |
| 0.50 | 0.881 | 0.876 | 0.113 | 0.000 | 0.000 |
| 0.45 | 0.876 | 0.860 | 0.144 | 0.000 | 0.000 |
| 0.40 | 0.873 | 0.844 | 0.371 | 0.000 | 0.000 |
| **0.35** | **0.853** | **0.828** | **0.671** | **0.000** | **0.333** |
| 0.30 | 0.659 | 0.634 | 0.624 | 0.000 | 0.667 |
| 0.25 | 0.468 | 0.468 | 0.452 | 0.000 | 0.667 |
| 0.20 | 0.478 | 0.468 | 0.484 | 0.000 | 0.667 |

## Reading it

Nothing changes above 0.50, because with this model almost no pair of unrelated
sentences is that far apart — the threshold is not binding.

Between 0.45 and 0.35, precision climbs from 0.144 to 0.671 while nDCG gives up
0.023. That is the whole trade, and it is heavily in favour of tightening.

Below 0.30 it falls apart: the threshold starts excluding genuine matches, and by
0.25 the semantic retriever contributes almost nothing — the numbers converge on
the lexical-only baseline, which is what you would expect when one of two
retrievers has been switched off.

**0.35 is the knee.** It keeps most of the ranking gain and recovers nearly all
of the precision.

## What it costs

Hybrid at 0.35 against full text:

| Metric | Full text | Hybrid @ 0.35 | Delta |
|---|---|---|---|
| nDCG@10 | 0.803 | 0.853 | **+0.050** |
| Recall@10 | 0.817 | 0.828 | **+0.011** |
| Precision@10 | 0.691 | 0.671 | −0.020 |
| Stale inclusion | 0.000 | 0.000 | 0.000 |
| Empty for unanswerable | 0.667 | 0.333 | **−0.334** |

Hybrid is better at ranking and finding, slightly worse at excluding, and
meaningfully worse at recognising a question it cannot answer. That last row is
the honest cost, and it is worth stating rather than burying: one query that full
text correctly answered with silence now returns results.

Whether that trade is right depends on the caller. For search, where a human
reads the list, it clearly is. For the context builder, where results are
injected into a budget without anyone reading them first, the answer may differ —
and that is a decision to make with this table in hand rather than by assumption.

## Reproducing

```bash
pip install -e ".[local-embeddings]"
pytest tests/eval -m real_embeddings
```

The threshold is a parameter of `hybrid_search`, so a sweep is a loop over it
rather than an edit-and-rerun.
