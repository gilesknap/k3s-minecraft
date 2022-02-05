#!/bin/bash

# thanks to https://linuxconfig.org/how-to-create-incremental-backups-using-rsync-on-linux
# A script to perform incremental backups using rsync

SOURCE_DIR=${1}
BACKUP_DIR=${2}

set -o errexit
set -o nounset
set -o pipefail

readonly DATETIME="$(date '+%Y-%m-%d_%H:%M:%S')"
readonly BACKUP_PATH="${BACKUP_DIR}/${DATETIME}"
readonly LATEST_LINK="${BACKUP_DIR}/latest"

mkdir -p "${BACKUP_DIR}"

rsync -av --delete \
  "${SOURCE_DIR}/" \
  --link-dest "${LATEST_LINK}" \
  --exclude=".cache" \
  "${BACKUP_PATH}"

rm -rf "${LATEST_LINK}"
ln -s "${BACKUP_PATH}" "${LATEST_LINK}"
