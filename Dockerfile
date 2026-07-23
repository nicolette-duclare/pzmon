FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends iw \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py
COPY templates /app/templates

# Persist the time-series database here (mount a volume to keep it across restarts).
ENV DB_PATH=/data/pzmon.db
VOLUME ["/data"]

EXPOSE 18080
CMD ["python", "app.py"]
