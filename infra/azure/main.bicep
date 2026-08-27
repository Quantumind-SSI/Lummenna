// Lummenna demo platform on Azure. One deployment plane, no second cloud.
//
// Provisions the full public footprint:
//   * Container Apps environment + the Lumena API container app
//   * Static Web App for the marketing site
//   * ACR for the image supply chain
//   * Storage account: blob container for database backups, file share for the
//     ONNX embedding model cache
//   * Log Analytics + Application Insights
//   * A user-assigned identity with exactly two grants: pull images, write backups
//
//   az deployment group create -g rg-lumena-leaderboard -f main.bicep \
//     -p namePrefix=lumena apiKey=<key>
//
// Cloudflare sits in front of this as an edge policy layer only: DNS, WAF,
// caching, TLS. It hosts nothing and deploys nothing. Every artefact that
// reaches production passes through this template.
//
// Deliberately NOT provisioned, and why:
//
//   Multi-replica scale-out. The API is a single-writer SQLite process. Two
//   replicas against one database is corruption, not availability, so
//   maxReplicas is pinned to 1. Raising it requires replacing SQLite first.
//
//   Persistent volume for the database. SQLite runs in WAL mode
//   (lummenna/data/schema.py), and WAL requires shared memory that no network
//   filesystem provides: https://sqlite.org/wal.html section 2.2. Azure Files
//   is SMB/NFS, so mounting the store there would silently drop the database
//   to rollback-journal mode. The store therefore lives on ephemeral local
//   disk, where WAL is valid, and durability comes from scheduled backups to
//   blob. This is a demo/leaderboard surface; that tradeoff is deliberate and
//   is documented rather than hidden.
//
//   Zone redundancy. Consumption workload profile, single region. A zone
//   failure is a demo outage, which is acceptable for this workload and is
//   cheaper than pretending otherwise.

@description('Prefix for all resource names (3-12 lowercase alphanumerics).')
@minLength(3)
@maxLength(12)
param namePrefix string = 'lumena'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Container image to run, including registry and tag. Never "latest": deploys are pinned to an immutable tag so a rollback is a redeploy of a known digest.')
param containerImage string

@description('API key required by the Lumena API. Stored as a Container Apps secret, never as a plaintext environment variable.')
@secure()
param apiKey string

@description('Log Analytics retention in days.')
@minValue(30)
@maxValue(730)
param logRetentionDays int = 30

@description('Port the API listens on inside the container.')
param apiPort int = 8848

// Name derivation keeps take() inside the interpolation rather than around it.
// uniqueString always contributes 13 characters, so the compiler can prove a
// non-zero minimum length and the resource name constraints are satisfied
// statically. Wrapping the whole expression in take() erases that proof and
// trips BCP334.
//   storage: <=11 + 13 = 24 chars max, within the 24-char account-name limit
//   acr:      <=12 + 3 + 13 = 28 chars max, within the 50-char registry limit
var storageName = '${take(namePrefix, 11)}${uniqueString(resourceGroup().id)}'
var acrName = '${namePrefix}acr${uniqueString(resourceGroup().id)}'
var modelShareName = 'models'
var backupContainerName = 'db-backups'

// ---------------------------------------------------------------------------
// Observability
// ---------------------------------------------------------------------------

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
  }
}

// ---------------------------------------------------------------------------
// Identity and image supply chain
// ---------------------------------------------------------------------------

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id'
  location: location
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false // pull via managed identity only
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, 'acrpull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Storage: model cache (file share) + database backups (blob)
// ---------------------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
  }
}

resource fileServices 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

// Read-mostly ONNX model cache. Safe on SMB precisely because it holds no
// SQLite database: no locking or shared-memory requirements apply.
resource modelShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileServices
  name: modelShareName
  properties: {
    shareQuota: 16
  }
}

resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 14
    }
  }
}

resource backupContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobServices
  name: backupContainerName
  properties: {
    publicAccess: 'None'
  }
}

// The API writes its own database backups. Data Contributor on the storage
// account is the narrowest built-in role that permits blob writes.
resource blobWrite 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, 'blobcontributor')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'ba92f5b4-2d11-453d-a403-e96b0029c9fe' // Storage Blob Data Contributor
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Container Apps
// ---------------------------------------------------------------------------

resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource modelStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: containerEnv
  name: 'models'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: modelShareName
      accessMode: 'ReadWrite'
    }
  }
}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-api'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: apiPort
        transport: 'Auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'api-key'
          value: apiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: '${namePrefix}-api'
          image: containerImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          // The prefix MUST be LUMMENNA_ (double M). LummennaConfig declares
          // env_prefix='LUMMENNA_' (lummenna/config.py). The image deployed by
          // hand on 2026-08-18 predates commit f51d6a4 (2026-08-22) which
          // renamed the prefix, and was configured with single-M names. Those
          // two errors cancelled out. With a current image, a single-M name is
          // silently ignored and the field falls back to its default, which for
          // api_key is None. server.py:144 guards auth with
          // `if _config.api_key and ...`, so an ignored key disables
          // authentication outright rather than failing closed. Do not "tidy"
          // these names.
          env: [
            { name: 'LUMMENNA_API_HOST', value: '0.0.0.0' }
            { name: 'LUMMENNA_API_PORT', value: string(apiPort) }
            // Ephemeral local disk, not the Azure Files mount. SQLite WAL
            // requires shared memory and is invalid on any network filesystem.
            { name: 'LUMMENNA_STORE_PATH', value: '/data/store' }
            { name: 'LUMMENNA_MODEL_PATH', value: '/data/models' }
            // Deliberately false. This is a hosted, internet-facing demo whose
            // durability depends on replicating SQLite to Azure Blob, and
            // LummennaConfig.assert_sovereign() refuses every outbound network
            // call while sovereign is true. Claiming sovereignty here while
            // shipping bytes to blob storage would be a false claim in the
            // sense AGENTS.md Rule 2 prohibits. Local and self-hosted installs
            // keep the sovereign default of true and get no backup.
            { name: 'LUMMENNA_SOVEREIGN', value: 'false' }
            { name: 'LUMMENNA_BACKUP_ENABLED', value: 'true' }
            { name: 'LUMMENNA_BACKUP_ACCOUNT', value: storage.name }
            { name: 'LUMMENNA_BACKUP_CONTAINER', value: backupContainerName }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'LUMMENNA_API_KEY', secretRef: 'api-key' }
          ]
          volumeMounts: [
            { volumeName: 'store', mountPath: '/data/store' }
            { volumeName: 'models', mountPath: '/data/models' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health', port: apiPort }
              initialDelaySeconds: 10
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/ready', port: apiPort }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      volumes: [
        // EmptyDir is node-local block storage: a real filesystem where WAL is
        // valid. Contents do not survive a revision change or restart, which is
        // why the API replicates to blob on a schedule.
        {
          name: 'store'
          storageType: 'EmptyDir'
        }
        {
          name: 'models'
          storageType: 'AzureFile'
          storageName: modelStorage.name
        }
      ]
      scale: {
        // Pinned to exactly one replica. See the header note on single-writer
        // SQLite; this is a correctness constraint, not a cost decision.
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Static Web App: the marketing site
// ---------------------------------------------------------------------------

resource site 'Microsoft.Web/staticSites@2023-01-01' = {
  name: '${namePrefix}-site'
  location: location
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    // Deployment is driven by GitHub Actions against this resource rather than
    // by the portal's repository integration, so the workflow in this repo
    // stays the single source of deployment truth.
    allowConfigFileUpdates: true
    stagingEnvironmentPolicy: 'Enabled'
  }
}

output acrLoginServer string = acr.properties.loginServer
output apiFqdn string = api.properties.configuration.ingress.fqdn
output siteDefaultHostname string = site.properties.defaultHostname
output identityClientId string = identity.properties.clientId
output storageAccountName string = storage.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
