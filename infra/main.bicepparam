using 'main.bicep'

param prefix              = readEnvironmentVariable('CES_PREFIX', 'ces')
param location            = readEnvironmentVariable('AZURE_LOCATION', 'westus3')
param openAiLocation      = readEnvironmentVariable('AZURE_OPENAI_LOCATION', 'westus3')
param openAiModel         = readEnvironmentVariable('AZURE_OPENAI_MODEL', 'gpt-5.6-luna')
param openAiModelVersion  = readEnvironmentVariable('AZURE_OPENAI_MODEL_VERSION', '2026-07-09')
param openAiModelCapacity = int(readEnvironmentVariable('AZURE_OPENAI_CAPACITY', '250'))
param principalId         = readEnvironmentVariable('AZURE_PRINCIPAL_ID', '')

// Leave empty on the first deploy: the orchestrator app provisions with a placeholder image, and
// `azd deploy` pushes the real one. prepare_images.py builds the engine disk images after provisioning.
param orchestratorImage   = readEnvironmentVariable('ORCHESTRATOR_IMAGE', '')
