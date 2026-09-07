# 0013: Production embeds in-process from the same GGUF file Ollama uses, never a hosted API

## Context

ARCHITECTURE.md's "Deliberately not decided yet" left the production embedding provider open until
the eval gate had to run against production. Phase 8 filled in the generator (Ollama Cloud primary,
NVIDIA fallback) but not the embedder, because neither of those two accounts serves
`nomic-embed-text`: Ollama Cloud's catalog is generation models only, and NVIDIA's NIM catalog has
no nomic-embed-text endpoint either. That mattered more than it first looked. The 221 vectors
already stored in `documents` were built by real local `nomic-embed-text` (a 262MiB F16 GGUF, arch
`nomic-bert`, 137M params, 768-dim, `n_ctx_train` 2048), and `NO_ANSWER_MAX_DISTANCE` (the no-answer
guardrail's threshold) is 0.50, derived from seven off-topic control queries sitting in a tight
cluster from 0.5251 up, with one real off-topic control ("car insurance") already at 0.4370, which
leaves the threshold 0.063 of slack. Moving the corpus to a different embedding model
means moving to a different vector space, which means re-deriving that threshold from scratch,
against a safety-critical guardrail, with no guarantee a new model's off-topic/on-topic separation
looks anything like `nomic-embed-text`'s. Nomic Atlas, a hosted embedding API from the company that
publishes the model, was considered as a way to avoid that re-derivation and rejected without being
tried. See "Alternatives" for why.

The alternative that avoids re-deriving the threshold at all is to keep using the identical model.
Production has no GPU, so "keep using it" means running the same GGUF file's weights on CPU, and
`llama.cpp` -- the engine Ollama itself is built on -- runs GGUF models on CPU already. The question
this ADR answers is whether running that engine in-process, without Ollama wrapped around it,
actually reproduces Ollama's own vectors closely enough to keep querying against the existing 221
stored ones, or whether it just looks like it should.

## Decision

Load `nomic-embed-text`'s own GGUF file in-process via `llama-cpp-python`, selected by
`EMBED_PROVIDER=gguf` (`app/providers/embeddings.py::GGUFEmbedder`). No hosted API, no new vendor
account, no third Fly machine: the same weights, loaded inside the same process that already serves
`/query`.

### The batch parameter is part of the contract, not an implementation detail

This is the finding that makes the decision safe rather than merely plausible, and it is the reason
this ADR exists rather than a one-line config change. The first attempt at this -- same weights,
same engine, llama.cpp's own defaults -- did NOT reproduce Ollama's vectors for every chunk. Measured
directly against 25 real stored corpus vectors read from Postgres:

| config | vectors below cosine 0.9999 | minimum cosine |
| --- | --- | --- |
| `n_batch=512` (llama.cpp's own default) | 5 of 25 | 0.974 |
| `n_batch=n_ubatch=n_ctx` | 0 of 25 | 0.99999412 |

Every one of the 5 divergent vectors under the default was a chunk of 583 tokens or more; every
chunk of 485 tokens or fewer matched regardless of batch size. `n_batch` bounds how many tokens
llama.cpp will process for one sequence, and `llama-cpp-python`'s `create_embedding` defaults to
`truncate=True`, so a longer input is silently cut down to the first `n_batch` tokens and only that
prefix is embedded. This was measured rather than reasoned about, because the obvious guess (that
the sequence is split into batches and recombined) is wrong: for chunk 456, 1588 tokens, the
`n_batch=512` vector matches the full text at cosine 0.974 but matches that same text truncated to
its first 512 tokens at 0.999. The vector describes the opening of the chunk and nothing after it.

Nothing about that failure is visible from the outside. The call returns a vector of the right shape
with a plausible magnitude, and it only shows up as retrieval quietly getting worse on the corpus's
longer chunks, which is indistinguishable from ordinary model variance unless it is measured against
the specific stored vectors it has to match. `GGUFEmbedder` closes this twice over: it passes
`truncate=False`, which turns the silent cut into a raised error, and it checks the token count
itself first so the error names the real count and the limit.

`GGUFEmbedder`'s constructor takes a single `n_ctx` argument and derives `n_batch`/`n_ubatch`
from it, rather than exposing three settings a future `.env` edit could set inconsistently, so this
cannot regress by a later change that only knows to set `n_ctx`.

### No task-instruction prefix, ever

`nomic-embed-text`'s model card documents optional `search_query:`/`search_document:` prefixes, and
adding one looked like the more "correct" way to call this model. Ollama does not: its own Modelfile
for this model is `TEMPLATE {{ .Prompt }}`, no prefix at all. Measured against 3 probe strings
against real local Ollama output: raw text (no prefix) matches at cosine 0.99999980 / 0.99999715 /
0.99999980. Adding `search_query: ` drops that to 0.970 / 0.982 / 0.976, and `search_document: ` to
0.909 / 0.969 / 0.859 -- both still "high" numbers in isolation, and both a completely different
embedding space from what the corpus was actually stored in. `GGUFEmbedder.embed()` sends every input
completely unchanged, with a comment carrying these exact numbers, so a future "improvement" that
adds a prefix to follow the model card has to consciously delete that comment first.

### Why this is safe against the golden set, not just three probe strings

Three probe strings and 25 corpus vectors say the mechanism works; the equivalence test
(`services/orchestrator/tests/test_gguf_embedder.py`) is what keeps it working. It reads real stored
vectors straight from `documents` -- never a fixture, never a hand-constructed vector -- spanning
both short and long chunks (the divergence appears only past `n_batch`, which the finding above
brackets exactly: 485 tokens matched, 583 did not, so a short-only sample would pass with the bug
still present), and requires cosine >= 0.9999
on every one. It is proven to have teeth, not just to pass: forcing `n_batch=512` back on
(reproducing the original bug) fails it immediately, on the same chunks, with the same low cosine
values recorded above.

## Tradeoff

The orchestrator now does CPU inference work it never did before, in the same process and on the
same machine that serves `/query`. Measured serving config (`n_ctx=n_batch=n_ubatch=2048`,
1 thread): 351MB loaded / 358MB peak, 16.2ms median query embed -- small next to the 20-140s a
generation call already takes, but it is real memory and real CPU time the orchestrator did not
previously spend, and it is spent on Fly's shared-cpu-1x, not a machine with headroom to spare.
The VM moved from 512MB to 1024MB to give that memory room (see
`infra/deploy/fly.orchestrator.toml`'s own `[[vm]]` comment for the exact cost: $3.19/mo to
$5.70/mo for this machine, $5.13/mo to $7.64/mo for the whole stack).

The GGUF file itself (262MB) is baked into the production image rather than fetched at boot, which
is the right tradeoff for this project (no boot-time dependency on a third party, see Decision above)
but it does mean the production image grows from 324MB to 1.06GB, measured, with the GGUF layer
accounting for 274MB of that and llama-cpp-python's dependencies, numpy above all, most of the rest, and a change to the embedding
model would require a new image build and deploy, not just an environment variable flip the way
switching between Ollama Cloud and a hosted embedding API would have been.

`EMBED_GGUF_N_CTX=2048` covers this corpus's longest chunk (1946 tokens) with headroom, but it is a
number derived from THIS corpus, not a universal one. Ingesting a future source with a much longer
undivided section would need this raised (the measured ingest-sized config, 8192, peaks at 742MB) --
`GGUFEmbedder` raises rather than silently truncating an overlong input specifically so that this
shows up as a loud, immediate failure instead of a quietly wrong vector.

## Alternatives considered

- **A hosted embedding API on a different model.** Rejected: every candidate (NVIDIA NIM, a generic
  OpenAI-compatible embeddings endpoint) embeds in a different vector space than the 221 already
  stored vectors, which means re-deriving `NO_ANSWER_MAX_DISTANCE` from scratch against a
  safety-critical guardrail that already has almost no margin in its current derivation (0.4370 vs.
  0.50). That re-derivation is not obviously safe to do at all, let alone as a Phase 8 config change.

- **Nomic Atlas, a hosted API from the company that publishes the model.** The natural middle
  ground, since it is the same model family, hosted. **It was rejected without ever being tried, and
  no measurement of it exists.** The plan had been to embed three identical strings through local
  Ollama and through Atlas and compare, adopting Atlas only if every pair came back at cosine 0.9999
  or above, and falling back to a dedicated machine otherwise. That test was never run. Three
  practical reasons stopped it before it started: Atlas now requires a company work email, so no
  account could be obtained; its pricing starts at $20 per month with no free tier, against a
  project whose whole hosting budget is a few dollars; and Nomic has repositioned its models toward
  architecture, engineering and construction, so the model served under a familiar name may no
  longer be the version Ollama packaged. Any one of those is enough on its own. The decision rule
  above was never exercised, and this entry should not be read as evidence that Atlas would have
  failed it.

- **A third Fly machine, GPU-less CPU inference behind its own service (e.g., a small Ollama running
  on Fly, or a bare llama.cpp server).** This is the same underlying mechanism as the chosen option,
  wrapped in an extra network hop. Rejected on cost ($3.19/mo more at the smallest usable size,
  $10.83/mo total against $7.64/mo) and on complexity: a second service to deploy, monitor, and keep
  in sync with the orchestrator's own corpus version, for a workload (16ms per query) that does not
  need isolating from the process that already waits 20-140s per generation call.
