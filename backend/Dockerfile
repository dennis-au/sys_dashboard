FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 sentinel \
    && useradd --uid 1000 --gid sentinel --create-home --shell /usr/sbin/nologin sentinel \
    && mkdir -p /run/secrets /run/forgejo-service-credentials /var/lib/sentinel/managed-ssh \
    && chown sentinel:sentinel /var/lib/sentinel /var/lib/sentinel/managed-ssh \
    && touch /run/secrets/postgres_password

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=sentinel:sentinel . .
RUN chown sentinel:sentinel /app

USER sentinel
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
