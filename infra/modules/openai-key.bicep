// Reads the Azure OpenAI key in its own step. Asking for the key while the model
// deployments are still being written makes Azure answer RequestConflict (the account
// is busy), so main.bicep runs this module only after the deployments are finished.
// The key leaves as a secure output and goes straight into Key Vault (secrets.bicep).
param accountName string

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: accountName
}

@secure()
output apiKey string = account.listKeys().key1
