# Calibrating the token estimator

`memory_context` promises never to exceed the budget it is given. This is the
measurement behind that promise, and the correction it forced.

## Why an estimate at all

The budget is spent in the *client's* model, and the server does not know which
model that is. Claude, GPT and Llama tokenise the same sentence differently, and
MCP carries no field that would tell us. So any count here is an approximation,
and the honest design question is which direction to be wrong in.

Those two errors are not comparable. Over-running corrupts the caller's context
window; under-filling wastes a little of it. So the estimator is deliberately
tuned to fail in the cheap direction, and the contract is stated as:

> **Never exceed the stated budget. May under-fill it by roughly 10%.**

Using `tiktoken` would look more precise and be less honest — it is OpenAI's
tokeniser, and using it to budget for Claude produces confident numbers that are
systematically wrong, which is worse than an approximation because nobody thinks
to check them.

## Method

Every hand-written memory in the evaluation corpus (33 samples), tokenised with a
real BPE tokeniser (`BAAI/bge-small-en-v1.5`, already present for embeddings) and
compared against `HeuristicEstimator`, content only, excluding the per-item
overhead so the two are comparable.

## The first value was wrong

`CHARS_PER_TOKEN` started at **3.6**, chosen as "safely below the ~4.0 usually
quoted for English prose".

| Divisor 3.6 | |
|---|---|
| Mean actual chars/token | 4.17 |
| **Minimum** actual chars/token | **3.25** |
| Mean estimate ÷ actual | 1.18 |
| **Worst estimate ÷ actual** | **0.938** |
| **Samples under-estimated** | **2 of 33** |

The reasoning was sound for prose and wrong for this corpus. Technical writing is
full of identifiers — `FOR UPDATE SKIP LOCKED`, `websearch_to_tsquery`,
`PostgreSQL` — and BPE splits those into many short tokens. The densest sample
came in at **3.25** characters per token, well under the 3.6 divisor.

A divisor above the densest real content is not conservative. It is optimistic
about exactly the material this system exists to store.

The 10% safety margin would have absorbed those two cases at the aggregate level,
which is precisely why this was worth measuring rather than reasoning about: the
guarantee would have held by accident, through a coupling nobody had written
down, and would have broken the first time someone tuned the margin.

## After correction

`CHARS_PER_TOKEN = 3.2`, below the densest observed sample.

| Divisor 3.2 | |
|---|---|
| Mean actual chars/token | 4.17 |
| Minimum actual chars/token | 3.25 |
| Mean estimate ÷ actual | 1.323 |
| **Worst estimate ÷ actual** | **1.048** |
| **Samples under-estimated** | **0 of 33** |

Every memory is now estimated at or above its true token count, with the worst
case only 4.8% over.

## What it costs

Over-estimating by ~32% on average means a brief fills roughly two thirds of the
requested budget before the safety margin is even applied. That is the price of
the guarantee, and it is the right side to be on: a caller who finds the brief
thin can raise the budget, and the response tells them what was dropped and why.
A caller whose context window was overrun has no such option.

Two ways to narrow the gap later, in order of preference:

1. **Supply the real tokeniser.** `TokenEstimator` is a protocol; a deployment
   that knows its client's model can pass the actual counter and get tight
   budgets with no other change. That is why it is a protocol.
2. **Re-calibrate per corpus.** The divisor is a constant chosen from one
   corpus. A project whose memories are prose rather than identifiers would
   safely support a higher one.

Neither is worth doing before someone has a budget too tight to work with.

## Reproducing

The comparison needs the optional embedding extra, for its tokeniser:

```bash
pip install -e ".[local-embeddings]"
```

Then tokenise `eval/dataset/memories.yaml` with `TextEmbedding(...).model.tokenizer`
and compare against `HeuristicEstimator().estimate(text) - PER_ITEM_OVERHEAD`.
