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

# Two installs, deliberately. The old single install passed --extra-index-url, which does NOT mean
# "use this index only for the package that needs it": pip queries EVERY index for EVERY package
# name in the resolution and takes the highest version found anywhere. Measured, not assumed --
# under the old flag this build queried abetlen's index for 59 distinct package names (58 of them
# 404), and a handmade fastapi 99.0.0 served from a throwaway index beat PyPI's real 0.141.1. That
# put the whole production dependency set behind one third-party GitHub Pages account. See
# REPORT.md, "EXPOSURE: the production Dockerfile is open to dependency confusion".
#
# Step 1 runs ONLY when "gguf" is requested. It uses --index-url (not --extra-index-url), so
# abetlen's index is the only index in scope, and --no-deps, so nothing but the one pinned name is
# ever resolved against it. --disable-pip-version-check suppresses pip's own self-update query,
# which would otherwise ask that index about the name "pip" as well; it cannot install anything,
# but leaving it out means the index is asked about two names instead of one. Net: an allowlist of
# exactly one package.
#
# Step 2 installs the project and its extras with PyPI as the sole index. llama-cpp-python==0.3.35
# is already satisfied by then, so pip reports "Requirement already satisfied" and never reaches
# PyPI's sdist -- no compiler in this image, and none needed -- while STILL resolving that wheel's
# own dependencies (typing-extensions, numpy, diskcache, jinja2, MarkupSafe) from PyPI, so --no-deps
# costs nothing in the final package set. Both halves of that were measured: `pip check` reports no
# broken requirements, and `pip freeze` is byte-identical to a same-day build of the old flags.
# Blast radius: 59 package names -> 1.
#
# The "," padding on the case guard makes the match token-exact: "gguf" and "dev,eval,gguf" select
# step 1; "evalgguf" and "dev,gguffy" do not.
#
# Empty INSTALL_EXTRAS must install the bare package with NO extras at all (`pip install -e "."`),
# not `pip install -e ".[]"` -- empty-bracket extras syntax is not something to rely on here, so
# this branches in shell instead.
RUN pip install --upgrade pip && \
    case ",$INSTALL_EXTRAS," in \
      *,gguf,*) pip install --index-url https://abetlen.github.io/llama-cpp-python/whl/cpu \
                  --disable-pip-version-check --no-deps "llama-cpp-python==0.3.35" ;; \
    esac && \
    if [ -n "$INSTALL_EXTRAS" ]; then \
      pip install --index-url https://pypi.org/simple -e ".[$INSTALL_EXTRAS]"; \
    else \
      pip install --index-url https://pypi.org/simple -e "."; \
    fi

EXPOSE 8000

CMD ["python", "-m", "app.serve"]