// DudeWheresMyLogs verification lab
//
// Deploys a deliberately misconfigured set of cheap resources so every
// finding category the tool claims to detect has a known-expected instance.
// Total cost is negligible: workspaces bill per GB ingested (near zero here),
// Key Vaults and NSGs are free, the storage account is pennies.
//
// After deployment, run seed.sh: it deletes the "doomed" workspace so the
// diagnostic setting pointing at it becomes a dead destination.
//
// Expected findings:
//   kvmiss*   -> Missing Diagnostics
//   nsg-dwml-missing -> Missing Diagnostics
//   kvdup*    -> Duplicate Shipping (two different workspaces)
//   kvdead*   -> Dead Destinations (after seed.sh)
//   kvcross*  -> Cross-Region Shipping (eastus resource -> westus2 workspace)
//   kvok*     -> Healthy (two settings to the SAME workspace with different
//                categories -- negative control for duplicate detection)
//   stdwml*   -> blob sub-service Enabled; queue/table/file sub-services Missing

param location string = 'eastus'
param crossRegionLocation string = 'westus2'

var suffix = uniqueString(resourceGroup().id)

// --- Destination workspaces ---

resource lawPrimary 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-dwml-primary'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource lawSecondary 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-dwml-secondary'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource lawWest 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-dwml-west'
  location: crossRegionLocation
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// Deleted by seed.sh after deployment to create the dead-destination finding
resource lawDoomed 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-dwml-doomed'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// --- Source resources ---

var kvProperties = {
  sku: { family: 'A', name: 'standard' }
  tenantId: subscription().tenantId
  enableRbacAuthorization: true
}

resource kvMissing 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kvmiss${suffix}'
  location: location
  properties: kvProperties
}

resource kvDup 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kvdup${suffix}'
  location: location
  properties: kvProperties
}

resource kvDead 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kvdead${suffix}'
  location: location
  properties: kvProperties
}

resource kvCross 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kvcross${suffix}'
  location: location
  properties: kvProperties
}

resource kvOk 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kvok${suffix}'
  location: location
  properties: kvProperties
}

resource nsgMissing 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: 'nsg-dwml-missing'
  location: location
  properties: {}
}

resource stg 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stdwml${suffix}'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource stgBlob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: stg
  name: 'default'
}

// --- Diagnostic settings (the deliberate misconfigurations) ---

var kvAuditLogs = [
  { category: 'AuditEvent', enabled: true }
]

// Duplicate: same destination type, two DIFFERENT workspaces
resource dupToPrimary 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: kvDup
  name: 'ship-to-primary'
  properties: { workspaceId: lawPrimary.id, logs: kvAuditLogs }
}

resource dupToSecondary 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: kvDup
  name: 'ship-to-secondary'
  properties: { workspaceId: lawSecondary.id, logs: kvAuditLogs }
}

// Storage destination on the duplicate vault: exercises the platform
// export fee flag (Storage/Event Hub destinations, billed since June 2026)
resource dupToStorage 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: kvDup
  name: 'ship-to-storage'
  properties: { storageAccountId: stg.id, logs: kvAuditLogs }
}

// Dead destination (after seed.sh deletes lawDoomed)
resource deadShip 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: kvDead
  name: 'ship-to-doomed'
  properties: { workspaceId: lawDoomed.id, logs: kvAuditLogs }
}

// Cross-region: eastus vault -> westus2 workspace
resource crossShip 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: kvCross
  name: 'ship-to-west'
  properties: { workspaceId: lawWest.id, logs: kvAuditLogs }
}

// Healthy negative control: two settings to the SAME workspace, split by
// category. Azure allows this (categories don't overlap) and the tool must
// NOT flag it as duplicate shipping.
resource okAudit 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: kvOk
  name: 'ship-audit'
  properties: { workspaceId: lawPrimary.id, logs: kvAuditLogs }
}

resource okPolicy 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: kvOk
  name: 'ship-policy'
  properties: {
    workspaceId: lawPrimary.id
    logs: [
      { category: 'AzurePolicyEvaluationDetails', enabled: true }
    ]
  }
}

// Workspace query auditing (v2.2 expectations):
//   primary: Audit enabled -> "Active" once queries land, "Unqueried" before
//   secondary: Audit enabled, never queried -> Unqueried Workspaces finding
//   west: no auditing -> No Query Auditing finding
resource primaryAudit 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: lawPrimary
  name: 'audit-self'
  properties: {
    workspaceId: lawPrimary.id
    logs: [
      { category: 'Audit', enabled: true }
    ]
  }
}

resource secondaryAudit 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: lawSecondary
  name: 'audit-to-primary'
  properties: {
    workspaceId: lawPrimary.id
    logs: [
      { category: 'Audit', enabled: true }
    ]
  }
}

// Storage blob sub-service enabled; queue/table/file left missing
resource blobShip 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: stgBlob
  name: 'blob-to-primary'
  properties: {
    workspaceId: lawPrimary.id
    logs: [
      { category: 'StorageRead', enabled: true }
      { category: 'StorageWrite', enabled: true }
      { category: 'StorageDelete', enabled: true }
    ]
  }
}

output doomedWorkspaceName string = lawDoomed.name
output suffix string = suffix
