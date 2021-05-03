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
Execute this command on your server to set up the K3S cluster Server node:
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
sudo sudo scp  /etc/rancher/k3s/k3s.yaml <YOUR_ACCOUNT>@<YOUR_WORKSTATION>:.kube/config
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
Take a copy of the file minecraft-helm.yaml and call it my-first-mc.yaml.
Edit the file and follow the comments to apply the settings you require for your
server.

Then install using helm. I recommend using the same release name (argument
after --install) as the basename
of the file so you can easily keep track of which file your servers got their
settings from. You can omit the rcon password if you disabled rcon in the yaml.
```
helm upgrade --install my-first-mc -f my-first-mc.yaml --set minecraftServer.eula=true,rcon.password=<YOUR_RCON_PWD> minecraft-server-charts/minecraft
```

THAT IS ALL! You should see a minecraft server spin up with your server address
and the port you specified in myfirst-mc.yaml.

You edit the yaml and repeat the above command to tweak settings without
deleting your world data. You can remove the minecraft server including world
data with:
```
helm delete my-first-mc
```

# Links

|description    | link |
|---------------|------|
|itgz minecraft image:         |  https://github.com/itzg/docker-minecraft-server |
|itgz helm chart:          |  https://artifacthub.io/packages/helm/minecraft-server-charts/ |minecraft
|K3S:           |  https://k3s.io/ |
|Helm:          |  https://helm.sh |
|kubectl:       |  https://kubernetes.io/docs/reference/kubectl/overview/
#
#
# Additional Nice to Have Stuff

## Add a worker node to your cluster
First get the node token on your server:
```
sudo cat /var/lib/rancher/k3s/server/node-token
```
Then execute the following on your new node to create a K3S Agent:
```
curl -sfL https://get.k3s.io | K3S_URL=https://gknuc:6443 K3S_TOKEN=<your token string>  sh -
```

## Install the Kubernetes Dashboard
Execute this on your workstation
```
GITHUB_URL=https://github.com/kubernetes/dashboard/releases
VERSION_KUBE_DASHBOARD=$(curl -w '%{url_effective}' -I -L -s -S ${GITHUB_URL}/latest -o /dev/null | sed -e 's|.*/||')
kubectl create -f https://raw.githubusercontent.com/kubernetes/dashboard/${VERSION_KUBE_DASHBOARD}/aio/deploy/recommended.yaml
```
Then create the admin user and role with the yaml file supplied in this repo and
get a token for the user
```
kubectl create -f dashboard-admin.yaml
kubectl -n kubernetes-dashboard describe secret admin-user-token | grep '^token'
```
finally start a proxy and goto the Dashboard URL
```
kubectl proxy &
browse to http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy
```

## Add a Raspberry Pi to the cluster
For a raspberry pi make sure you set the imageTag: multiarch in the yaml file.

It should be possible to
install the K3S Server node on a pi, but you would need one pi for the K3S
Server and Pi K3S Agent (worker node)
per minecraft server for decent performance.

You need the following changes before installing:
```
sudo update-alternatives --set iptables /usr/sbin/iptables-legacy
# edit /boot/cmdline and make sure the single line contains:
#  cgroup_memory=1 cgroup_enable=memory
sudo reboot
```