FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

# The data directories must exist and belong to the app user before a fresh named volume is
# mounted on them, otherwise the volume is created root-owned and the services cannot write.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app \
    && mkdir -p /data/archive /data/timetables \
    && chown app:app /data/archive /data/timetables
USER app

CMD ["/app/.venv/bin/python", "-m", "ov_reliability.collector"]
