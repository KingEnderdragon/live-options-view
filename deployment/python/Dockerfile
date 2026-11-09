FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY deployment/python/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY market_data/ ./market_data/

# Each service manifest selects its own entry point.
CMD ["python", "-m", "market_data.live_market"]
