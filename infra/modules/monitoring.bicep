// Alerts for Leyline, built on what the containers already write to Log Analytics and
// on Postgres metrics. No extra service and no extra dependency: the application logs
// one JSON line per security event (services/logger.py) and a line per HTTP request,
// and these rules query them. docs/SLO.md says what each objective means and which rule
// guards it; docs/RUNBOOKS.md says what to do when one fires.
//
// An alert with no action group still shows in the portal under Alerts. infra/alerts.ps1
// creates the action group (it holds your email address, which is kept out of the repository)
// and deploy.ps1 passes its id here.
param location string
param tags object
param logAnalyticsName string
param postgresName string
@description('Resource id of the action group to notify, or empty for none.')
param actionGroupId string = ''

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: postgresName
}

var groups = empty(actionGroupId) ? [] : [actionGroupId]
var metricActions = empty(actionGroupId) ? [] : [{ actionGroupId: actionGroupId }]

var web = 'ContainerAppConsoleLogs_CL | where ContainerAppName_s == \'leyline-web\''

// Each rule fires when its query returns a row. "Silence" rules summarise to a count
// and keep the row only when it is zero.
var logRules = [
  {
    name: 'leyline-site-down'
    title: 'Leyline: the site is not answering readiness probes'
    description: 'No successful /readyz in 10 minutes: the site is down or a dependency (database, Redis, storage) is unreachable. Runbook: "The site is down". Objective: availability.'
    severity: 1
    frequency: 'PT5M'
    window: 'PT10M'
    query: '${web} | where Log_s has \'GET /readyz\' and Log_s has \'" 200 \' | summarize n = count() | where n == 0'
  }
  {
    name: 'leyline-server-errors'
    title: 'Leyline: server errors (5xx)'
    description: 'Five or more 5xx responses in 10 minutes, readiness probes included. Runbook: "Server errors". Objective: availability.'
    severity: 2
    frequency: 'PT5M'
    window: 'PT10M'
    query: '${web} | where Log_s matches regex @\'" 5[0-9][0-9] \' | summarize n = count() | where n >= 5'
  }
  {
    name: 'leyline-slow-first-token'
    title: 'Leyline: first token is slower than 2.5 s (95th percentile)'
    description: 'Over the last 30 minutes, with at least 10 answers, the 95th percentile of time-to-first-token (first_token_ms on NORMAL_REQUEST events) is above 2500 ms. Runbook: "Answers are slow". Objective: first token.'
    severity: 2
    frequency: 'PT10M'
    window: 'PT30M'
    query: '${web} | where Log_s has \'NORMAL_REQUEST\' and Log_s has \'first_token_ms\' | extend e = parse_json(Log_s) | extend ft = todouble(e.details.first_token_ms) | where isnotnull(ft) | summarize n = count(), p95 = percentile(ft, 95) | where n >= 10 and p95 > 2500'
  }
  {
    name: 'leyline-model-errors'
    title: 'Leyline: the chat model keeps failing (or its circuit is open)'
    description: 'Five or more MODEL_ERROR events in 10 minutes. The reason says why: circuit_open, timeout, http_429 (throttled), http_5xx. Runbook: "The assistant is unavailable".'
    severity: 2
    frequency: 'PT5M'
    window: 'PT10M'
    query: '${web} | where Log_s has \'MODEL_ERROR\' | summarize n = count() | where n >= 5'
  }
  {
    name: 'leyline-model-throttled'
    title: 'Leyline: Azure OpenAI is throttling requests (HTTP 429)'
    description: 'Three or more 429 answers from the model service in 10 minutes: the deployment quota (50 requests a minute) is being reached. Runbook: "The assistant is unavailable".'
    severity: 3
    frequency: 'PT5M'
    window: 'PT10M'
    query: '${web} | where Log_s has \'MODEL_ERROR\' and Log_s has \'http_429\' | summarize n = count() | where n >= 3'
  }
  {
    name: 'leyline-event-store-failing'
    title: 'Leyline: security events are not being stored'
    description: 'An EVENT_STORE_FAILED line appeared: events were dropped (queue full) or the database refused them. The stdout line remains the record. Runbook: "Security events are dropped".'
    severity: 2
    frequency: 'PT5M'
    window: 'PT10M'
    query: 'ContainerAppConsoleLogs_CL | where Log_s has \'EVENT_STORE_FAILED\' | summarize n = count() | where n >= 1'
  }
  {
    name: 'leyline-worker-silent'
    title: 'Leyline: the ingestion worker has gone quiet'
    description: 'No "worker heartbeat" log line in 10 minutes (it logs one a minute): uploads will not become searchable. Runbook: "The worker is silent".'
    severity: 2
    frequency: 'PT5M'
    window: 'PT10M'
    query: 'ContainerAppConsoleLogs_CL | where ContainerAppName_s == \'leyline-worker\' | where Log_s has \'worker heartbeat\' | summarize n = count() | where n == 0'
  }
]

resource logAlerts 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = [
  for rule in logRules: {
    name: rule.name
    location: location
    tags: tags
    properties: {
      displayName: rule.title
      description: rule.description
      severity: rule.severity
      enabled: true
      evaluationFrequency: rule.frequency
      windowSize: rule.window
      scopes: [logs.id]
      autoMitigate: true
      criteria: {
        allOf: [
          {
            query: rule.query
            timeAggregation: 'Count'
            operator: 'GreaterThan'
            threshold: 0
            failingPeriods: {
              numberOfEvaluationPeriods: 1
              minFailingPeriodsToAlert: 1
            }
          }
        ]
      }
      actions: { actionGroups: groups }
    }
  }
]

var metricRules = [
  {
    name: 'leyline-database-cpu'
    title: 'Leyline: the database CPU is above 80%'
    metric: 'cpu_percent'
    threshold: 80
  }
  {
    name: 'leyline-database-storage'
    title: 'Leyline: the database storage is above 85% full'
    metric: 'storage_percent'
    threshold: 85
  }
]

resource databaseAlerts 'Microsoft.Insights/metricAlerts@2018-03-01' = [
  for rule in metricRules: {
    name: rule.name
    location: 'global'
    tags: tags
    properties: {
      description: rule.title
      severity: 2
      enabled: true
      scopes: [postgres.id]
      evaluationFrequency: 'PT5M'
      windowSize: 'PT15M'
      criteria: {
        'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
        allOf: [
          {
            name: rule.metric
            metricName: rule.metric
            operator: 'GreaterThan'
            threshold: rule.threshold
            timeAggregation: 'Average'
            criterionType: 'StaticThresholdCriterion'
          }
        ]
      }
      actions: metricActions
    }
  }
]
