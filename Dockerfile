FROM python:3.12-slim

WORKDIR /app

# System deps for Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxcomposite1 libxrandr2 libgbm1 \
    libpango-1.0-0 libpangocairo-1.0-0 \
    libasound2 libxshmfence1 libxdamage1 libxfixes3 \
    fonts-liberation libappindicator3-1 \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser for Playwright
RUN playwright install chromium
RUN playwright install-deps chromium

COPY main.py .

EXPOSE 8000

# Single worker — browser singleton is not safe across workers
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--timeout-keep-alive", "120"]
