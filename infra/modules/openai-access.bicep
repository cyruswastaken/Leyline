// Lets the app's managed identity call the models, instead of a key.
// Adds one role assignment to the Azure OpenAI account that already exists in rg-leyline
// (the same group the model deployments live in); nothing else on the account changes.
param accountName string
param identityPrincipalId string

// Built-in role: Cognitive Services OpenAI User (use the models, not manage the account).
var openAiUserId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: accountName
}

resource useModels 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, identityPrincipalId, openAiUserId)
  scope: account
  properties: {
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserId)
  }
}
