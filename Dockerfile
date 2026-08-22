FROM python:3.11-slim

# ffmpeg: video -> audio extraction + segmenting for the session media
# pipeline (brain slice 3). slim image ships without it.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Headless Chromium for link rendering (LINK_RENDER_ENABLED). --with-deps
# pulls the system libraries Chromium needs on slim images. Kept AFTER the
# pip layer so requirement changes don't re-download the browser.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]