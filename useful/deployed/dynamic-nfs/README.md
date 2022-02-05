# Auto provisioning NFS Persistent Volume Claims
To make the minecraft servers robust and able to switch nodes,
I need to use a network filesystem for their data folders.

I'm going to use my nfs server and create dynamic Peristent Volume Provisioning.

Details obtained here:

- https://medium.com/@myte/kubernetes-nfs-and-dynamic-nfs-provisioning-97e2afb8b4a9
- https://www.ibm.com/support/pages/how-do-i-create-storage-class-nfs-dynamic-storage-provisioning-openshift-environment

The yaml in this folder is hard coded for my nfs server.

see the script ``deploy-dnfs.sh`` for details

TODO: generalize

For an example if using the Dynamic Provisioning see ../giles-servers/fire-ice2.yaml

# Permanent Persistent Volumes
I have switched to using PVs because I want the data to survive a helm delete 
and re-deploy. More importantly I need the data to survive tearing down the 
cluster and rebuilding from scratch (while I experiment with IaC).

I now have a NAS which provides some storage that is totally outside of the 
lifecycle of the cluster and for each PVC I predefine a PV that maps to a 
folder in this NAS. I have set these folders to be owned by UID 1022.

For the minecraft servers see example PV/PVC definitions in the folder
giles-servers/pvcs e.g. [science lab](../../../giles-servers/pvcs/science-lab-pv.yaml)

To refer to the existing PVC in the minecraft helm values, modify the 
persistence section as follows:
```yaml
securityContext:
  runAsUser: 1022
  fsGroup: 1022

# always reuse an existing nfs mount so the world survives chart deletion
persistence:
  dataDir:
    enabled: true
    existingClaim: science-lab-minecraft-pvc
```