Backup Snapshots
================

The yaml in this folder specifies cron job that will
take a snapshot of the etcd database and save it away in
an nfs volume.

It runs on the master so that it has access
to the certs required for etcdctl to connect to the
etcd endpoint.

Comments
========

This was fun to do but maybe not the best practice.

So I guess this container is exposed to container
escape. See hostPath here https://kubernetes.io/docs/concepts/storage/volumes/.
(but how does someone get in here in the first place?)

For the moment I'm happy that my basic k3s installation has a disaster
recovery capability.

I Need to review other ways to do this.

The master already does snapshots but to the local filesystem, so no good
for master node failure.

There is supposed to be a way to make the built in snapshots go to a
configured directory. see --etcd-snapshot-dir in
https://rancher.com/docs/k3s/latest/en/backup-restore/

I could not get the above to work, but if it did then an nfs mount here
would be a much simpler solution.