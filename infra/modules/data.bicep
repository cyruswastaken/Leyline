// Data services: PostgreSQL (with pgvector) and Azure Managed Redis.
//
// Both are reachable over the public internet, protected by TLS and a password or key.
// That is a deliberate demo-tier choice (private networking adds subnets, private
// endpoints and cost). Postgres only accepts connections from Azure services plus
// whatever firewall rules you add; docs/DEPLOYMENT.md lists the production hardening.
param location string
param suffix string
param tags object

@secure()
param postgresAdminPassword string

var postgresName = 'leyline-pg-${suffix}'
var redisName = 'leyline-redis-${suffix}'

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresName
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: 'leyline'
    administratorLoginPassword: postgresAdminPassword
    storage: {
      storageSizeGB: 32
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: 'leyline'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// The first migration runs CREATE EXTENSION vector, which Azure only allows for
// extensions on this allow-list.
resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    value: 'VECTOR'
    source: 'user-override'
  }
  dependsOn: [database]
}

// 0.0.0.0 - 0.0.0.0 is Azure's name for "other Azure services" (the Container Apps).
resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
  dependsOn: [extensions]
}

resource redis 'Microsoft.Cache/redisEnterprise@2025-04-01' = {
  name: redisName
  location: location
  tags: tags
  sku: { name: 'Balanced_B0' }
  properties: { minimumTlsVersion: '1.2' }
}

// Sessions and rate limits are short-lived, so nothing is persisted. EnterpriseCluster
// keeps the endpoint a plain single-node address that ordinary Redis clients understand.
resource redisDb 'Microsoft.Cache/redisEnterprise/databases@2025-04-01' = {
  parent: redis
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'
    port: 10000
    clusteringPolicy: 'EnterpriseCluster'
    evictionPolicy: 'VolatileLRU'
    accessKeysAuthentication: 'Enabled'
  }
}

output postgresHost string = postgres.properties.fullyQualifiedDomainName
output redisName string = redis.name
