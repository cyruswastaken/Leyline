// Model deployments on the Azure OpenAI account that already exists in rg-leyline.
// Only deployments are added; the account itself is left as it is. The key is read in a
// separate module afterwards (openai-key.bicep): asking for it while deployments are being
// written makes Azure answer RequestConflict.
param accountName string
param chatModel string
param chatModelVersion string
param chatCapacity int
param embeddingModel string
param embeddingModelVersion string
param embeddingCapacity int

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: accountName
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: chatModel
  sku: {
    name: 'GlobalStandard'
    capacity: chatCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModel
      version: chatModelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

// Deployments on one account are created one after another; in parallel Azure answers
// with a conflict.
resource embeddings 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: embeddingModel
  sku: {
    name: 'GlobalStandard'
    capacity: embeddingCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModel
      version: embeddingModelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [chat]
}

output endpoint string = account.properties.endpoint
output chatDeployment string = chat.name
output embeddingDeployment string = embeddings.name
