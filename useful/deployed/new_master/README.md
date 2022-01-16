Disaster Recovery
=================

backups
-------
You will need to backup these files on the original master to be able to
do a disaster recovery:

| What | Location | notes |
|------|----------|-------|
| Latest snapshot file | /var/lib/rancher/k3s/server/db/snapshots/ |
| K3S token file | /var/lib/rancher/k3s/server/node-token | No Longer needed |

I successfully used this approach to spin up a new master on raspi 64 when
previously it was a rapsi 32 server.

Restore from Backup with brand new server
=========================================

build a new server with your etcd snapshot
------------------------------------------

Note that we use --disable traefik since we are using ingress-nginx instead for ingress.

``` bash
curl -fL https://get.k3s.io | sh -s - server --disable traefik
systemctl stop k3s.service
sudo k3s server --cluster-reset --cluster-reset-restore-path $(pwd)/on-demand-1627314923
# test with
sudo k3s server --disable traefik
# stop with ^C
systemctl start k3s.service
```

re join the workers
-------------------

    kubectl delete node pi4
    kubectl delete node gknuc
    add nodes as per the quickstart


NOTE: pi4 needed:

    curl -sfL https://get.k3s.io | K3S_URL=https://192.168.86.41:6443 K3S_TOKEN=K102e-redacted-be5ab60ba sh -
instead of

    curl -sfL https://get.k3s.io | K3S_URL=https://pi1:6443 K3S_TOKEN=K102e-redacted-be5ab60ba sh -


The former caused journal -fu k3s-agent
to get this error:

    Jul 27 21:34:37 pi4 k3s[1101]: time="2021-07-27T21:34:37.695461493+01:00" level=error msg="failed to get CA certs: Get \"https://127.0.0.1:6444/cacerts\": read tcp 127.0.0.1:57846->127.0.0.1:6444: read: connection reset by peer"

I can't easily explain this. Perhaps there is a routing issue on this Pi. TODO
see what happens when joining further pis.

Also NOTE:
   I needed to delete and re-create the service account for kubernetes-dashboard
   before I could log in

