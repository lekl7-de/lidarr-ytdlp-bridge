FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

ENV DOWNLOAD_DIR=/downloads \
    COOKIES_FILE=/config/cookies.txt \
    PORT=9117

EXPOSE 9117

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9117"]
