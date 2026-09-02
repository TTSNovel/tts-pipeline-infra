variable "project_id" {
  type = string # set via TF_VAR_project_id (see .envrc) — no default, personal GCP project
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

# HTTP Basic Auth gate for novel-web (the public reading site) — network is
# reachable by anyone, but every route requires these credentials. Pass via
# TF_VAR_web_auth_user / TF_VAR_web_auth_pass (see .envrc) or -var.
variable "web_auth_user" {
  type      = string
  sensitive = true
}

variable "web_auth_pass" {
  type      = string
  sensitive = true
}

# Signs novel-web's login session cookie (Flask's session, stateless — no
# server-side session store). Pass via TF_VAR_session_secret or -var.
variable "session_secret" {
  type      = string
  sensitive = true
}
