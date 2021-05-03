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

## Add a worker node (aka K3S Agent) to your cluster
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
Finally, start a proxy and goto the Dashboard URL, use the above token to log in.
```
kubectl proxy &
browse to http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy
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
Use the script included in this repo to do backups and save typing for
a few other commands. See the functions in mc-k8.sh for details, some examples
below:
```
$ source mc-k8s.sh

$ mclist
MC SERVER NAME      RUNNING
the-lockdown-krew   1
adventure-of-doom   1
skorponok           1
science-rpi         1
science-lab         1

$ mcstop the-lockdown-krew
localhost:25565 : version=1.16.5 online=0 max=20 motd='Where the Lockdown Krew meet'
deployment.apps/the-lockdown-krew-minecraft scaled

$ mclist
MC SERVER NAME      RUNNING
adventure-of-doom   1
skorponok           1
science-rpi         1
science-lab         1
the-lockdown-krew   <none>

$ mcbackup science-lab
science-lab is running
Automatic saving is now disabled
Saving the game (this may take a moment!)Saved the game
tar: removing leading '/' from member names
data/
data/world/
data/world/poi/
data/world/poi/r.0.-2.mca
...
data/white-list.txt.converted
data/ops.txt.converted
Automatic saving is now enabled

$ mcdeploy giles-servers/devils-deep.yaml
"minecraft-server-charts" already exists with the same configuration, skipping
Release "devils-deep" does not exist. Installing it now.
NAME: devils-deep
LAST DEPLOYED: Mon May  3 12:03:53 2021
NAMESPACE: minecraft
STATUS: deployed
REVISION: 1
TEST SUITE: None
NOTES:
Get the IP address of your Minecraft server by running these commands in the
same shell:

!! NOTE: It may take a few minutes for the LoadBalancer IP to be available. !!

You can watch for EXTERNAL-IP to populate by running:
  kubectl get svc --namespace minecraft -w devils-deep-minecraft```

