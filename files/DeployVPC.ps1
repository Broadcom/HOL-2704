Write-Host "1. Import Module & Connect to vCenter" 
Import-Module VMware.VimAutomation.Vpc 
Connect-VIServer -Server vc-wld01-a.site-a.vcf.lab -Protocol https | Out-Null 

Write-Host "2a. Create Corp VPC" 
New-Vpc -Name Corp-VPC | Out-Null 
Write-Host "2b. Create Shared Services VPC" 
New-Vpc -Name Shared-SVC-VPC -PrivateIp 192.168.2.0/24 | Out-Null 

Write-Host "3a. Create Corp VPC public subnet" 
New-VpcSubnet -Vpc Corp-VPC -Name Web-Subnet-Public -DhcpMode Server -AccessMode public -IpV4Size 16 -GatewayConnectivity| Out-Null 
Write-Host "3b. Create Shared Services VPC subnets: TGW and private" 
New-VpcSubnet -Vpc Shared-SVC-VPC -Name App-Subnet-TGW -DhcpMode Server -AccessMode privatetgw -IpV4Size 16 -GatewayConnectivity | Out-Null 
New-VpcSubnet -Vpc Shared-SVC-VPC -Name DB-Subnet-Private -DhcpMode Server -AccessMode private -IpV4Size 16 -GatewayConnectivity | Out-Null 
Start-Sleep 2 

Write-Host "4. Connect VMs to new VPC subnets" 
Get-NetworkAdapter -VM web-01a -Name "Network adapter 1" | Set-NetworkAdapter -Subnet Web-Subnet-Public -Confirm:$false | Out-Null 
Get-NetworkAdapter -VM app-01a -Name "Network adapter 1" | Set-NetworkAdapter -Subnet App-Subnet-TGW -Confirm:$false | Out-Null 
Get-NetworkAdapter -VM db-01a -Name "Network adapter 1" | Set-NetworkAdapter -Subnet DB-Subnet-Private -Confirm:$false | Out-Null 

Write-Host "5.Restart Web VM" 
Get-VM -Name web-01a | Restart-VM -Confirm:$false | Out-Null 

Write-Host "6.Create External-IP for the App-01a vm" 
Get-NetworkAdapter -VM app-01a -Name "Network adapter 1" | Set-NetworkAdapter -Subnet App-Subnet-TGW -AutoAssignExternalIp -Confirm:$false | Out-Null
