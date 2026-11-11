FROM python:3.12-slim

ARG IOW_UID=1000
ARG IOW_GID=1000

RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
# The agent runs as the host user that owns the target workspace, so files it
# writes stay readable by the target and git never sees a foreign owner.
RUN (getent group "${IOW_GID}" || groupadd -g "${IOW_GID}" iow) \
 && useradd -o -m -u "${IOW_UID}" -g "${IOW_GID}" -s /bin/bash iow

WORKDIR /agent
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data && chown "${IOW_UID}:${IOW_GID}" /data

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ANONYMIZED_TELEMETRY=False \
    IOW_CONFIG=/pristine/iow.config.yml IOW_WORKSPACE=/workspace/target
USER ${IOW_UID}:${IOW_GID}
EXPOSE 8042
CMD ["python", "-m", "iow"]
