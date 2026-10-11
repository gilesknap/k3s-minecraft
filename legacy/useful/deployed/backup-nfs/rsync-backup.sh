#!/bin/sh

# thanks to https://linuxconfig.org/how-to-create-incremental-backups-using-rsync-on-linux
# A script to perform incremental backups using rsync

SOURCE_DIR=${1}
BACKUP_DIR=${2}

set -o errexit
set -o nounset
set -o pipefail

readonly DATETIME="$(date '+%Y-%m-%d_%H:%M:%S')"
readonly LATEST_LINK="${BACKUP_DIR}/latest"
BACKUP_PATH="${BACKUP_DIR}/${DATETIME}"

echo SOURCE PATH is $SOURCE_DIR
echo BACKUP DIR is $BACKUP_DIR

mkdir -p "${BACKUP_PATH}"

# check for a previous incompleted run by looking for the file called
# ${BACKUP_DIR}/${DATETIME}.in-progress
inprogress=$(find  ${BACKUP_DIR} -maxdepth 1 -name *.in-progress)
if test -z $inprogress ; then
    # previous run completed - use the new dated backup folder
    touch ${BACKUP_PATH}.in-progress
    echo "STARTING NEW INCREMENTAL BACKUP IN ${BACKUP_PATH} ..."
else
    # continue the backup in the in-progress folder (remove the suffix)
    BACKUP_PATH=${inprogress%.in-progress}
    echo "RESUMING INCOMPLETE INCREMENTAL BACKUP IN ${BACKUP_PATH} ..."
fi

# I had to stop using -a which would give a perfect archive.
# The thousands of symlinks in my photo albums seemed to cause rsync lockups. 
# Options -rptgov give the same as -a except ignore symlinks
rsync -rptgo --delete \
  "${SOURCE_DIR}/" \
  --info=SKIP3,STATS,FLIST,NAME \
  --link-dest "${LATEST_LINK}" \
  --exclude=".cache" \
  "${BACKUP_PATH}" \
  | grep -v "skipping non-regular"

if test -d ${LATEST_LINK} ; then unlink "${LATEST_LINK}"; fi
ln -s "${BACKUP_PATH}" "${LATEST_LINK}"

# Verify completion of this backup 
mv "${BACKUP_PATH}.in-progress" "${BACKUP_PATH}.completed"
