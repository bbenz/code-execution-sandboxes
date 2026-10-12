// ============================================================================
// Code Execution Sandboxes — "Every Loop Needs a Referee"
// ============================================================================
// Provisions everything the referee demo needs:
//   - Azure Container Registry (holds the engine + orchestrator images)
//   - Azure OpenAI (AIServices) with a coding-agent model deployment
//   - Log Analytics + Application Insights (the one distributed trace)
//   - An ACA environment and the orchestrator Container App
//   - A Microsoft.App/sandboxGroups resource with a user-assigned identity
//   - Role assignments for keyless auth throughout
//
// Sandboxes and disk images are NOT declared here: they live behind the sandbox
// group's data plane. scripts/prepare_images.py builds the primed and base disk
// images after provisioning (postprovision hook in azure.yaml).
// ============================================================================

targetScope = 'resourceGroup'

@description('Short name prefix for all resources.')
param prefix string = 'ces'

@description('Azure region for the demo resources (must support ACA Sandboxes).')
param location string = resourceGroup().location

@description('Azure region for the Azure OpenAI account.')
param openAiLocation string = location

@description('Coding-agent model to deploy.')
param openAiModel string = 'gpt-6-luna'

@description('Coding-agent model version.')
param openAiModelVersion string = '2026-09-22'

@minValue(1)
@description('Model deployment capacity (thousands of tokens per minute).')
param openAiModelCapacity int = 250

@description('Orchestrator image. Leave empty on first deploy; azd deploy pushes the real one.')
param orchestratorImage string = ''

@description('azd service tag so `azd deploy orchestrator` targets this app.')
param azdServiceName string = 'orchestrator'

@description('Entra object ID of the local developer, for running the orchestrator or scripts locally.')
param principalId string = ''

@allowed(['Basic', 'Standard', 'Premium'])
param acrSku string = 'Basic'

// ── Names ────────────────────────────────────────────────────────────────────

var suffix               = uniqueString(resourceGroup().id)
var acrName              = replace('${prefix}acr${suffix}', '-', '')
var openAiName           = '${prefix}-openai-${suffix}'
var logAnalyticsName     = '${prefix}-logs-${suffix}'
var appInsightsName      = '${prefix}-appi-${suffix}'
var acaEnvName           = '${prefix}-env'
var orchestratorAppName  = '${prefix}-orchestrator'
var sandboxGroupName     = '${prefix}-sandboxes-${location}'
var orchestratorUamiName = '${prefix}-orchestrator-uami'
var sandboxGroupUamiName = '${prefix}-sandboxgroup-uami'

// Built-in role definition IDs
var acrPullRoleId               = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var openAiUserRoleId            = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
var sandboxGroupDataOwnerRoleId = 'c24cf47c-5077-412d-a19c-45202126392c' // Container Apps SandboxGroup Data Owner

var sandboxGroupResourceId = resourceId('Microsoft.App/sandboxGroups', sandboxGroupName)
var placeholderImage       = 'mcr.microsoft.com/k8se/quickstart:latest'

// ── Managed identities ───────────────────────────────────────────────────────
// User-assigned identities expose principalId at template-eval time, which avoids
// preview-API identity-resolution issues on the sandbox group resource.

resource orchestratorUami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: orchestratorUamiName
  location: location
}

resource sandboxGroupUami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: sandboxGroupUamiName
  location: location
}

// ── Observability ────────────────────────────────────────────────────────────

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
  }
}

// ── Container registry ───────────────────────────────────────────────────────

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: acrSku }
  properties: {
    adminUserEnabled: false
    policies: {
      azureADAuthenticationAsArmPolicy: { status: 'enabled' }
    }
  }
}

// ── Azure OpenAI (the coding agent) ──────────────────────────────────────────

resource openAi 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: openAiName
  location: openAiLocation
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: openAiName
    publicNetworkAccess: 'Enabled'
  }
}

resource openAiDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAi
  name: openAiModel
  sku: {
    name: 'GlobalStandard'
    capacity: openAiModelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: openAiModel
      version: openAiModelVersion
    }
  }
}

// ── ACA environment ──────────────────────────────────────────────────────────

resource acaEnvironment 'Microsoft.App/managedEnvironments@2026-03-02-preview' = {
  name: acaEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ── Sandbox group ────────────────────────────────────────────────────────────
// The management boundary for every referee sandbox. Its user-assigned identity
// pulls the engine image from ACR (keyless), so no registry secret is needed.

resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' = {
  name: sandboxGroupName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${sandboxGroupUami.id}': {}
    }
  }
  properties: {
    imageRegistryCredentials: [
      {
        server: acr.properties.loginServer
        identity: sandboxGroupUami.id
      }
    ]
  }
}

// ── Orchestrator Container App ───────────────────────────────────────────────

resource orchestratorApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: orchestratorAppName
  location: location
  dependsOn: [
    orchestratorAcrPull
    sandboxGroupAcrPull
    orchestratorSandboxDataOwner
  ]
  tags: {
    'azd-service-name': azdServiceName
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${orchestratorUami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: acaEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: orchestratorUami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'orchestrator'
          image: empty(orchestratorImage) ? placeholderImage : orchestratorImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            { name: 'SANDBOX_BACKEND', value: 'aca' }
            { name: 'AZURE_CLIENT_ID', value: orchestratorUami.properties.clientId }
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
            { name: 'AZURE_RESOURCE_GROUP', value: resourceGroup().name }
            { name: 'SANDBOX_GROUP_NAME', value: sandboxGroupName }
            { name: 'SANDBOX_GROUP_REGION', value: location }
            { name: 'SANDBOX_GROUP_IDENTITY_RESOURCE_ID', value: sandboxGroupUami.id }
            { name: 'SANDBOX_GROUP_IDENTITY_CLIENT_ID', value: sandboxGroupUami.properties.clientId }
            { name: 'REFEREE_ENGINE_IMAGE', value: '${acr.properties.loginServer}/referee-engine:latest' }
            { name: 'ACR_LOGIN_SERVER', value: acr.properties.loginServer }
            { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.properties.endpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: openAiDeployment.name }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
            { name: 'OTEL_SERVICE_NAME', value: 'orchestrator' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// ── Role assignments ─────────────────────────────────────────────────────────

// Orchestrator identity can pull its own image from ACR.
resource orchestratorAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, orchestratorUami.id, acrPullRoleId)
  scope: acr
  properties: {
    principalId: orchestratorUami.properties.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalType: 'ServicePrincipal'
  }
}

// Sandbox group identity can pull the engine image from ACR (keyless disk-image builds).
resource sandboxGroupAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, sandboxGroupUami.id, acrPullRoleId)
  scope: acr
  properties: {
    principalId: sandboxGroupUami.properties.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalType: 'ServicePrincipal'
  }
}

// Orchestrator identity can call the coding-agent model.
resource orchestratorOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAi.id, orchestratorUami.id, openAiUserRoleId)
  scope: openAi
  properties: {
    principalId: orchestratorUami.properties.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
    principalType: 'ServicePrincipal'
  }
}

// Orchestrator identity drives the sandbox group's data plane (create/delete sandboxes, disk images).
resource orchestratorSandboxDataOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sandboxGroupResourceId, orchestratorUami.id, sandboxGroupDataOwnerRoleId)
  scope: sandboxGroup
  properties: {
    principalId: orchestratorUami.properties.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sandboxGroupDataOwnerRoleId)
    principalType: 'ServicePrincipal'
  }
}

// Optional local-developer access, so you can run the orchestrator or scripts/prepare_images.py locally.
resource localAcrPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(acr.id, principalId, acrPullRoleId)
  scope: acr
  properties: {
    principalId: principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource localOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(openAi.id, principalId, openAiUserRoleId)
  scope: openAi
  properties: {
    principalId: principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
}

resource localSandboxDataOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(sandboxGroupResourceId, principalId, sandboxGroupDataOwnerRoleId)
  scope: sandboxGroup
  properties: {
    principalId: principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sandboxGroupDataOwnerRoleId)
  }
}

// ── Outputs (consumed by scripts and `azd env get-values`) ───────────────────

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.properties.loginServer
output acrName string = acr.name
output AZURE_OPENAI_ENDPOINT string = openAi.properties.endpoint
output AZURE_OPENAI_DEPLOYMENT string = openAiDeployment.name
output SANDBOX_GROUP_NAME string = sandboxGroup.name
output SANDBOX_GROUP_REGION string = location
output SANDBOX_GROUP_IDENTITY_RESOURCE_ID string = sandboxGroupUami.id
output SANDBOX_GROUP_IDENTITY_CLIENT_ID string = sandboxGroupUami.properties.clientId
output REFEREE_ENGINE_IMAGE string = '${acr.properties.loginServer}/referee-engine:latest'
output orchestratorAppName string = orchestratorApp.name
output orchestratorUrl string = 'https://${orchestratorApp.properties.configuration.ingress.fqdn}'
output APPLICATIONINSIGHTS_CONNECTION_STRING string = appInsights.properties.ConnectionString
output resourceGroupName string = resourceGroup().name
output subscriptionId string = subscription().subscriptionId
