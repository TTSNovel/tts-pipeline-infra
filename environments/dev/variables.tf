variable "project_id" {
  type    = string
  default = "tts-pipeline-yl"
}

variable "region" {
  type    = string
  default = "asia-southeast1"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "image_tag" {
  type    = string
  default = "latest"
}

# Shared secret for tts-generate — pass via TF_VAR_tts_api_secret (see
# .envrc) or -var. Not committed anywhere.
variable "tts_api_secret" {
  type      = string
  sensitive = true
}
