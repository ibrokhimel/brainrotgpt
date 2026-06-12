FROM python:3.12-slim

# Fonts so share cards render text on Linux (Pillow has no bundled TTF).
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# DB lives on a mounted volume by default (see docker-compose.yml).
ENV DB_PATH=/app/data/brainrot.db
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
