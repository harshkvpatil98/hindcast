# syntax=docker/dockerfile:1.7
# The Cascade bench image (M11): runs `cascade db migrate`, the corpus restore,
# `cascade retrieval index` and `cascade retrieval bench` inside the VPC.
#
# Built for the task definition's constraints, not merely compatible with them:
#   * read-only root filesystem -> the CLI runs from the venv, never `uv run`
#     (which writes a lockfile); every write goes to /scratch (settings in the
#     task definition);
#   * no internet in the VPC -> the pinned embedding model is baked in here and
#     Hugging Face runs offline;
#   * pg_restore must be >= the pg_dump that made the dump (16), and Debian
#     bookworm ships 15 -> the client comes from the PostgreSQL project's repo.
#
# Build for the task's architecture from the repository root:
#   docker buildx build --platform linux/arm64 -f infra/docker/bench.Dockerfile -t cascade-bench:m11 .

# Pinned by digest, not tag: the tag moves, the digest names one image forever.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

ARG PG_MAJOR=16
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends "postgresql-client-${PG_MAJOR}" \
 && apt-get purge -y curl gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HF_HOME=/opt/hf

# Dependencies first, so a code change does not re-download torch.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
      --extra kernel --extra embed --extra aws

COPY cascade ./cascade
COPY configs ./configs
COPY migrations ./migrations
# Editable (uv's default) on purpose: cascade/config.py resolves configs/ and
# migrations/ relative to the package's parent, which must be /app.
RUN uv sync --frozen --no-dev --extra kernel --extra embed --extra aws

# Bake the pinned embedding model (spec §2.3) so the task never needs the hub.
RUN /app/.venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

ENV PATH=/app/.venv/bin:$PATH \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# Non-root. The task mounts /scratch writable; everything else is read-only.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin cascade
USER 10001

ENTRYPOINT []
CMD ["cascade", "doctor", "--offline"]
