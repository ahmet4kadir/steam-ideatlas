FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir requests
COPY steam_crawl.py .
ENV STEAM_DB=/data/steam.db PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "steam_crawl.py"]
