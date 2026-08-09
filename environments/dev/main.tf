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

data "google_project" "current" {
  project_id = var.project_id
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
    # Default concurrency (160) assumes light/IO-bound requests sharing one
    # instance's CPU fine — true for piper_vi/google_tts, but measured false
    # for vieneu: a single request already drives ~2.8 cores of parallel
    # onnxruntime work (see main.py), more than this instance's whole "2"
    # cpu limit. At the old default, a burst of concurrent requests (the
    # extension's read-aloud prefetch fires up to 10 at once) all piled onto
    # ONE instance's 2 vCPU instead of Cloud Run spinning up more instances
    # — observed live as per-request synth time ballooning from ~5s to ~54s
    # as the backlog grew. Set to 1 so each instance handles one request at
    # a time and gets its full CPU budget; max_instance_count raised below
    # so a 10-request prefetch burst can actually get 10 parallel instances
    # instead of queuing.
    max_instance_request_concurrency = 1

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
      min_instance_count = 0  # scale to zero — no idle cost
      max_instance_count = 10 # matches the extension's PRELOAD_AHEAD burst size
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

# ── novel-web site storage (GCS, mounted straight into Cloud Run) ────────────
# Book content lives here instead of baked into the Docker image — the
# /import page needs to add new books at runtime, and a container's own
# filesystem is ephemeral/per-instance (writes there would vanish on
# restart and never reach the other scaled-out instances anyway). Cloud
# Run v2 can mount a GCS bucket as a real read/write filesystem path
# (requires the gen2 execution environment below), so server.py's existing
# send_from_directory()-based routes and the import handler's plain
# file-write both just work unchanged against a local-looking path.
resource "google_storage_bucket" "novel_web_site" {
  project                     = var.project_id
  name                        = "${var.project_id}-novel-web-site"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
}

# Cloud Run defaults to the project's Compute Engine default service
# account when none is set (true for novel-web) — grant it read/write on
# the bucket it mounts.
resource "google_storage_bucket_iam_member" "novel_web_site_rw" {
  bucket = google_storage_bucket.novel_web_site.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

# ── Cloud Run (novel-web) ──────────────────────────────────────────────────
# Public reading site (epub library converted to per-chapter HTML pages,
# grouped by category) with an embedded read-aloud reader. Network-reachable
# by anyone (same allUsers-invoker posture as tts-generate below), but every
# route requires a login session — chosen over pre-generating audio for
# every chapter of every book (thousands of chapters, most never read) or
# exposing tts-generate's secret to client-side JS on a public page. The app
# proxies /api/tts to tts-generate server-side so that secret never reaches
# the browser. See src/novel-web/server.py.
resource "google_cloud_run_v2_service" "novel_web" {
  name     = "novel-web"
  location = var.region
  project  = var.project_id

  template {
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2" # required for GCS volume mounts

    volumes {
      name = "site-bucket"
      gcs {
        bucket    = google_storage_bucket.novel_web_site.name
        read_only = false
      }
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.tts_pipeline.repository_id}/novel-web:${var.image_tag}"

      ports {
        container_port = 8080
      }

      volume_mounts {
        name       = "site-bucket"
        mount_path = "/mnt/site"
      }

      env {
        name  = "SITE_DIR"
        value = "/mnt/site"
      }
      env {
        name  = "WEB_AUTH_USER"
        value = var.web_auth_user
      }
      env {
        name  = "WEB_AUTH_PASS"
        value = var.web_auth_pass
      }
      env {
        name  = "SESSION_SECRET"
        value = var.session_secret
      }
      env {
        name  = "TTS_API_URL"
        value = google_cloud_run_v2_service.tts_generate.uri
      }
      env {
        name  = "TTS_API_SECRET"
        value = var.tts_api_secret
      }

      resources {
        limits = {
          # Extraction (ebooklib/bs4/lxml over a multi-MB book) now runs
          # in-process on /import, on top of the plain request handling —
          # bumped from the original static-file-server-only sizing.
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle = true
      }
    }

    scaling {
      min_instance_count = 0 # scale to zero — no idle cost
      max_instance_count = 3 # low-traffic personal site behind auth
    }

    # 900s: not just a live TTS proxy call (60s would cover that alone) —
    # /import writes one file per chapter onto the GCS FUSE-mounted volume,
    # measured ~0.18s/chapter, so the biggest book here (3543 chapters)
    # needs ~630s. 900s gives margin, still well under Cloud Run's 3600s cap.
    timeout = "900s"
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "novel_web_public_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.novel_web.location
  name     = google_cloud_run_v2_service.novel_web.name
  role     = "roles/run.invoker"
  # Public at the network level — access is actually gated by HTTP Basic
  # Auth inside the app (every route, see server.py's require_auth).
  member = "allUsers"
}
