"""Guards ARCHITECTURE.md's "the eval judge is a hosted model from a different family than the
generator" against Phase 8's failover chain silently breaking it: the fallback generator (NVIDIA,
`openai/gpt-oss-20b`) sits on the same vendor as the judge
(`nvidia/nemotron-3.5-lightning-30b-a3b`), so a self-preference bias could creep back in the
moment failover actually fires, without any other test noticing.

Every check here drives app.providers.llm.resolve_generator_models (the SAME dispatch function
get_llm() itself uses to decide what to construct) and app.providers.llm.resolve_model_family --
never a hardcoded list of "known bad" model ids -- so a future change to LLM_MODEL,
LLM_FALLBACK_MODEL, or JUDGE_MODEL is caught here automatically instead of silently going
unchecked.

Two DIFFERENT things are checked below, and they are not interchangeable:

1. The DOCUMENTED production configuration (_DOCUMENTED_PRODUCTION_SETTINGS, a literal this test
   file writes itself, mirroring .env.example / infra/deploy/fly.orchestrator.toml). This is a
   fixture the test constructs, so a real misconfiguration made anywhere else (a real .env, real
   `fly secrets`) can never touch it -- it only proves the DOCUMENTED values are consistent with
   each other, nothing about what is actually deployed or actually running.
2. The REAL, resolved configuration (bare `Settings()`, reading whatever the ACTUAL environment
   this test process runs in provides -- OS env vars, and any real `.env` file). This is the one
   that would actually catch someone setting LLM_FALLBACK_MODEL=nvidia/nemotron-3-nano in a real
   `.env` or in `fly secrets`: if that value were present in this process's environment when
   pytest runs, resolve_generator_models(Settings()) would include it and the assertion below would
   fail for real, not against a fixture that already matches itself.

The actual ENFORCEMENT point for (2) is eval/run.py's main(), which calls
app.providers.llm.assert_no_generator_judge_family_collision on get_settings() (the real,
resolved settings) as the very first thing it does, before a single judge call or generation call --
see that function's own docstring and eval/run.py. The tests below exercise that same function
directly, both on a config that must pass and on one deliberately built to collide, so the "make
the wrong state impossible" mechanism itself is under test, not just its intended inputs.
"""

import pytest

from app.config import Settings
from app.providers.llm import (
    ModelFamilyCollisionError,
    assert_no_generator_judge_family_collision,
    resolve_generator_models,
    resolve_model_family,
)

# Mirrors the Phase 8 production configuration DOCUMENTED in .env.example and
# infra/deploy/fly.orchestrator.toml: LLM_PROVIDER stays "ollama" (Ollama Cloud speaks the same
# /api/chat shape as local Ollama -- see app/providers/llm.py::OllamaLLM's docstring) with
# OLLAMA_CLOUD=true and the model/base URL swapped to the cloud endpoint; LLM_FALLBACK_* wires up
# NVIDIA as the failover. Base URLs and API keys never reach the network in this test (resolving
# the chain and classifying its models makes no HTTP call at all) and are filled with placeholders
# only so Settings() construction does not depend on real secrets being present in the environment
# this test runs in.
#
# THIS IS A FIXTURE THE TEST FILE WRITES ITSELF, NOT THE LIVE ENVIRONMENT. A real misconfiguration
# made in an actual `.env` or in `fly secrets` would never touch this literal, so the tests that use
# it below (test_documented_production_chain_...) prove only that the DOCUMENTATION is internally
# consistent -- see the module docstring for where the REAL enforcement lives instead.
_DOCUMENTED_PRODUCTION_SETTINGS = Settings(
    LLM_PROVIDER="ollama",
    LLM_MODEL="gpt-oss:120b",
    OLLAMA_BASE_URL="https://ollama.com",
    OLLAMA_API_KEY="placeholder",
    OLLAMA_CLOUD=True,
    LLM_FALLBACK_PROVIDER="nvidia",
    LLM_FALLBACK_MODEL="openai/gpt-oss-20b",
    LLM_FALLBACK_BASE_URL="https://integrate.api.nvidia.com/v1/chat/completions",
    LLM_FALLBACK_API_KEY="placeholder",
)


def test_documented_production_chain_fixture_actually_has_a_fallback():
    """Sanity check on the fixture above, not the guard itself. If this configuration ever stopped
    resolving to two generators, the guard test below would quietly stop covering the fallback --
    the DoD requires checking "primary AND fallback, not just the primary" -- so this makes that
    precondition an explicit, separately-failing assertion instead of a silent gap.
    """
    chain = resolve_generator_models(_DOCUMENTED_PRODUCTION_SETTINGS)
    assert len(chain) == 2
    assert chain == ["gpt-oss:120b", "openai/gpt-oss-20b"]


def test_no_generator_in_the_documented_production_config_shares_the_judge_family():
    """Checks the DOCUMENTED config against itself (see module docstring, case 1) -- this proves
    .env.example / fly.orchestrator.toml's own values are internally consistent, nothing about what
    is actually deployed. See test_the_real_resolved_environment_is_checked_by_the_same_guard below
    for the case that reads the actual environment instead.
    """
    judge_family = resolve_model_family(_DOCUMENTED_PRODUCTION_SETTINGS.JUDGE_MODEL)
    for model_id in resolve_generator_models(_DOCUMENTED_PRODUCTION_SETTINGS):
        generator_family = resolve_model_family(model_id)
        assert generator_family != judge_family, (
            f"Generator {model_id!r} resolves to family {generator_family!r}, the same family as "
            f"judge {_DOCUMENTED_PRODUCTION_SETTINGS.JUDGE_MODEL!r} ({judge_family!r}). "
            "ARCHITECTURE.md requires the eval judge to be a different family than the generator, "
            "to avoid self-preference bias; this must hold for EVERY generator in the failover "
            "chain, not just the one serving the happy path."
        )
    # The same check via the actual guard function eval/run.py calls -- must not raise.
    assert_no_generator_judge_family_collision(_DOCUMENTED_PRODUCTION_SETTINGS)


def test_the_real_resolved_environment_is_checked_by_the_same_guard():
    """Case 2 from the module docstring: bare `Settings()`, reading whatever this test process's
    ACTUAL environment provides (OS env vars, and any real `.env` file) -- not a fixture that
    matches itself. If LLM_FALLBACK_MODEL/LLM_FALLBACK_PROVIDER were actually set to a same-family
    pair in the real environment this test runs in, this assertion would fail for real; it is not
    assumed to pass, it is checked directly, exactly as eval/run.py's main() checks it before a
    single judge call or generation call.

    `Settings()` with zero overrides also happens to be the local dev default (no fallback
    configured) -- so this test doubles as the "local dev default never collides" check, and a
    future change to either LLM_MODEL's or JUDGE_MODEL's default is caught here too, not just the
    documented production configuration above.
    """
    settings = Settings()
    assert_no_generator_judge_family_collision(settings)


def test_guard_raises_on_a_real_family_collision():
    """The actual teeth: assert_no_generator_judge_family_collision must RAISE, not merely report,
    on a genuinely colliding configuration -- an NVIDIA fallback generator resolving to the same
    "nemotron" family as the default JUDGE_MODEL. This is what eval/run.py's main() relies on to
    refuse to run before a single judge or generation call; a guard that only warns would still let
    the corrupting run happen.
    """
    colliding_settings = Settings(
        LLM_PROVIDER="ollama",
        LLM_MODEL="gpt-oss:120b",
        LLM_FALLBACK_PROVIDER="nvidia",
        LLM_FALLBACK_MODEL="nvidia/nemotron-3-nano",
        LLM_FALLBACK_BASE_URL="https://integrate.api.nvidia.com/v1/chat/completions",
        # JUDGE_MODEL left at its code default: nvidia/nemotron-3.5-lightning-30b-a3b (nemotron).
    )
    with pytest.raises(ModelFamilyCollisionError) as exc_info:
        assert_no_generator_judge_family_collision(colliding_settings)

    message = str(exc_info.value)
    # The DoD requires naming which models and which families collided -- assert the real values
    # appear in the message, not just that *some* error was raised.
    assert "nvidia/nemotron-3-nano" in message
    assert "nemotron" in message
    assert colliding_settings.JUDGE_MODEL in message


def test_guard_raises_rather_than_silently_passing_on_an_unclassifiable_generator():
    """An unrecognized model id must be a loud failure, never a quiet "different family" that lets
    the guard pass for the wrong reason -- see resolve_model_family's own docstring in
    app/providers/llm.py.
    """
    settings = Settings(
        LLM_PROVIDER="ollama",
        LLM_FALLBACK_PROVIDER="nvidia",
        LLM_FALLBACK_MODEL="some-vendor/brand-new-model-never-classified-before",
        LLM_FALLBACK_BASE_URL="https://example.invalid/v1/chat/completions",
    )
    with pytest.raises(ValueError):
        assert_no_generator_judge_family_collision(settings)


def test_guard_raises_on_an_unclassifiable_judge_model():
    """Symmetric with the generator-side check above: an unrecognized JUDGE_MODEL must also fail
    loudly rather than silently comparing against a placeholder family.
    """
    settings = Settings(JUDGE_MODEL="another-vendor/another-brand-new-model")
    with pytest.raises(ValueError):
        assert_no_generator_judge_family_collision(settings)


def test_classifier_model_is_excluded_from_the_family_guard():
    """Phase 8 round 3, cheap-model routing (app/guardrails/classifier.py, Settings.
    CLASSIFIER_LLM_PROVIDER/MODEL): the DELIBERATE decision, documented in
    Settings.CLASSIFIER_LLM_PROVIDER's own comment in app/config.py and in
    resolve_generator_models's docstring, is that the classifier model is NEVER part of the chain
    this guard checks -- its raw output never reaches the judge (only the generator's answer text
    does; see app/pipeline.py), so a family collision there cannot create the self-preference bias
    this guard exists to prevent.

    This test builds a configuration that WOULD collide if the classifier model were included:
    CLASSIFIER_LLM_MODEL is set to an nvidia/nemotron-* model, the SAME family as the default
    JUDGE_MODEL ("nvidia/nemotron-3.5-lightning-30b-a3b"). If a future edit ever widened the guard
    to include the classifier, this is exactly the configuration that would start raising --- so
    this test would have to be consciously changed to let that happen, not silently stop covering
    it.
    """
    settings = Settings(
        LLM_PROVIDER="ollama",
        LLM_MODEL="gpt-oss:120b",
        CLASSIFIER_LLM_PROVIDER="ollama",
        CLASSIFIER_LLM_MODEL="nvidia/nemotron-3-nano",
        # JUDGE_MODEL left at its code default: nvidia/nemotron-3.5-lightning-30b-a3b (nemotron) --
        # the SAME family CLASSIFIER_LLM_MODEL above resolves to.
    )
    assert resolve_model_family(settings.CLASSIFIER_LLM_MODEL) == resolve_model_family(
        settings.JUDGE_MODEL
    ), "test setup error: CLASSIFIER_LLM_MODEL must share the judge's family for this to be real"

    # The teeth: resolve_generator_models must NOT include the classifier model at all ...
    assert settings.CLASSIFIER_LLM_MODEL not in resolve_generator_models(settings)
    # ... and the guard itself must NOT raise, even though the classifier model, if it were
    # checked, would collide.
    assert_no_generator_judge_family_collision(settings)
