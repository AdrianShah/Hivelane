{{/* Name of the chart, overridable. */}}
{{- define "synergo-relay.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Fully qualified app name: release-chart, unless the release already says it. */}}
{{- define "synergo-relay.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "synergo-relay.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "synergo-relay.labels" -}}
helm.sh/chart: {{ include "synergo-relay.chart" . }}
{{ include "synergo-relay.selectorLabels" . }}
app.kubernetes.io/version: {{ (default .Chart.AppVersion .Values.image.tag) | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "synergo-relay.selectorLabels" -}}
app.kubernetes.io/name: {{ include "synergo-relay.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* True (non-empty) when the chart creates and mounts a PVC for /data. */}}
{{- define "synergo-relay.usePVC" -}}
{{- if and .Values.persistence.enabled (eq (int .Values.replicaCount) 1) }}true{{- end }}
{{- end }}

{{/* Refuse configurations the relay cannot serve correctly. */}}
{{- define "synergo-relay.validate" -}}
{{- if and (gt (int .Values.replicaCount) 1) (not (or .Values.relay.redisUrl .Values.relay.existingSecret)) }}
{{- fail "replicaCount > 1 needs Redis: set relay.redisUrl (or SYNERGO_REDIS_URL in relay.existingSecret), and route each /ws/<code> to one pod (see the chart README)" }}
{{- end }}
{{- if and (ne .Values.relay.env "development") (not .Values.relay.corsOrigins) }}
{{- fail "relay.corsOrigins is required unless relay.env is development: the relay refuses to boot without it (list this relay's own URL if the bundled dashboard is the only one)" }}
{{- end }}
{{- end }}
