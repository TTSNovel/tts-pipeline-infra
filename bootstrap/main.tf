##############################################################################
# bootstrap/main.tf — one-time setup, run manually with local state.
#
# Creates the GCS bucket that environments/* use as their remote backend.
# Can't use that backend for itself (chicken-and-egg), so this stays on
# local state permanently — re-run only if the state bucket itself needs to
# change (rare).
##############################################################################

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = ">= 5.0" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_storage_bucket" "terraform_state" {
  name                        = "${var.project_id}-terraform-state"
  location                    = var.state_bucket_location
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }

  # Keep the last 10 versions of each state file, drop older ones — state
  # files are small, this is just to stop the bucket growing forever.
  lifecycle_rule {
    condition {
      num_newer_versions = 10
    }
    action {
      type = "Delete"
    }
  }
}
