Useful additions to k3s
=======================

More Things in this repo
------------------------
Notes on things I have deployed to my k3s cluster

- [Ingress and Cert Manager](deployed/ingress-nginx/README.md)
- [backup etcd](deployed/backup/README.md)
- [ddclient for opendns and noip](deployed/ddclient/README.md)
- [use docker creds for better pull limits](deployed/docker-account/README.md)
- [auto provisioning nfs persistent volumes](deployed/dynamic-nfs/README.md)
- [grafana monitoring](deployed/monitoring/README.md)
- [create new master node from etcd backup](deployed/ambassador/README.md)

Other Repos
-----------
For a very simple docker build and deploy to kubernetes for a simple service
see. This pattern should work for any simple service.

- https://github.com/gilesknap/noip

Other useful tips
=================

Automated Update of K3S version
-------------------------------
For automatic Update to Latest Stable K3S version 
  - see https://rancher.com/docs/k3s/latest/en/upgrades/automated/

Raspberry Pis
-------------
Unfortunately some pods which don't support multi arch may try to run
on the raspis so I added a taint to them e.g.
```bash
kubectl taint nodes pi3 architecture=arm:NoSchedule
```
Then for those items you do want to run there, add a toleration.
e.g.
``` yaml
tolerations:
- key: "architecture"
  operator: "Equal"
  value: "arm"
  effect: "NoSchedule"
```

Alternatively, to quickly avoid running on arm nodes (only run on amd64) edit the
deployment and set:

``` yaml
spec:
  template:
    spec:
      nodeSelector:
        kubernetes.io/arch: amd64
```

Finally to ensure pods do run on a pi:
``` yaml
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: kubernetes.io/arch
                operator: In
                values:
                - arm64
                - arm

```

When deploying a new raspi to the cluster these are some quick notes on
the settings to use. These apply to the 64 bit Raspberry Pi OS which I
recommend because many images support arm64 but not arm32.

```
Use a USB drive for booting the raspi - raspi 4 will boot of USB by default

- Burn a standard raspios latest disto onto USB with balenea etcher
- Before booting make these changes in the /boot partition

touch ssh

edit /boot/cmdline.txt and add
  " cgroup_memory=1 cgroup_enable=memory"
to the end of the command line

edit /boot/config.txt and add these lines:
    dtoverlay=disable-wifi
    dtoverlay=disable-bt

- Boot from USB and find out the IP, then ssh to the pi with user pi pass raspberry.
- Maybe create a new user and delete pi if you are paranoid.

- sudo raspi-config
change these:
  set hostname, password and boot/autologin (boot to cmdline and no autologin)
  set Performance/GPU Memory to 16GB (the minimum)
- sudo apt update
- sudo apt full-upgrade
  (this may take a while)

- sudo shutdown now

- Put the USB key back in your desktop and make a image copy:
- lsblk
  look for the drive with /boot and /rootfs - that i the drive to image e.g. /dev/sdd
- sudo dd if=/dev/sdd bs=1024 conv=sparse count=10M | gzip > 2021-05-rpi1-tiny4.zip

This image 2021-05-rpi1-tiny4.zip can be used to quickly burn a copy, to use the new
copy:
- use the gnome disks utility to expand the rootfs partition to fill the new USB drive
- boot in a pi and change the hostname if necessary
```

Stuck Deleting ...
------------------
When deleting a master node k3s got stuck on the finalizer.

The workaround is below. In general this works for resources that cant
run their finalizers.

``` bash
kubectl get node -o name <nodename> | xargs -i kubectl patch {} -p '{"metadata":{"finalizers":[]}}' --type=merge
```

Node Affinity
-------------
to prefer running on a given node edit the deployment and add


``` yaml
spec:
  template:
    nodeAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 1
        preference:
          matchExpressions:
          - key: kubernetes.io/hostname
            operator: In
            values:
            - WORKER_NODE_IP
```