FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# git for clone/commit/push, openssh-client for `ssh-keygen -Y sign`
# (commit signing matches gpg.format=ssh in the user's gitconfig).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src /app/src

# Non-root runtime user; persistent dedupe DB lives in /var/lib/comment-commander.
RUN useradd --create-home --home-dir /home/app --shell /usr/sbin/nologin app && \
    mkdir -p /var/lib/comment-commander && \
    chown -R app:app /var/lib/comment-commander /home/app

USER app
ENV PYTHONPATH=/app/src \
    HOME=/home/app
WORKDIR /app/src
EXPOSE 8000

CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
