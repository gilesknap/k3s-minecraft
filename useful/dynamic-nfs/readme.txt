To make the minecraft servers robust and able to switch nodes,
I need to use a network filesystem for their data folders.

I'm going to use my nfs server and create dynamic Peristent Volume Provisioning.

Details obtained here:
https://medium.com/@myte/kubernetes-nfs-and-dynamic-nfs-provisioning-97e2afb8b4a9
https://www.ibm.com/support/pages/how-do-i-create-storage-class-nfs-dynamic-storage-provisioning-openshift-environment

The yaml in this folder is hard coded for my nfs server.
TODO: generalize

For an example if using the Dynamic Provisioning see ../giles-servers/fire-ice2.yaml