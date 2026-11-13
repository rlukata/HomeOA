# Backup/restore tool image: pg_dump/pg_restore 17 (matches the server), age, and the
# AWS CLI for any S3-compatible bucket (no vendor assumed; set BACKUP_S3_ENDPOINT).
# Build from the repository root:  docker build -f deploy/backup.Dockerfile -t homeoa-backup:local .
FROM postgres:17
RUN apt-get update \
 && apt-get install -y --no-install-recommends age awscli ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY deploy/backup.sh deploy/restore.sh deploy/backup-loop.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/backup.sh /usr/local/bin/restore.sh /usr/local/bin/backup-loop.sh
# Status volume mount point, owned by the backup user (a fresh named volume copies this).
RUN mkdir -p /status && chown postgres:postgres /status
USER postgres
WORKDIR /tmp
ENTRYPOINT []
CMD ["backup-loop.sh"]
