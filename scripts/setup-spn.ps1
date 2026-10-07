<#
.SYNOPSIS
  Creates a service principal that can read/scale/resume ONE Fabric capacity.
.EXAMPLE
  ./scripts/setup-spn.ps1 -SubscriptionId <sub> -ResourceGroup rg-fabric -CapacityName mycapacity
#>
param(
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $CapacityName,
    [string] $SpnName = "sp-fabric-capacity-autoscaler"
)
$ErrorActionPreference = "Stop"

az account set --subscription $SubscriptionId
$scope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Fabric/capacities/$CapacityName"

# Confirm the capacity exists before creating anything.
az resource show --ids $scope --query "{name:name, sku:sku.name, state:properties.state}" -o table

# Contributor scoped to the single capacity resource (read + PATCH sku + resume/suspend).
$sp = az ad sp create-for-rbac --name $SpnName --role "Contributor" --scopes $scope -o json | ConvertFrom-Json

Write-Host ""
Write-Host "Store these values (put the secret in Azure Key Vault / Activator parameters, never in git):" -ForegroundColor Yellow
Write-Host "  subscription_id : $SubscriptionId"
Write-Host "  resource_group  : $ResourceGroup"
Write-Host "  capacity_name   : $CapacityName"
Write-Host "  tenant_id       : $($sp.tenant)"
Write-Host "  client_id       : $($sp.appId)"
Write-Host "  client_secret   : $($sp.password)"
