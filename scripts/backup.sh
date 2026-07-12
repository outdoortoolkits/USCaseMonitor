#!/usr/bin/env sh
set -eu
mkdir -p backups
stamp=$(date +%Y%m%d_%H%M%S)
docker compose exec -T db pg_dump -U uscase -d uscase | gzip > "backups/uscase_${stamp}.sql.gz"
find backups -type f -name 'uscase_*.sql.gz' -mtime +30 -delete
echo "Backup created: backups/uscase_${stamp}.sql.gz"

