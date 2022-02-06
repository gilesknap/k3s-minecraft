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

# I had to stop using -a which would give a perfect archive
# the thousands of symlinks in my photo albums seemed to cause 
# issues -rptgov gives the same as -a but with ignore symlinks
rsync -rptgov --delete \
  "${SOURCE_DIR}/" \
  --link-dest "${LATEST_LINK}" \
  --exclude=".cache" \
  "${BACKUP_PATH}"

unlink "${LATEST_LINK}"
ln -s "${BACKUP_PATH}" "${LATEST_LINK}"

# create a file to verify completion of this backup
echo done > "${BACKUP_PATH}.completed"
