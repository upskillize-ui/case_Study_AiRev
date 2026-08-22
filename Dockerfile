# Debian 12 (bookworm), pinned deliberately — NOT the floating slim tag.
# 22 Aug the python:3.11-slim tag moved to Debian 13 (trixie), which
# Playwright 1.49 does not know: it fell back to an Ubuntu 20.04 package
# list and the build died on ttf-ubuntu-font-family / ttf-unifont, packages
# trixie does not carry. 1.49's supported set is debian11, debian12,
# ubuntu20.04/22.04/24.04 — bookworm is both supported and the base every
# earlier build of this Space already used.
FROM python:3.11-slim-bookworm

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
#
# The browser goes in /opt, not the build user's home: whichever uid the
# Space ends up running as must be able to READ it, and a browser installed
# into /root/.cache is invisible to every other user. a+rX makes that
# explicit rather than incidental.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright \
    && rm -rf /var/lib/apt/lists/*

# Devanagari and other Indian scripts, for SCREENSHOTS only: a student's
# Marathi or Hindi portfolio page renders as empty boxes without them, and
# the vision reviewer would describe a page of tofu. Harvested page text is
# unaffected either way. Non-fatal on purpose — a missing font must never
# be the reason a cohort cannot be reviewed.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends fonts-indic || true) \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]