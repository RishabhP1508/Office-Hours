"""app/telemetry.py::record_generation_tokens_metric tests (Phase 8 round 4).

setup_telemetry itself needs a real MeterProvider/exporter and is exercised implicitly by every
other test file that imports app.main (which calls it at import time); this file only tests
record_generation_tokens_metric's own logic in isolation, using a small fake Counter so no real
OTel SDK setup or OTLP endpoint is needed.
"""

from __future__ import annotations

import app.telemetry as telemetry


class _FakeCounter:
    def __init__(self):
        self.calls: list[tuple[float, dict]] = []

    def add(self, amount, attributes=None):
        self.calls.append((amount, attributes or {}))


def _reset_token_counter(counter):
    telemetry._token_counter = counter


def test_record_generation_tokens_metric_is_a_noop_before_setup():
    telemetry._token_counter = None
    # Must not raise even with real-looking usage.
    telemetry.record_generation_tokens_metric({"prompt_tokens": 10, "completion_tokens": 5})


def test_record_generation_tokens_metric_is_a_noop_for_none_usage():
    counter = _FakeCounter()
    _reset_token_counter(counter)
    telemetry.record_generation_tokens_metric(None)
    assert counter.calls == []


def test_record_generation_tokens_metric_records_both_fields_with_correct_labels():
    counter = _FakeCounter()
    _reset_token_counter(counter)
    telemetry.record_generation_tokens_metric({"prompt_tokens": 200, "completion_tokens": 30})
    assert (200, {"token_type": "prompt"}) in counter.calls
    assert (30, {"token_type": "completion"}) in counter.calls
    assert len(counter.calls) == 2


def test_record_generation_tokens_metric_never_fabricates_a_missing_field():
    """A provider that reports only one field (or an empty dict of usage) must add ONLY the field
    it actually reported -- never a fabricated 0 or estimate for the other."""
    counter = _FakeCounter()
    _reset_token_counter(counter)
    telemetry.record_generation_tokens_metric({"prompt_tokens": 50, "completion_tokens": None})
    assert counter.calls == [(50, {"token_type": "prompt"})]


def test_record_generation_tokens_metric_records_nothing_when_both_fields_are_none():
    counter = _FakeCounter()
    _reset_token_counter(counter)
    telemetry.record_generation_tokens_metric({"prompt_tokens": None, "completion_tokens": None})
    assert counter.calls == []
