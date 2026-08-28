#!/usr/bin/bash
#
# This script, if present in your vpodrepo root, is run at the end of labsartup.
# It is called during the "final.py"
# Here's the overall flow:
# prelim.py -> ESXi.py -> VCF.py -> VVF.py -> vSphere.py -> pings.py -> services.py -> Kubernetes.py -> urls.py -> VCFfinal.py -> final.py -> odyssey.py 
#
# If you prefer scripting in Python:
# You may optionally place a "lab-update.py" in this folder and it would be called immediately folling the call of this lab-update.sh script
# 
# Source the .bashrc file for settings/paths/etc...
. /home/holuser/.bashrc
# Insert your custom code here:
if [ -f /lmchol/home/holuser/Documents/files/addoninstall-istio.yaml ]; then
    mv -f /lmchol/home/holuser/Documents/files/addoninstall-istio.yaml /lmchol/home/holuser/Documents/addoninstall-istio.yaml
fi

if [ -f /lmchol/home/holuser/Documents/files/vcf-inspector-linux-amd64 ]; then
    chmod +x /lmchol/home/holuser/Documents/files/vcf-inspector-linux-amd64
fi

# Example to echo text into file on Console VM. 
# NOTE: when this script runs, /lmchol is mounted to the "/" of the Console VM
# echo "Functional Testing!" > /lmchol/home/holuser/Documents/FT.txt
