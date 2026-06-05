FROM python:3.11-slim

WORKDIR /app

# System dependencies required by camoufox (Firefox-based) and Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install system deps for Firefox, then download browser binaries
RUN playwright install-deps firefox
RUN scrapling install

COPY scraper.py .

CMD ["python", "-u", "scraper.py"]
