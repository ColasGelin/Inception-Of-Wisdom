# The hello target. Code is NOT baked in: it is bind-mounted from the runtime
# workspace at /app, so restarting the container picks up patched code.
FROM python:3.12-slim

ARG IOW_UID=1000
ARG IOW_GID=1000
# Same UID/GID as the owner of the workspace on the host, so the target can
# read files the agent wrote and git never sees a foreign owner.
RUN (getent group "${IOW_GID}" || groupadd -g "${IOW_GID}" app) \
 && useradd -o -M -u "${IOW_UID}" -g "${IOW_GID}" -s /usr/sbin/nologin app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
USER ${IOW_UID}:${IOW_GID}
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
