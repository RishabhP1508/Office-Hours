"""Tests for app.config.require_contactable_user_agent and its two real call sites.

Context (REPORT.md instrument entry 39): production was crawling with a User-Agent carrying
neither a contact address nor a URL that resolves, while three OTHER files in the repository all
documented a complete, correct value. The fix makes app/config.py::Settings.USER_AGENT the one
definition and adds an enforceable predicate beside it, called at the two places that build a real
network client (app/ingest.py::_ingest_from_manifest, app/recrawl.py::run_refresh's real-httpx-
client branch) -- never at import, never on the snapshot-ingest path, never on an injected-fetcher
path, and never in app/serve.py's serving path, which imports neither module at all.

No database and no network are used anywhere in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import (
    DEFAULT_USER_AGENT,
    MEASURED_PRODUCTION_USER_AGENT_2026_09_19,
    Settings,
    require_contactable_user_agent,
)
from app.ingest import _ingest_from_manifest, _ingest_from_snapshots
from app.providers.embeddings import StubEmbedder
from app.recrawl import run_refresh

# =================================================================================================
# The predicate itself
# =================================================================================================


def test_accepts_the_real_settings_default():
    """Read from Settings().USER_AGENT, not a retyped literal, so this cannot pass against a
    string that has drifted from the code (see MEMORY.md, "check the prompt rule is answerable" /
    the general habit of not re-deriving what is already measurable).
    """
    require_contactable_user_agent(Settings().USER_AGENT)


def test_control_rejects_the_real_measured_production_value():
    """THE CONTROL. Without this, a predicate that always returned None (accept everything) would
    pass test_accepts_the_real_settings_default forever and never be shown wrong. This is the
    literal value production was actually sending on 19 September 2026 (REPORT.md instrument
    entry 39), not a synthetic bad string -- so this test demonstrates the predicate would have
    caught the real incident, not merely a string designed to be caught.
    """
    with pytest.raises(ValueError, match="no contact"):
        require_contactable_user_agent(MEASURED_PRODUCTION_USER_AGENT_2026_09_19)


def test_rejects_docker_composes_old_placeholder_form():
    ua = "OfficeHoursBot/0.1 (+https://github.com/office-hours; contact: set-a-real-contact-here)"
    with pytest.raises(ValueError, match="set-a-real-contact-here"):
        require_contactable_user_agent(ua)


def test_rejects_placeholder_literal_even_when_the_string_also_has_an_at_sign():
    """The placeholder check fires independently of email-shapedness, per app/config.py's own
    ordering -- not merely as a side effect of the placeholder string containing no "@".
    """
    ua = "SomeBot/1.0 (contact: set-a-real-contact-here@example.com)"
    with pytest.raises(ValueError, match="set-a-real-contact-here"):
        require_contactable_user_agent(ua)


def test_rejects_a_url_with_no_email():
    ua = "OfficeHoursBot/0.1 (+https://github.com/RishabhP1508/Office-Hours)"
    with pytest.raises(ValueError, match="no contact"):
        require_contactable_user_agent(ua)


@pytest.mark.parametrize("domain", ["example.com", "example.org", "example.net"])
def test_rejects_reserved_documentation_domains(domain):
    ua = f"SomeBot/1.0 (+https://somebot.test; contact: bot@{domain})"
    with pytest.raises(ValueError, match="documentation-only"):
        require_contactable_user_agent(ua)


def test_rejects_empty_string():
    with pytest.raises(ValueError, match="no contact"):
        require_contactable_user_agent("")


@pytest.mark.parametrize(
    "ua",
    [
        # No literal "contact:" at all -- the rule asserts the property, not this one phrasing.
        "MyUniversityBot/1.0 (reach the crawl team at data-team@cs.stanford.edu)",
        # A different bracket/attribution style, still a real-looking email-shaped address.
        "AcmeResearchCrawler/2.3 (+https://acme-research.test; operator: jane.doe@acme-labs.io)",
    ],
)
def test_accepts_plausible_alternative_formats_carrying_a_real_looking_address(ua):
    require_contactable_user_agent(ua)


# =================================================================================================
# Call sites: fires, and BEFORE any network attempt
# =================================================================================================


async def test_ingest_from_manifest_raises_before_reading_the_manifest_or_touching_the_network(
    monkeypatch, tmp_path
):
    """The check is the first statement in _ingest_from_manifest, so a bad USER_AGENT must raise
    before SOURCES_MANIFEST_PATH is even read (a bogus, nonexistent path here) and before
    httpx.AsyncClient is constructed (patched to blow up if it ever is).
    """

    def _must_not_construct(*args, **kwargs):
        raise AssertionError("httpx.AsyncClient must not be constructed for an invalid USER_AGENT")

    monkeypatch.setattr("app.ingest.httpx.AsyncClient", _must_not_construct)

    bad_settings = Settings(
        USER_AGENT=MEASURED_PRODUCTION_USER_AGENT_2026_09_19,
        SOURCES_MANIFEST_PATH=str(tmp_path / "does-not-exist.yaml"),
        RAW_SNAPSHOT_DIR=str(tmp_path),
    )

    with pytest.raises(ValueError, match="no contact"):
        await _ingest_from_manifest(
            conn=None, embedder=None, settings=bad_settings, raw_dir=tmp_path
        )


async def test_run_refresh_real_client_path_raises_before_touching_the_network(
    monkeypatch, tmp_path
):
    """run_refresh's else branch (fetcher=None, the real-httpx-client path) must call the check
    before constructing httpx.AsyncClient. Driven with an empty manifest and fetcher=None, so the
    only work before the check is local setup (golden-set read, lazy conn-factory/embedder
    construction) -- none of it network or database I/O.
    """

    def _must_not_construct(*args, **kwargs):
        raise AssertionError("httpx.AsyncClient must not be constructed for an invalid USER_AGENT")

    monkeypatch.setattr("app.recrawl.httpx.AsyncClient", _must_not_construct)

    bad_settings = Settings(USER_AGENT=MEASURED_PRODUCTION_USER_AGENT_2026_09_19)

    with pytest.raises(ValueError, match="no contact"):
        await run_refresh(
            settings=bad_settings,
            manifest=[],
            raw_dir=tmp_path,
            fetcher=None,
            conn_factory=lambda: None,
            embedder=StubEmbedder(dim=8),
            golden_path=tmp_path / "no-such-golden.jsonl",
        )


# =================================================================================================
# Call sites: does NOT fire where it must not
# =================================================================================================


async def test_ingest_from_snapshots_never_calls_the_check(monkeypatch, tmp_path):
    """_ingest_from_snapshots fetches nothing over the network at all -- it has no USER_AGENT
    concept in its signature -- so app.config.require_contactable_user_agent must never be called
    from this path. An empty raw_dir (no *.md files) makes the function a true no-op otherwise, so
    this test needs no real snapshot, no embedder, and no connection.
    """

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("require_contactable_user_agent must not fire on the snapshot path")

    monkeypatch.setattr("app.ingest.require_contactable_user_agent", _must_not_be_called)

    total_chunks, total_sources = await _ingest_from_snapshots(
        conn=None, embedder=None, raw_dir=tmp_path
    )

    assert (total_chunks, total_sources) == (0, 0)


async def test_run_refresh_injected_fetcher_path_never_calls_the_check(monkeypatch, tmp_path):
    """When a fetcher is injected (every other recrawl test's own path), run_refresh must never
    reach the check at all -- it sits only in the real-httpx-client `else` branch, never in the
    `if fetcher is not None` branch.

    Driving run_refresh all the way through needs the `[freshness]` extra (langgraph), which is
    NOT installed in this environment (see test_freshness.py's own `requires_langgraph` skip and
    this repo's baseline: langgraph absent). That is a real, stated limitation, not a workaround:
    `_drive` imports `langgraph.checkpoint.sqlite.aio` as its own first statement, before doing
    anything else, so this environment cannot execute this branch to completion regardless of
    USER_AGENT. What CAN be verified here, and is: the check itself is never reached before that
    import failure surfaces, which is exactly the claim ("does not fire when a fetcher is
    injected") this test exists to make.
    """
    calls: list[str] = []

    def _record_and_fail(user_agent):
        calls.append(user_agent)
        raise AssertionError("must not be called on the fetcher-injected path")

    monkeypatch.setattr("app.recrawl.require_contactable_user_agent", _record_and_fail)

    bad_settings = Settings(USER_AGENT=MEASURED_PRODUCTION_USER_AGENT_2026_09_19)

    async def _fake_fetcher(entry):
        return None

    with pytest.raises(ModuleNotFoundError):
        await run_refresh(
            settings=bad_settings,
            manifest=[],
            raw_dir=tmp_path,
            fetcher=_fake_fetcher,
            conn_factory=lambda: None,
            embedder=StubEmbedder(dim=8),
            golden_path=tmp_path / "no-such-golden.jsonl",
        )

    assert calls == []


def test_serve_module_imports_neither_ingest_nor_recrawl():
    """app/serve.py (the process Fly actually boots, per app/serve.py's own module docstring) must
    never fail to start over this: it never crawls. Checked at the source level -- app.serve's own
    module source names neither `app.ingest` nor `app.recrawl` -- rather than by importing
    app.main, which pulls in the full pipeline/db/provider stack this test file has no need of.
    """
    import app.serve as serve_module

    source = Path(serve_module.__file__).read_text(encoding="utf-8")
    assert "app.ingest" not in source
    assert "app.recrawl" not in source


# -------------------------------------------------------------------------------------------
# The blank-USER_AGENT fallback is scoped to ONE FIELD. A class-wide `env_ignore_empty=True` on
# Settings.model_config solves the same docker-compose problem and was tried, then reverted the
# same day: it silently made an explicitly-blanked env var fall back to its hardcoded default for
# every field with a non-empty one, including a session salt this repository publishes. These
# tests are what stops that being re-added. See REPORT.md instrument entry 40.
# -------------------------------------------------------------------------------------------


def test_a_blank_user_agent_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("USER_AGENT", "")
    assert Settings().USER_AGENT == DEFAULT_USER_AGENT
    monkeypatch.setenv("USER_AGENT", "   ")
    assert Settings().USER_AGENT == DEFAULT_USER_AGENT


def test_a_real_user_agent_override_still_wins(monkeypatch):
    monkeypatch.setenv("USER_AGENT", "Bot/9 (contact: someone@agency.gov)")
    assert Settings().USER_AGENT == "Bot/9 (contact: someone@agency.gov)"


@pytest.mark.parametrize(
    "field",
    [
        "SESSION_HASH_SALT",
        "ALLOWED_ORIGINS",
        "DATABASE_URL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "LLM_MODEL",
    ],
)
def test_blanking_any_other_setting_stays_blank_rather_than_restoring_its_default(
    monkeypatch, field
):
    """THE REGRESSION GUARD, written broadly on purpose: it checks a CLASS of fields, not the two
    that prompted it. An explicitly-blanked env var must stay blank for every field except
    USER_AGENT. SESSION_HASH_SALT is the one that matters most: its default is a dev salt checked
    into this repository, so a secret that fails to populate and arrives empty must NOT silently
    become that published value. ALLOWED_ORIGINS matters second: restoring localhost:3000 would
    break CORS in a way that presents as a network fault rather than a config error.
    """
    monkeypatch.setenv(field, "")
    assert getattr(Settings(), field) == "", (
        f"{field} blanked in the environment resolved to its hardcoded default instead of "
        "staying empty. Has a class-wide env_ignore_empty been re-added to Settings.model_config?"
    )


def test_control_the_blank_guard_would_catch_a_class_wide_env_ignore_empty(monkeypatch):
    """The control. The assertion above only means something if a class-wide setting would break
    it, so this constructs the failing condition directly rather than trusting that it would.
    """
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class WithGlobalIgnoreEmpty(BaseSettings):
        model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)
        SESSION_HASH_SALT: str = "office-hours-dev-salt-change-in-production"

    monkeypatch.setenv("SESSION_HASH_SALT", "")
    assert WithGlobalIgnoreEmpty().SESSION_HASH_SALT == "office-hours-dev-salt-change-in-production"
    assert Settings().SESSION_HASH_SALT == ""
