##############################################################################
# environments/dev/main.tf — Day-2 infra for tts-pipeline-yl (dev)
##############################################################################

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = ">= 5.0" }
  }
  backend "gcs" {
    bucket = "tts-pipeline-yl-terraform-state"
    prefix = "dev"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# APIs the TTS pipeline needs as it gets built out (Cloud Run service +
# Artifact Registry image + Workload Identity Federation for CI come next).
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",              # Cloud Run
    "artifactregistry.googleapis.com", # Docker image storage
    "iam.googleapis.com",
    "iamcredentials.googleapis.com", # Workload Identity Federation
    "cloudresourcemanager.googleapis.com",
  ])
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}
