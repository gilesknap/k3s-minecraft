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

To get notified of alerts via slack follow these simple instructions which
work with a free slack installation:

https://medium.com/@_oleksii_/grafana-alerting-and-slack-notifications-3affe9d5f688

# Adding a Data Source

log in with default user/pass at https://localhost:3000

Find add data source panel and use these settings:

- URL: http://prometheus-kube-prometheus-prometheus.monitoring:9090/
- Access: Server
- Name: Prometheus, Default: True

# useful dashboards

Use import dashboard and choose these numbers

- 1860: full node exporter dashboard with every stat possible on your nodes.
