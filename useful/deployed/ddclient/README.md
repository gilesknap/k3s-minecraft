Intro
=====
ddclient can update your dynamic IP for the openDNS service
and the same client works for the noip dynamic DNS service too.
A helm chart for ddclient is provided by the seminal https://k8s-at-home.com/

I deploy 2 instances of this one for each of the above DDNS services as follows:

OpenDNS
========

The service is free and recommended for fast and safe DNS with
family filters.

See here for openDNS family https://www.opendns.com/setupguide/#familyshield

See here for details of ddclient usage
https://support.opendns.com/hc/en-us/articles/227987727-Linux-IP-Updater-for-Dynamic-Networks
Get the helm repo
```
helm repo add k8s-at-home https://k8s-at-home.com/charts/
helm repo update
kubectl create namespace admin
```

Install with some config for OpenDNS and for running on my pi nodes only.

```
sed s/\${password}/YOUR_PASSWORD/ opendns.yaml | helm upgrade -i  ddclient --namespace admin k8s-at-home/ddclient -f -
```
NoIP
====

The service has a free tier and provides a Dynamic Public DNS entry for your 
home router for plans where the ISP changes the IP address occasionally.

See here for NoIP https://www.noip.com/remote-access

See here for details of ddclient usage
https://www.andreagrandi.it/2014/09/02/configuring-ddclient-to-update-your-dynamic-dns-at-noip-com/

Get the helm repo
```
helm repo add k8s-at-home https://k8s-at-home.com/charts/
helm repo update
kubectl create namespace admin
```

Install with some config for NoIP and for running on my pi nodes only.

```
sed s/\${password}/YOUR_PASSWORD/ noip.yaml | helm upgrade -i  ddclient --namespace admin k8s-at-home/ddclient -f -
```

