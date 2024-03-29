Useful additions to k3s
=======================

More Things in this repo
------------------------
Notes on things I have deployed to my k3s cluster

- [Install Kubernetes Dashboard](deployed/dashboard/README.md)
- [Automatic upgrade of k3s](deployed/upgrade_plans/README.md)
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

Taint your master node
----------------------
If you have more than one node, it is best to not allow random pods to run
on your master node. This keeps it free to manage the cluster.

If you add this taint then most pods will not get scheduled on to it,
usually pods that are designed to run on the master will have a toleration 
for this taint in their helm charts.
```
kubectl taint node pi1 node-role.kubernetes.io/master=true:NoSchedule
```

Deploying to Raspberry Pi
-------------------------

When deploying a new raspi to the cluster these are some quick notes on
the settings to use. These apply to the 64 bit Raspberry Pi OS which I
recommend because many images support arm64 but not arm32.

The Pi 4 is suitable as a master or worker node.

```bash
# Use a USB drive for booting the raspi - raspi 4 will boot of USB by default

# Burn a standard raspios latest disto onto USB with balenea etcher
# Before moving the USB drive to the Pi and booting make these changes in 
#  the /boot partition

cd <mountpoint>/boot
touch ssh

vim cmdline.txt 
# add
#  " cgroup_enable=cpuset cgroup_memory=1 cgroup_enable=memory"
# to the end of the command line

vim config.txt 
# add these lines:
dtoverlay=disable-wifi
dtoverlay=disable-bt

# Boot from USB and find out the IP, then ssh to the pi with user pi pass raspberry.
# Maybe create a new user and delete pi if you are paranoid.

ssh my-new-pi-ip-address
sudo raspi-config
# change these:
#   set hostname, password and boot/autologin (boot to cmdline and no autologin)
#   set Performance/GPU Memory to 16GB (the minimum)
sudo apt update
sudo apt full-upgrade
#  (this may take a while)
```

Copying your USB image for reuse on multiple Pis


```bash
sudo shutdown now

# Put the USB key back in your desktop and make a image copy:
lsblk
#  look for the drive with /boot and /rootfs - that i the drive to image e.g. /dev/sdd
sudo dd if=/dev/sdd bs=1024 conv=sparse count=10M | gzip > 2021-05-rpi1-tiny4.zip
```

This image 2021-05-rpi1-tiny4.zip can be used to quickly burn a copy, to use the new
copy:
- use the gnome disks utility to expand the rootfs partition to fill the new USB drive
- boot in a pi and change the hostname if necessary


Cluster changes for Raspberry Pis 
---------------------------------

UPDATE - this blew up for system-upgrade-controller and is a pain for everything
that does support raspi. So I'm removing these taints and will put affinity 
onto any pods that can't run on raspi instead. The documentation below is still
a useful reminder of how to use taints and tolerations.

Unfortunately some pods which don't support multi arch may try to run
on the raspis so I added a taint to them e.g.
```bash
kubectl taint nodes pi3 architecture=arm64:NoSchedule
```

Remove taints by adding a '-' suffix to the command:
``` bash
kubectl taint nodes pi3 architecture=arm64:NoSchedule-
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

Finally if you want to ensure pods do run on a pi:
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