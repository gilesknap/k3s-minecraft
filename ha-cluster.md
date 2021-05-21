# Installing a High Availability Cluster

Although the documentation here
https://rancher.com/docs/k3s/latest/en/installation/ha-embedded/ is correct
it can be a little confusing setting up the HA cluster and getting it
running as a service on each node.

You must have a minimum of 3 master nodes. This can then tolerate the loss
of 1 master without the cluster becoming headless. If you have 5 masters
then you can tolerate the loss of 2 nodes. Do not use even numbers, there
is a great explanation
[here](https://discuss.kubernetes.io/t/high-availability-host-numbers/13143)

In the following, replace CHANGE_ME with a chosen secret and
FIRST_MASTER_NODE with the name or IP of the first machine.

## First Master Node
This command will initialise your first master as a multi master cluster.

WARNING: this erases etcd and resets your cluster to new.
```
curl -sfL https://get.k3s.io | sh - --cluster-init --token CHANGE_ME
```

## Further Nodes
This command will join the server as a another cluster master and start it as a service
```
curl -sfL https://get.k3s.io |  sh - --server  https://FIRST_MASTER_NODE:6443 --token CHANGE_ME
```

Note that if the first master node is lost you need to replace FIRST_MASTER_NODE with one of the working master node when bringing up a replacement.

## Raspberry Pi results
I have 3 Rapberry Pi 4 with 4GB RAM as my master nodes. This is
probably overkill.

They run at about 15% utilisation and < 1GB memory usage.

I have the [Flirc passive cooling case](https://flirc.tv/more/raspberry-pi-4-case)
and this can sustain 100% utilization for 30 minutes or more with no throttling.
See my test results below.

![alt text](https://github.com/gilesknap/k3s-minecraft/blob/main/images/mytest.png "pi3 stress test")
