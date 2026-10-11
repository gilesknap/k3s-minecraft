{{/* Chart name. */}}
{{- define "mcdash.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Fully qualified app name. */}}
{{- define "mcdash.fullname" -}}
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

{{- define "mcdash.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mcdash.labels" -}}
helm.sh/chart: {{ include "mcdash.chart" . }}
{{ include "mcdash.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "mcdash.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcdash.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "mcdash.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "mcdash.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Image reference; also passed to the app as MCD_JOB_IMAGE. */}}
{{- define "mcdash.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{/* PVC name for a storage tier: include "mcdash.pvcName" (list . "archive") */}}
{{- define "mcdash.pvcName" -}}
{{- $root := index . 0 }}
{{- $tier := index . 1 }}
{{- $cfg := index $root.Values.storage $tier }}
{{- if $cfg.existingClaim }}
{{- $cfg.existingClaim }}
{{- else }}
{{- printf "%s-%s" (include "mcdash.fullname" $root) $tier | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/* Cluster-scoped PV name, made unique by prefixing the release namespace. */}}
{{- define "mcdash.pvName" -}}
{{- $root := index . 0 }}
{{- $tier := index . 1 }}
{{- printf "%s-%s-%s" $root.Release.Namespace (include "mcdash.fullname" $root) $tier | trunc 63 | trimSuffix "-" }}
{{- end }}
