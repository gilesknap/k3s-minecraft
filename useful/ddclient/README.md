Intro
=====
ddclient can update your dynamic IP for the openDNS service.

The service is free and recommended for fast and safe DNS with
family filters.

See here for openDNS family https://www.opendns.com/setupguide/#familyshield

See here for details of ddclient usage
https://support.opendns.com/hc/en-us/articles/227987727-Linux-IP-Updater-for-Dynamic-Networks

commands
========

Get the helm repo
```
helm repo add k8s-at-home https://k8s-at-home.com/charts/
helm repo update
```

Install with some config for OpenDNS and for running on my pi nodes
only

```
sed s/\${password}/YOUR_PASSWORD/ values.yaml | helm upgrade -i  ddclient --namespace admin k8s-at-home/ddclient -f -
# or maybe (if ddclient picks up environ vars in /defaults/ddclient.conf - not yet tested)
# helm install ddclient k8s-at-home/ddclient -f values.yaml --set env.password=YOUR_PASSWORD
```

