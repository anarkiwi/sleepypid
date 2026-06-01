# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sleepypid/ ./sleepypid/

# Build/test stage: `docker build --target test` fails if tests or lint fail.
FROM base AS test
RUN pip install --no-cache-dir pylint pytest
RUN PYTHONPATH=sleepypid pylint --fail-under=8 sleepypid/sleepypid.py
RUN PYTHONPATH=sleepypid python sleepypid/test_sleepypid.py

# Runtime stage: the sleepypid daemon. Exposes Prometheus metrics on 9110.
FROM base AS runtime
EXPOSE 9110
ENTRYPOINT ["python", "sleepypid/sleepypid.py"]
