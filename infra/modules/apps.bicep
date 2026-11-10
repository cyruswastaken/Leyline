// Container Apps: the environment (always created, so its address is known before the
// sign-in redirect is registered), then the web app, the ingestion worker and a manual
// task job (migrations and corpus seeding) when deployApps is true. All three run the
// same image.
param location string
param tags object
param deployApps bool
param appImage string
param logAnalyticsName string
param identityId string
param registryServer string
param keyVaultUri string
param openAiEndpoint string
param chatDeployment string
param embeddingDeployment string
param oidcClientId string
param oidcMetadataUrl string
param webAlwaysOn bool

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-leyline'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
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

var identity = {
  type: 'UserAssigned'
  userAssignedIdentities: { '${identityId}': {} }
}

var registries = [
  {
    server: registryServer
    identity: identityId
  }
]

// Key Vault references: the platform pulls each value with the managed identity.
var secretNames = [
  { name: 'database-url', vault: 'database-url' }
  { name: 'redis-url', vault: 'redis-url' }
  { name: 'blob-connection-string', vault: 'blob-connection-string' }
  { name: 'openai-api-key', vault: 'openai-api-key' }
  { name: 'oidc-client-secret', vault: 'oidc-client-secret' }
]
var secrets = [
  for s in secretNames: {
    name: s.name
    keyVaultUrl: '${keyVaultUri}secrets/${s.vault}'
    identity: identityId
  }
]

var llmBaseUrl = '${openAiEndpoint}openai/v1/'

var commonEnv = [
  { name: 'LEYLINE_ENV', value: 'production' }
  { name: 'DATABASE_URL', secretRef: 'database-url' }
  { name: 'BLOB_CONNECTION_STRING', secretRef: 'blob-connection-string' }
  { name: 'REDIS_URL', secretRef: 'redis-url' }
  { name: 'LLM_BASE_URL', value: llmBaseUrl }
  { name: 'LLM_MODEL', value: chatDeployment }
  { name: 'LLM_API_KEY', secretRef: 'openai-api-key' }
  // The GPT-5 family only accepts its default temperature, and reasoning takes longer.
  { name: 'LLM_SEND_TEMPERATURE', value: 'false' }
  { name: 'LLM_TIMEOUT_SECONDS', value: '55' }
  { name: 'EMBEDDING_BASE_URL', value: llmBaseUrl }
  { name: 'EMBEDDING_MODEL', value: embeddingDeployment }
  { name: 'EMBEDDING_API_KEY', secretRef: 'openai-api-key' }
  // text-embedding-3 can return the schema's fixed 768 dimensions directly.
  { name: 'EMBEDDING_SEND_DIMENSIONS', value: 'true' }
  { name: 'OIDC_METADATA_URL', value: oidcMetadataUrl }
  { name: 'OIDC_CLIENT_ID', value: oidcClientId }
  { name: 'OIDC_CLIENT_SECRET', secretRef: 'oidc-client-secret' }
]

var webEnv = concat(commonEnv, [
  { name: 'SESSION_COOKIE_SECURE', value: 'true' }
  // Container Apps ingress is the one proxy in front of the app.
  { name: 'TRUSTED_PROXY_COUNT', value: '1' }
])

resource web 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: 'leyline-web'
  location: location
  tags: tags
  identity: identity
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: registries
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'web'
          image: appImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: webEnv
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/readyz', port: 8000 }
              periodSeconds: 15
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: webAlwaysOn ? 1 : 0
        maxReplicas: 2
        rules: [
          {
            name: 'http'
            http: { metadata: { concurrentRequests: '30' } }
          }
        ]
      }
    }
  }
}

// The worker has no ingress. Container Apps cannot run an exec probe, so the heartbeat
// file check used by compose is not available here; the platform restarts the container
// if the process exits.
resource worker 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: 'leyline-worker'
  location: location
  tags: tags
  identity: identity
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registries
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: appImage
          command: ['python', '-m', 'leyline.worker']
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: commonEnv
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

// Started by hand: `az containerapp job start ... --command python --args "-m leyline.db upgrade"`.
resource tasks 'Microsoft.App/jobs@2024-03-01' = if (deployApps) {
  name: 'leyline-task'
  location: location
  tags: tags
  identity: identity
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: registries
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'task'
          image: appImage
          command: ['python', '-m', 'leyline.db', 'upgrade']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: commonEnv
        }
      ]
    }
  }
}

output defaultDomain string = environment.properties.defaultDomain
output webUrl string = deployApps ? 'https://${web!.properties.configuration.ingress.fqdn}' : ''
