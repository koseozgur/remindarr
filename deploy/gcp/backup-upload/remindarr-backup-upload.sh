#!/bin/bash
# Copy the newest local backup to GCS, once. Runs on the VM host (not in the
# container) from remindarr-backup-upload.timer.
#
# The VM's service account may only *create* objects in the bucket: it can't
# list, read, overwrite or delete them, so a compromised VM can't destroy the
# off-site copies. That's also why this uses a plain JSON-API upload rather
# than `gcloud storage cp`, which reads the object first. ifGenerationMatch=0
# makes the upload fail rather than overwrite. The bucket's lifecycle rule
# deletes copies after 30 days.
#
# REMINDARR_BACKUP_BUCKET comes from /etc/remindarr/backup.env.
set -euo pipefail

dir=/var/lib/remindarr/backups
state="${STATE_DIRECTORY:-/var/lib/remindarr-backup-upload}/last-uploaded"
bucket="${REMINDARR_BACKUP_BUCKET:?set REMINDARR_BACKUP_BUCKET in /etc/remindarr/backup.env}"

newest=$(find "$dir" -maxdepth 1 -name 'remindarr-*.db' -printf '%f\n' 2>/dev/null | sort | tail -1)
if [[ -z "$newest" ]]; then
    echo "no backups in $dir yet"
    exit 0
fi
if [[ -f "$state" && "$(cat "$state")" == "$newest" ]]; then
    echo "$newest already uploaded"
    exit 0
fi

token=$(curl -sSf -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token |
    jq -r .access_token)

status=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/vnd.sqlite3' \
    --data-binary "@$dir/$newest" \
    "https://storage.googleapis.com/upload/storage/v1/b/$bucket/o?uploadType=media&ifGenerationMatch=0&name=$newest")

case "$status" in
    200) echo "uploaded $newest" ;;
    412) echo "$newest was already in the bucket" ;;
    *) echo "upload of $newest failed: HTTP $status" >&2; exit 1 ;;
esac
echo "$newest" > "$state"
