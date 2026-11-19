FROM python:3.12-slim-bookworm

# libarchive reads ZIP, 7z, tar and most RARs; unar covers old solid RARs,
# encrypted RAR5 and split archives
RUN apt-get update \
 && apt-get install -y --no-install-recommends libarchive13 unar \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ferry ferry
COPY web web

ENV FERRY_DATA=/data HOME=/data USER=ferry PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8420
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8420/api/health', timeout=4)"
CMD ["uvicorn", "ferry.main:app", "--host", "0.0.0.0", "--port", "8420", "--timeout-keep-alive", "30"]
