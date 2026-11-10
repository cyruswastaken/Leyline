// Every secret the app needs goes into Key Vault here, built from the live resources.
// The Container Apps read them through the managed identity (a Key Vault reference), so
// no secret appears in the app's configuration, in a template or in an environment variable
// of the deployment.
param keyVaultName string
param storageAccountName string
param redisName string
param postgresHost string
@secure()
param postgresAdminPassword string
@secure()
param openAiKey string
@secure()
param oidcClientSecret string
param includeOidcSecret bool

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource redis 'Microsoft.Cache/redisEnterprise@2025-04-01' existing = {
  name: redisName
}

resource redisDb 'Microsoft.Cache/redisEnterprise/databases@2025-04-01' existing = {
  parent: redis
  name: 'default'
}

var storageKey = storage.listKeys().keys[0].value
var redisKey = redisDb.listKeys().primaryKey

// ssl_context=true makes SQLAlchemy's pg8000 driver use TLS, which Azure requires.
// The password is URL-encoded so any character is safe in the URL.
resource databaseUrl 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'database-url'
  properties: {
    value: 'postgresql+pg8000://leyline:${uriComponent(postgresAdminPassword)}@${postgresHost}:5432/leyline?ssl_context=true'
  }
}

resource redisUrl 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'redis-url'
  properties: {
    value: 'rediss://:${uriComponent(redisKey)}@${redis.properties.hostName}:10000/0'
  }
}

resource blobConnection 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'blob-connection-string'
  properties: {
    value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccountName};AccountKey=${storageKey};EndpointSuffix=${environment().suffixes.storage}'
  }
}

resource llmKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'openai-api-key'
  properties: { value: openAiKey }
}

resource oidcSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (includeOidcSecret) {
  parent: vault
  name: 'oidc-client-secret'
  properties: { value: oidcClientSecret }
}
