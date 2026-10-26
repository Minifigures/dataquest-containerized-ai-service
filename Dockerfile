# Multi-stage build. The builder compiles wheels; the runtime image gets only
# the installed packages and the source, so build tools never ship to
# production and the image stays small.

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim AS runtime

# A non-root user. If the process is ever compromised, it should not own the
# filesystem it is standing on.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser . .

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('PORT','8000'),timeout=4)" || exit 1

# Shell form, not exec form, and that is the whole point.
#
# Exec form does not start a shell, so nothing expands $PORT and uvicorn
# receives the literal four characters:
#
#   CMD ["uvicorn", "main:app", "--port", "$PORT"]
#   Error: Invalid value for '--port': '$PORT' is not a valid integer.
#
# The container then exits immediately, the platform restarts it, and the
# deploy fails as a crash loop whose real cause is one unexpanded variable.
#
# Shell form runs through /bin/sh, so ${PORT:-8000} expands: the platform's
# assigned port in production, 8000 when nothing sets it locally.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
