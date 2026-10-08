FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# libmagic: needed by neonize (the linked WhatsApp client)
RUN apt-get update && apt-get install -y --no-install-recommends libmagic1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# shown on the Changelog page (app/changelog.py)
COPY CHANGELOG.md ./CHANGELOG.md

# Settings files (database choice, FTP backup). docker-compose mounts the
# app_data volume here; declaring it also keeps the files in an anonymous
# volume when a compose file has no volume for it. The settings also have a
# copy in the database (app/settings_store.py).
ENV DATA_DIR=/srv/data
VOLUME /srv/data

EXPOSE 8000

# Uses the Python already in the image, so no curl is needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
