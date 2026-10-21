{{/* Labels on every object: lets you select everything in this release, e.g. kubectl get all -l app.kubernetes.io/instance=railbook */}}
{{- define "railbook.labels" -}}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: railbook
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{/*
Vault Agent Injector annotations. The injector's webhook sees these when the pod is created and adds:
  - an init container that logs into Vault (role below, using the pod's service account token)
    and renders /vault/secrets/app.env BEFORE the app starts;
  - unless prePopulateOnly, a sidecar that keeps renewing the DB credential's lease.
The template is Vault Agent (Go template) syntax, so it is wrapped in {{` `}} to stop Helm
from interpreting it. The file holds `export` lines that the start command loads into the env.
*/}}
{{- define "railbook.vaultAnnotations" -}}
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: {{ .root.Values.vault.role | quote }}
{{- if .prePopulateOnly }}
vault.hashicorp.com/agent-pre-populate-only: "true"
{{- end }}
vault.hashicorp.com/agent-inject-secret-app.env: {{ .root.Values.vault.dbCredsPath | quote }}
vault.hashicorp.com/agent-inject-template-app.env: |
  {{`{{ with secret "`}}{{ .root.Values.vault.dbCredsPath }}{{`" -}}`}}
  export DB_USER="{{`{{ .Data.username }}`}}"
  export DB_PASSWORD="{{`{{ .Data.password }}`}}"
  {{`{{ end -}}`}}
  {{`{{ with secret "`}}{{ .root.Values.vault.paymentPath }}{{`" -}}`}}
  export PAYMENT_API_KEY="{{`{{ .Data.data.api_key }}`}}"
  {{`{{ end -}}`}}
{{- end }}

{{/* Non-secret config from the ConfigMap. Secrets come only from Vault (above). */}}
{{- define "railbook.apiEnv" -}}
envFrom:
  - configMapRef:
      name: railbook-config
{{- end }}

{{/* Load the Vault-rendered file, then exec the process so it stays PID 1 and gets SIGTERM. */}}
{{- define "railbook.command" -}}
command: ["sh", "-c", ". /vault/secrets/app.env && exec {{ . }}"]
{{- end }}

{{/* Locked-down container: non-root, no privilege escalation, read-only filesystem, no Linux capabilities. */}}
{{- define "railbook.apiSecurityContext" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
{{- end }}
