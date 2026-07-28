#!/bin/bash

#This script will execute all final bash commands needed for the vPod to be ready
#The PW variable requires the file listed to be present as it is on the manager VM.
#This script will not work on the LMC

#Copy files from Git repo on Manager to be used on Console in HOL environment
vPodPW=$(</home/holuser/creds.txt) 
#Script to build VPC environment for Lab2 Module1
sshpass -p $vPodPW scp -o StrictHostKeyChecking=no /vpodrepo/2026-labs/2640/lab-startup/DeployVPC.ps1 holuser@10.1.10.130:/home/holuser/labfiles/hol-2640-02/DeployVPC.ps1
#Script to remove Distributed Connectivity configured in Lab2 Module2 in preparation for Module3 Centralized Connectivity.
sshpass -p $vPodPW scp -o StrictHostKeyChecking=no /vpodrepo/2026-labs/2640/lab-startup/Disconnect-TGW.ps1 holuser@10.1.10.130:/home/holuser/labfiles/hol-2640-02/Disconnect-TGW.ps1
