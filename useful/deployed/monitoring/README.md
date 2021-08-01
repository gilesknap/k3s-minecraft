Install
=======

Setup
-----

See deploy-monitoring.sh for deploying grafana

See remove-monitoring.sh to remove grafana

To use
------
- set up an ingress for grafana using
  - k apply -f ../ambassador/grafana.yaml
- goto https://gknuc.local/grafana/
  - login with admin and password from ;
    - kubectl get secret --namespace monitoring grafana -o jsonpath="{.data.admin-password}" | base64 --decode ; echo

- create a new data source and choose prometheus, set the server address to:

  - http://prometheus-server.monitoring.svc.cluster.local

- import a dashboard such as 1860 (node exporter full)
- actually this one seems better 11074


grafana helm chart output
-------------------------
```
NAME: grafana
LAST DEPLOYED: Sat Jul 31 07:56:06 2021
NAMESPACE: monitoring
STATUS: deployed
REVISION: 1
NOTES:
```
1. Get your 'admin' user password by running:

   - kubectl get secret --namespace monitoring grafana -o jsonpath="{.data.admin-password}" | base64 --decode ; echo

2. The Grafana server can be accessed via port 80 on the following DNS name from within your cluster:

   grafana.monitoring.svc.cluster.local

   Get the Grafana URL to visit by running these commands in the same shell:

     - export POD_NAME=$(kubectl get pods --namespace monitoring -l "app.kubernetes.io/name=grafana,app.kubernetes.io/instance=grafana" -o jsonpath="{.items[0].metadata.name}")
     - kubectl --namespace monitoring port-forward $POD_NAME 3000

3. Login with the password from step 1 and the username: admin

prometheus helm chart output
----------------------------

```
NAME: prometheus
LAST DEPLOYED: Sat Jul 31 07:56:14 2021
NAMESPACE: monitoring
STATUS: deployed
REVISION: 2
TEST SUITE: None
```
NOTES:

The Prometheus server can be accessed via port 80 on the following DNS name from within your cluster:
  - prometheus-server.monitoring.svc.cluster.local


Get the Prometheus server URL by running these commands in the same shell:
  - export POD_NAME=$(kubectl get pods --namespace monitoring -l "app=prometheus,component=server" -o jsonpath="{.items[0].metadata.name}")
  - kubectl --namespace monitoring port-forward $POD_NAME 9090


The Prometheus alertmanager can be accessed via port 80 on the following DNS name from within your cluster:
  - prometheus-alertmanager.monitoring.svc.cluster.local


Get the Alertmanager URL by running these commands in the same shell:
  - export POD_NAME=$(kubectl get pods --namespace monitoring -l "app=prometheus,component=alertmanager" -o jsonpath="{.items[0].metadata.name}")
  - kubectl --namespace monitoring port-forward $POD_NAME 9093
  ```
    #################################################################################
    ######   WARNING: Pod Security Policy has been moved to a global property.  #####
    ######            use .Values.podSecurityPolicy.enabled with pod-based      #####
    ######            annotations                                               #####
    ######            (e.g. .Values.nodeExporter.podSecurityPolicy.annotations) #####
    #################################################################################
```

The Prometheus PushGateway can be accessed via port 9091 on the following DNS name from within your cluster:
  - prometheus-pushgateway.monitoring.svc.cluster.local


Get the PushGateway URL by running these commands in the same shell:
  - export POD_NAME=$(kubectl get pods --namespace monitoring -l "app=prometheus,component=pushgateway" -o jsonpath="{.items[0].metadata.name}")
  - kubectl --namespace monitoring port-forward $POD_NAME 9091

For more information on running Prometheus, visit:
  - https://prometheus.io/



Configure
=========
See config docs here
 - https://prometheus.io/docs/prometheus/latest/configuration/configuration/

This is an example configuration for dashboard 315
 - https://grafana.com/grafana/dashboards/315

For this exercise we need to add in scrape_configs: job_name: kubernetes-nodes-cadvisor
listed in the above link. This needs to be merged with the existing set of
scrape_configs. So first get the current yml config file from the server pod

``` bash
export POD_NAME=$(kubectl get pods --namespace monitoring -l "app=prometheus,component=server" -o jsonpath="{.items[0].metadata.name}")

kubectl cp $POD_NAME:/etc/config/..data/prometheus.yml prometheus.yml
```
extract the scrape_configs section from the yaml and merge in kubernetes-nodes-cadvisor.
update the prometheus.values.yaml serverFiles section accordingly.
(in this repo the kubernetes-nodes-cadvisor is already merged in).

NOTE: there is already a kubernetes-nodes-cadvisor and you need to overwrite
the existing one.

redeploy with ``./deploy-monitoring.sh``

There is probably a better way to get the default config and maybe there is a
way to do the merge automatically so you only supply kubernetes-nodes-cadvisor
definition.  (TODO for refining in the future).

Add the Dashboard
-----------------

Go to grafana at http:/gknuc.local/grafana/ go to settings and create a
data source, choose Prometheus and give it the URL of http://prometheus-server.monitoring.svc.cluster.local.

Go to Dashboards / Manage and choose import. Provide the dash number 315.

