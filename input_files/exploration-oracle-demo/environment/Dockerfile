# Stage 0: compile runner to bytecode only (no source in final image → no answer leak)
FROM python:3.11-slim AS builder
WORKDIR /build
COPY runner.py .
RUN python3 -m py_compile runner.py \
    && cp __pycache__/runner.*.pyc /build/runner.pyc

# Stage 1: final image with only .pyc (agent cannot read source)
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY config.json /app/
COPY --from=builder /build/runner.pyc /app/runner.pyc

# Run bytecode only; no runner.py in image
CMD ["python3", "/app/runner.pyc"]
