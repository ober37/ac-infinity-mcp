# Pinned by digest (P3-C2-F008). Dependabot's Docker ecosystem updates the
# digest pin in-place and includes the resolved tag in the commit message.
FROM python:3.11-slim@sha256:2c285c669cc837aa3bcf1af23ea1932b7b5214f9c9d3aad22417446ad91cb4fb AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir --prefix=/install .


FROM python:3.11-slim@sha256:2c285c669cc837aa3bcf1af23ea1932b7b5214f9c9d3aad22417446ad91cb4fb AS runtime

COPY --from=builder /install /usr/local

ENV PYTHONUNBUFFERED=1

RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

CMD ["ac-infinity-mcp"]
