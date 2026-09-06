"""Guardrail stages the pipeline runs around retrieval and generation.

classifier.py    advice-vs-information (two layers: a deterministic rule, then a model escalation)
clarifier.py     one clarifying question for a query too vague to retrieve against
citations.py     programmatic verification that every cited index resolves to a retrieved chunk

freshness.py (volatile-topic flagging, Phase 5) does not exist yet.
"""
