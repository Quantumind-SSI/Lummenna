// Lumena on AKS — minimal, honest Azure footprint.
//
// Provisions: AKS (1 system pool), ACR (image supply chain), Log Analytics +
// Application Insights (observability), and the AcrPull grant so the cluster
// can pull images. Scope: resource group.
//
//   az group create -n rg-lummenna -l westeurope
//   az deployment group create -g rg-lummenna -f main.bicep -p namePrefix=lummenna
//
// Deliberately NOT provisioned: multi-zone HA, autoscaling beyond the node
// pool, geo-replication — Lumena is a single-writer, single-replica workload
// and pretending otherwise in infrastructure would be dishonest.

@description('Prefix for all resource names (3-12 lowercase alphanumerics).')
@minLength(3)
@maxLength(12)
param namePrefix string = 'lummenna'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('AKS node VM size. Lumena + ONNX inference fits comfortably in 4 vCPU / 16 GiB.')
param nodeVmSize string = 'Standard_D4s_v5'

@description('Node count for the system pool. Lumena itself uses one pod; extra nodes are for the rest of your workloads.')
@minValue(1)
@maxValue(10)
param nodeCount int = 2

@description('Kubernetes version. Empty = AKS default for the region.')
param kubernetesVersion string = ''

@description('Log Analytics retention in days.')
@minValue(30)
@maxValue(730)
param logRetentionDays int = 30

var acrName = replace('${namePrefix}acr${uniqueString(resourceGroup().id)}', '-', '')

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: logRetentionDays
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-appinsights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    // Connection string feeds APPLICATIONINSIGHTS_CONNECTION_STRING; Lumena
    // only exports telemetry when LUMENA_SOVEREIGN=false (fail-loud gate).
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: take(acrName, 50)
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false // pull via managed identity only
  }
}

resource aks 'Microsoft.ContainerService/managedClusters@2024-05-01' = {
  name: '${namePrefix}-aks'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    dnsPrefix: '${namePrefix}-aks'
    kubernetesVersion: empty(kubernetesVersion) ? null : kubernetesVersion
    enableRBAC: true
    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        count: nodeCount
        vmSize: nodeVmSize
        osType: 'Linux'
        osSKU: 'AzureLinux'
        type: 'VirtualMachineScaleSets'
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPolicy: 'azure' // NetworkPolicy in the Helm chart depends on this
      loadBalancerSku: 'standard'
    }
    addonProfiles: {
      omsagent: {
        enabled: true
        config: {
          logAnalyticsWorkspaceResourceID: logAnalytics.id
        }
      }
    }
    oidcIssuerProfile: { enabled: true }       // for GitHub Actions OIDC federation
    securityProfile: {
      workloadIdentity: { enabled: true }
    }
  }
}

// Grant the AKS kubelet identity pull access on ACR.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, aks.id, 'acrpull')
  scope: acr
  properties: {
    // AcrPull built-in role
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
    principalId: aks.properties.identityProfile.kubeletidentity.objectId
    principalType: 'ServicePrincipal'
  }
}

output acrLoginServer string = acr.properties.loginServer
output aksClusterName string = aks.name
output aksOidcIssuer string = aks.properties.oidcIssuerProfile.issuerURL
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output logAnalyticsWorkspaceId string = logAnalytics.id
