# k3s-minecraft
Minecraft servers managed in a lightweight Kubernetes cluster

Thanks to https://github.com/itzg/docker-minecraft-server and https://k3s.io/

# Intro
This is a very easy set of instructions for setting up a Kubernetes cluster
and deploying minecraft java edition servers.

It has been tested on Ubuntu 20.10 and Raspbian Buster.

Give it a try, K3S provides a good uninstaller that will clean up your system
if you decide to back out.

# Installation Steps

## Install K3S lightweight Kubernetes
Execute this command on your server to set up the cluster master (aka K3S Server node):
```
curl -sfL https://get.k3s.io | sh -
```

Install kubectl on the workstation from which you will be managing the cluster
(workstation==server if you have one machine only)
```
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
```
Go to the server machine and copy over the kubectl configuration to your
workstation
```
sudo scp  /etc/rancher/k3s/k3s.yaml <YOUR_ACCOUNT>@<YOUR_WORKSTATION>:.kube/config
# edit the file .kube/config replacing 127.0.0.1 with your server IP Address
```

## Create a minecraft namespace and context
From the workstation execute the following:
```
kubectl create namespace minecraft
kubectl config set-context minecraft --namespace=minecraft --user=default --cluster=default
kubectl config use-context minecraft
```

## Install helm
Execute this on the workstation:
```
curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/master/scripts/get-helm-3
bash get_helm.sh
```

## Deploy a Minecraft Server to your Cluster
Take a copy of the file **minecraft-helm.yaml** and call it **my-first-mc.yaml**.
Edit the file and follow the comments to apply the settings you require for your
server.

Then install using helm. I recommend using the same release name (argument
after --install) as the basename
of the file so you can easily keep track of which file your servers got their
settings from. You can omit the rcon password if you disabled rcon in the yaml.
```
export HELM_EXPERIMENTAL_OCI=1
helm repo add minecraft-server-charts https://itzg.github.io/minecraft-server-charts/

helm upgrade --install my-first-mc -f my-first-mc.yaml --set minecraftServer.eula=true,rcon.password=<YOUR_RCON_PWD> minecraft-server-charts/minecraft
```

THAT IS ALL! You should see a minecraft server spin up with your server address
and the port you specified in myfirst-mc.yaml.

You can edit the yaml and repeat the above command to update settings without
deleting your world data. You can remove the minecraft server including world
data with:
```
helm delete my-first-mc
```

# Links

|description    | link |
|---------------|------|
|itgz minecraft image:          |  https://github.com/itzg/docker-minecraft-server |
|itgz helm chart:               |  https://artifacthub.io/packages/helm/minecraft-server-charts/minecraft |
|K3S:                           |  https://k3s.io/ |
|Helm:                          |  https://helm.sh |
|kubectl:                       |  https://kubernetes.io/docs/reference/kubectl/overview/ |
#
#
# Additional Nice to Have Stuff

## check the latest logs for k3s

Go to your master / server and do:

    sudo journalctl -fu k3s

## Add a worker node (aka K3S Agent) to your cluster
First get the node token on your server:
```
sudo cat /var/lib/rancher/k3s/server/node-token
```
Then execute the following on your new node to create a K3S Agent:
```
curl -sfL https://get.k3s.io | K3S_URL=https://gknuc:6443 K3S_TOKEN=<your token string>  sh -
```


## Add a Raspberry Pi to the cluster
For a Raspberry Pi make sure you set imageTag: multiarch in the yaml file.

It should be possible to
install the K3S Server node on a Pi, but you would need one Pi for the K3S
Server and one Pi K3S Agent (worker node)
per minecraft server for decent performance.

You need the following changes before installing:
```
sudo update-alternatives --set iptables /usr/sbin/iptables-legacy
# edit /boot/cmdline and make sure the single line contains:
#  cgroup_memory=1 cgroup_enable=memory
sudo reboot
```

## Do Backups of running servers and more ...
Use the script included in this repo to save/restore backups and save typing
for a few other commands. See details by invoking the following:
```
$ source k8s-mc.sh
$ k8s-mchelp

    This script adds the following functions to bash. It assumes that your
    servers have been deployed using:
        https://artifacthub.io/packages/helm/minecraft-server-charts/minecraft
    or using the k8s-mcdeploy function below.

    In all cases below <server name> is the helm chart release name.

    k8s-mclist
        list all minecraft servers deployed to the cluster

    k8s-mcports
        details of the ports exposed by servers and rcon

    k8s-mcstart <server name>
        start the server (set replicas to 1)

    k8s-mcstop <server name>
        stop the server (set replicas to 0)

    k8s-mcexec <server name>
        execute bash in the server's container

    k8s-mclog <server name> [-p] [-f]
        get logs for the server
        -p = log for previous instance after a restart of the pod
        -f = attach to the log and monitor it

    k8s-mcdeploy <my_server_def.yaml>
        deploys a server to the cluster with release name my_server_def and
        with values overrides from my_server_def.yaml

        recommended: copy and edit minecraft-helm.yaml for most common options,
        see in file comments for details.

    k8s-mcbackups
        List the backups in the folder /mnt/bigdisk/minecraft-k8s-backup

    k8s-mcbackup <server name>
        zips up the server data folder to a dated zip file and places it
        in the folder /mnt/bigdisk/minecraft-k8s-backup (prompts for MCBACKUP if not set)

    k8s-mcrestore <server name> <backup file name>
        restore the world from backup for a server, overwriting its
        current world
```

More Useful Things to do with your Cluster
------------------------------------------

see [More Useful Stuff](useful/README.md)
