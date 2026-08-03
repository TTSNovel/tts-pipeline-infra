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

# APIs the TTS pipeline needs (Cloud Run service + Artifact Registry image +
# Workload Identity Federation for CI, once there is a CI to wire up).
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",              # Cloud Run
    "artifactregistry.googleapis.com", # Docker image storage
    "iam.googleapis.com",
    "iamcredentials.googleapis.com", # Workload Identity Federation
    "cloudresourcemanager.googleapis.com",
    "texttospeech.googleapis.com", # google_tts mode
  ])
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# ── Model assets (Piper voice, ~63MB binary) ──────────────────────────────────
# Kept out of git entirely (was going to bloat repo history permanently) —
# lives here instead. Downloaded into the image at `docker build` time (see
# scripts/fetch-model.sh), same "bake into image, zero runtime network calls"
# property the AWS cold-start fix depends on — just fetched from here instead
# of committed to the repo.
resource "google_storage_bucket" "tts_assets" {
  project                     = var.project_id
  name                        = "${var.project_id}-tts-assets"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
}

resource "google_storage_bucket_object" "piper_model" {
  name   = "piper/vi_VN-vais1000-medium.onnx"
  bucket = google_storage_bucket.tts_assets.name
  source = "${path.module}/../../src/tts-generate/piper_models/vi_VN-vais1000-medium.onnx"
}

resource "google_storage_bucket_object" "piper_model_config" {
  name   = "piper/vi_VN-vais1000-medium.onnx.json"
  bucket = google_storage_bucket.tts_assets.name
  source = "${path.module}/../../src/tts-generate/piper_models/vi_VN-vais1000-medium.onnx.json"
}

# English voice — routes English runs (character names, brand/platform
# names) that segment_by_language() splits out of otherwise-Vietnamese
# text, instead of forcing them through the vi phonemizer. Same 22050Hz
# rate as the Vietnamese voice (verified against both onnx.json files at
# implementation time) so runs concatenate without resampling.
resource "google_storage_bucket_object" "piper_model_en" {
  name   = "piper/en_US-lessac-medium.onnx"
  bucket = google_storage_bucket.tts_assets.name
  source = "${path.module}/../../src/tts-generate/piper_models/en_US-lessac-medium.onnx"
}

resource "google_storage_bucket_object" "piper_model_en_config" {
  name   = "piper/en_US-lessac-medium.onnx.json"
  bucket = google_storage_bucket.tts_assets.name
  source = "${path.module}/../../src/tts-generate/piper_models/en_US-lessac-medium.onnx.json"
}

# ── Artifact Registry (Docker images) ─────────────────────────────────────────
resource "google_artifact_registry_repository" "tts_pipeline" {
  project       = var.project_id
  location      = var.region
  repository_id = "tts-pipeline"
  format        = "DOCKER"

  cleanup_policies {
    id     = "keep-last-10"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  depends_on = [google_project_service.apis]
}

# ── Cloud Run (tts-generate) ──────────────────────────────────────────────────
# Piper TTS, synchronous request/response — Cloud Run equivalent of the AWS
# Lambda tts-generate test deployment (same handler logic, ported to Flask).
resource "google_cloud_run_v2_service" "tts_generate" {
  name     = "tts-generate"
  location = var.region
  project  = var.project_id

  template {
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.tts_pipeline.repository_id}/tts-generate:${var.image_tag}"

      ports {
        container_port = 8080
      }

      env {
        name  = "TTS_API_SECRET"
        value = var.tts_api_secret
      }

      resources {
        limits = {
          cpu    = "2"
          # 1Gi was enough for Piper alone; vieneu mode adds a ~200MB model
          # plus gradio/librosa import overhead pulled in as vieneu's pip
          # dependencies, so bumped for headroom.
          memory = "2Gi"
        }
        # CPU-bound ONNX inference (confirmed on the AWS side: warm latency
        # tracks allocated CPU almost linearly) — allocate CPU only during
        # request processing (default, request-based billing / free-tier
        # eligible) but boost it during cold start to shorten model load.
        cpu_idle          = true
        startup_cpu_boost = true
      }
    }

    scaling {
      min_instance_count = 0 # scale to zero — no idle cost
      max_instance_count = 3
    }

    timeout = "25s" # match the AWS deployment's Lambda timeout
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.tts_generate.location
  name     = google_cloud_run_v2_service.tts_generate.name
  role     = "roles/run.invoker"
  # Public endpoint — same posture as the AWS deployment: no IAM-level auth,
  # the app itself checks x-tts-secret on every request.
  member = "allUsers"
}
