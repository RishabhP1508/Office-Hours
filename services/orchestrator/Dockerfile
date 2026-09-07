FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# INSTALL_EXTRAS selects which of pyproject.toml's [project.optional-dependencies] extras get
# installed alongside the base runtime dependencies. Defaults to "dev,eval,gguf" -- "dev,eval" is
# the same set this Dockerfile has always installed, so `docker compose build` (docker-compose.yml
# passes no --build-arg at all) still gets pytest/ruff/black plus the full eval stack (ragas,
# langchain, textstat, datasets) baked into the image, e.g. for `docker compose exec orchestrator
# python -m eval.run`. "gguf" is new (Phase 8 round 8): a small, prebuilt-wheel-only dependency
# (llama-cpp-python) that lets a fresh local clone actually run tests/test_gguf_embedder.py's
# equivalence test, not just the production image.
#
# The image actually deployed to Fly (services that serve /query at request time) never runs
# pytest, ruff, black, or `python -m eval.run` -- those all run from a developer's machine or from
# GitHub Actions, against a container the deployed image never becomes. Yet ragas alone pulls in
# langchain, langchain-core, langchain-community, langchain-openai, datasets, and everything each of
# those pulls in transitively -- none of it imported anywhere on app.main's import graph (confirmed:
# `importlib.util.find_spec` returns None for langgraph, langchain, langchain_core, ragas, datasets,
# and textstat when this image is built with INSTALL_EXTRAS="gguf").
# Build the production image with `--build-arg INSTALL_EXTRAS=gguf` (just the embedding provider,
# nothing else): infra/deploy/fly.orchestrator.toml sets this via [build.args], so a plain
# `fly deploy` picks it up with no flag to remember.
ARG INSTALL_EXTRAS=dev,eval,gguf

COPY pyproject.toml ./
COPY app ./app
COPY tests ./tests
# Phase 8 round 8: the nomic-embed-text GGUF file, baked into the image at build time -- NEVER
# downloaded at container start (a boot-time network dependency on a third party is exactly what
# this in-process embedder exists to remove). This directory is tracked with only a .gitkeep in
# git; the real .gguf file is gitignored and must be placed here by whoever builds the production
# image (see the README's deploy guide for how to obtain it from Ollama's own local blob store).
# COPY always succeeds either way: local dev's image gets an empty directory (harmless --
# EMBED_PROVIDER defaults to "ollama", so GGUFEmbedder is never constructed), and a production
# build gets the real ~262MB file alongside it.
COPY models ./models

# Empty INSTALL_EXTRAS must install the bare package with NO extras at all (`pip install -e "."`),
# not `pip install -e ".[]"` -- empty-bracket extras syntax is not something to rely on here, so
# this branches in shell instead. --extra-index-url adds abetlen's own wheel index alongside PyPI
# (pip still resolves every other dependency from PyPI as usual); it is what lets llama-cpp-python
# (the "gguf" extra) install from a prebuilt manylinux2014_x86_64 wheel with no compiler in this
# image, and it is harmless to pass even when INSTALL_EXTRAS never requests that package at all.
RUN pip install --upgrade pip && \
    if [ -n "$INSTALL_EXTRAS" ]; then \
      pip install --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu \
        -e ".[$INSTALL_EXTRAS]"; \
    else \
      pip install -e "."; \
    fi

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
