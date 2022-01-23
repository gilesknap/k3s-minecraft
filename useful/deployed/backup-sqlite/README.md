Backup and Restore of Sqlite K3S Database
=========================================

K3S uses sqlite for its database when a single master is configured.

At present there is no documentation on K3S as to how to backup and restore 
this. I have tested the naive approach of zipping the whole of the folder
/var/lib/rancher/k3s/server and verified that I can restore this to a new
machine. Read on for details.

Backup server folder to NFS
=======================
The enclosed `cron_sqlite_backup.yaml` sets up a kubernetes cron job to backup the 
server folder periodically to an NFS folder. For this to work as is requires
that the auto provisioning storage class is set up 
(see [here](../dynamic-nfs/README.md)). If not you could manually mount an NFS
folder in a in the volumes section.

IMPORTANT if you have changed any server options in /etc/systemd/system/k3s.service
or /etc/rancher/k3s/config.yaml. Take a note of them (these could be backed up
too I guess)


Bring up a new master node from backup
======================================

I tested this by bringing up an amd64 VM with ubuntu server and using that 
instead of my raspberry pi arm64 ubuntu desktop. This worked well and demonstrates
that cross processor architecture restore is possible.

Steps

- Shutdown the existing master server
- Bring up the VM and give it the same IP address as the old master
- install k3s using 
  - curl -sfL https://get.k3s.io | sh -
- systemctl stop k3s
- sudo -s
- cd /var/lib/rancher/k3s
- mv server server.old
- tar -xf /PATH_TO_BACKUP_TAR_FILE
- make sure that `/etc/systemd/system/k3s.service` and `/etc/rancher/k3s/config.yaml`
  have the same startup options as the original master  
  - e.g. for the ingress setup we added `--no-deploy traefik`
- systemctl start k3s

Thats it! Everything should spring back to life.

set fixed IP in ubuntu
----------------------

To set the IP in the new machine I used a fixed address (because Google Wifi
would have made this laborious otherwise)

edit /etc/netplan/???.yaml and make it look like the following
```
network:
  version: 2
  renderer: networkd
  ethernets:
    ens3:
      dhcp4: no
      addresses:
        - 192.168.121.221/24
      gateway4: 192.168.121.1
      nameservers:
          addresses: [8.8.8.8, 1.1.1.1]
```

then
```bash
sudo netplan apply
```

