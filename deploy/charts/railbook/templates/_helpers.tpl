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
    and renders the files below into /vault/secrets BEFORE the app starts;
  - unless prePopulateOnly, a sidecar that keeps renewing the DB credential's lease and
    re-renders db.json when Vault issues a new user (at the role's max_ttl).
The templates are Vault Agent (Go template) syntax, so they are wrapped in {{` `}} to stop Helm
from interpreting them.
  - app.env: `export` lines that the start command loads into the env once (static secrets).
  - db.json: the dynamic DB user. The app reads it on every new connection (app/db.py), so a
    re-rendered file is picked up without a restart.
*/}}
{{- define "railbook.vaultAnnotations" -}}
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: {{ .root.Values.vault.role | quote }}
{{- if .prePopulateOnly }}
vault.hashicorp.com/agent-pre-populate-only: "true"
{{- else }}
# The init container saves its Vault token and leases to a shared in-memory cache; the sidecar
# starts from that cache. Without it the sidecar asks for its own DB user and renews only that
# one, so the user the app started with expires at the 1 h lease TTL.
vault.hashicorp.com/agent-cache-enable: "true"
{{- end }}
vault.hashicorp.com/agent-inject-secret-db.json: {{ .root.Values.vault.dbCredsPath | quote }}
vault.hashicorp.com/agent-inject-template-db.json: |
  {{`{{ with secret "`}}{{ .root.Values.vault.dbCredsPath }}{{`" -}}`}}
  {{`{{ .Data | toJSON }}`}}
  {{`{{- end }}`}}
vault.hashicorp.com/agent-inject-secret-app.env: {{ .root.Values.vault.paymentPath | quote }}
vault.hashicorp.com/agent-inject-template-app.env: |
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
