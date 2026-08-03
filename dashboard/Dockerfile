FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG HUBBLE_VERSION=v1.16.4
RUN curl -sSL -o /tmp/hubble.tar.gz \
    "https://github.com/cilium/hubble/releases/download/${HUBBLE_VERSION}/hubble-linux-amd64.tar.gz" \
    && tar -xzf /tmp/hubble.tar.gz -C /usr/local/bin hubble \
    && chmod +x /usr/local/bin/hubble \
    && rm /tmp/hubble.tar.gz

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
