# Monitoring with Prometheus and Grafana

This is a second try using the instructions here:

https://k21academy.com/docker-kubernetes/prometheus-grafana-monitoring/

The script install.sh says it all - this is a really easy helm install
of a full monitoring stack. For my mixed cluster of pis and intel nuc
it just worked.

Once installed and port forward is running browse http://locahost:3000

To avoid running the port forward I created an ingress [here](../ingress-nginx/grafana-ingress.yaml).

And then set the host name to point at any of the cluster nodes in ets/hosts
on my workstation.
