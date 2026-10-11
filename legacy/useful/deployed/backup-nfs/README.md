Backup the NAS Data to separate disk
====================================

This backup process copies the NAS shares to a separate backup NFS mount.
It uses rsync as described here:
https://linuxconfig.org/how-to-create-incremental-backups-using-rsync-on-linux
and this gives an incremental backup (with granularity at the file level)

Create a config map with the rsync script in it and deploy the cron job:
```
kubectl create configmap rsync-backup --from-file=./rsync-backup.sh -n admin
kubectl apply -f cron_backup_nfs.yaml
```

Note the backup folder NFS mount targeted by the above required no_root_squash
because rsync needs full control to set the UIDs on the files it is
creating. HOWEVER, for speed I switched to running the pod directly on the
node that has the backup filesystem attached. That way we can use hostPath
for the backup and it is MUCH faster as NFS is rubbish for lots of small files.