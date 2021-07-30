Ark Survival Evolved Game Server
================================

This is not yet working because it does not seem to create the
Storage - my nfs-storageclass provisioner does not seem to fire.

https://artifacthub.io/packages/helm/ark-server-charts/ark-cluster

helm repo add ark https://drpsychick.github.io/ark-server-charts
helm repo update
helm search repo ark
helm upgrade --create-namespace --namespace ark --install --values values.yaml arkcluster ark/ark-cluster

