##############################################################################
# environments/dev/main.tf — Day-2 infra (dev)
##############################################################################

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = ">= 5.0" }
  }
  # bucket intentionally omitted (partial backend config) — the state bucket
  # name is project-specific, not committed. Run:
  #   terraform init -backend-config="bucket=${TF_VAR_project_id}-terraform-state"
  backend "gcs" {
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
    "cloudbuild.googleapis.com",   # gcloud builds submit (tts-gpu's CUDA image is too slow to buildx/emulate locally on arm64)
    "translate.googleapis.com",    # novel-web /api/translate (Cloud Translation v3 + per-book glossaries)
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

# ── SideStore source (AltStore-style repo.json feed) ─────────────────────────
# Public-read feed + .ipa hosting for sideloading personal iOS builds
# (WebnovelReader, etc.) through SideStore instead of Xcode-installing them
# directly — lets SideStore auto re-sign before the 7-day free-account
# provisioning profile expires. Objects are uploaded ad-hoc via `gcloud
# storage cp` (same manual-deploy posture as the Docker images pushed via
# `gcloud builds submit`, see README) rather than as google_storage_bucket_object
# resources — the .ipa changes every build, so tracking it in Terraform state
# would mean a plan/apply cycle per rebuild for no benefit.
resource "google_storage_bucket" "sidestore_source" {
  project                     = var.project_id
  name                        = "${var.project_id}-sidestore-source"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true # small, disposable dev-build artifacts
}

resource "google_storage_bucket_iam_member" "sidestore_source_public_read" {
  bucket = google_storage_bucket.sidestore_source.name
  role   = "roles/storage.objectViewer"
  # Public read — a SideStore source URL must be fetchable by the phone
  # over the internet with no auth, same posture as the allUsers Cloud Run
  # invokers below.
  member = "allUsers"
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

# Cloud Build (gcloud builds submit, used for tts-gpu's CUDA image — see
# src/tts-gpu/README note) runs as the project's default Compute Engine
# service account, not the legacy cloudbuild.gserviceaccount.com identity,
# which otherwise can't push the built image here.
resource "google_artifact_registry_repository_iam_member" "tts_pipeline_cloudbuild_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.tts_pipeline.location
  repository = google_artifact_registry_repository.tts_pipeline.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
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

# ── Cloud Run (tts-gpu) ────────────────────────────────────────────────────
# gwen-tts (Qwen3-TTS-0.6B finetune), Vietnamese voice cloning — a separate
# service from tts-generate rather than a new MODELS entry there, because
# attaching a GPU forces GPU billing/sizing (min 4 CPU / 16Gi mem,
# instance-based billing — no cpu_idle) onto the WHOLE service, which would
# make every cheap piper_vi/google_tts request pay GPU-instance pricing if
# they shared one. See src/tts-gpu/main.py.
resource "google_cloud_run_v2_service" "tts_gpu" {
  name     = "tts-gpu"
  location = var.region
  project  = var.project_id

  template {
    # nvidia-l4 with zonal redundancy OFF is the variant GCP auto-grants 3
    # units of on first use, no manual quota request needed — the zonal-
    # redundant variant requires an explicit quota increase. A personal
    # reading-site backend doesn't need multi-zone HA for this.
    gpu_zonal_redundancy_disabled = true

    node_selector {
      accelerator = "nvidia-l4"
    }

    # One GPU per instance — one request at a time per instance, same
    # reasoning as tts_generate's vieneu concurrency fix above, just
    # inherent here (only one GPU to share, not a tunable trade-off).
    max_instance_request_concurrency = 1

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.tts_pipeline.repository_id}/tts-gpu:${var.image_tag}"

      ports {
        container_port = 8080
      }

      env {
        name  = "TTS_API_SECRET"
        value = var.tts_api_secret
      }

      resources {
        limits = {
          cpu              = "4"
          memory           = "16Gi"
          "nvidia.com/gpu" = "1"
        }
        # GPU services require instance-based billing (CPU always
        # allocated) — cpu_idle=true (request-based) isn't supported here.
        cpu_idle = false
      }
    }

    scaling {
      min_instance_count = 0 # scale to zero — no idle GPU cost
      # reader.js prefetches PRELOAD_AHEAD=6 sentences concurrently — set to
      # 6 to match, though actual scaling is capped by whatever the L4
      # no-zonal-redundancy quota is at any given time (auto-granted 3,
      # increase to 6 requested via `gcloud alpha quotas preferences
      # create` and pending Google's approval as of this change; excess
      # concurrent requests beyond the granted quota just queue longer,
      # they don't hard-fail).
      max_instance_count = 6
    }

    # Generative (autoregressive) synthesis on a 0.6B transformer is much
    # slower than Piper's ONNX vocoder — measured ~0.4-0.5s/char with no
    # flash-attn (sdpa fallback) on an L4, so reader.js's 150-char max
    # chunk plus a cold model load (~12s) can approach 90-100s. 180s gives
    # headroom without reaching novel_web's 900s import-job ceiling.
    timeout = "180s"
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "tts_gpu_public_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.tts_gpu.location
  name     = google_cloud_run_v2_service.tts_gpu.name
  role     = "roles/run.invoker"
  # Same posture as tts_generate — public at the network level, gated by
  # the app's own x-tts-secret check.
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

# novel-web calls Cloud Translation v3 as this same default compute service
# account: /api/translate translates with the glossary, and /api/glossary
# (saving the glossary edited in the reader/app) re-creates the Google
# glossaries from TSVs it writes to the site bucket above — hence editor,
# not just user (which can't create/delete glossaries). See tts-webnovel's
# app/translation.py.
resource "google_project_iam_member" "novel_web_translate_editor" {
  project = var.project_id
  role    = "roles/cloudtranslate.editor"
  member  = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
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

    # Previously unset here, which left Cloud Run's default of 80 in place —
    # a number nobody ever measured against this app, unlike tts_generate's
    # deliberately-tuned max_instance_request_concurrency above. The actual
    # in-process capacity is gunicorn's own `--workers 2 --threads 4` in
    # Dockerfile (2*4 = 8 requests truly served at once); anything beyond
    # that just queues inside the container, invisible to Cloud Run's own
    # scaling signal since it never gets near the stale 80 threshold. Live
    # incident: a burst of concurrent gwen_tts /api/tts calls (each blocking
    # a worker/thread for up to 180s waiting on tts_gpu, see tts_proxy() in
    # server.py) drove every route on novel-web — /login, chapter pages,
    # /api/progress — to stall for up to ~80s, all on a SINGLE instance the
    # whole time (confirmed via `gcloud logging read` during the incident:
    # one instanceId for all 283 requests in the 9-minute window), because
    # Cloud Run never saw enough concurrent load to justify scaling out.
    # Setting this to the real number (8) makes Cloud Run actually spin up
    # additional instances once one fills up, instead of silently queueing
    # everything onto the one instance it thinks still has headroom.
    max_instance_request_concurrency = 8

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
        name  = "GPU_TTS_API_URL"
        value = google_cloud_run_v2_service.tts_gpu.uri
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
      min_instance_count = 0  # scale to zero — no idle cost
      # Was 3 — with the corrected concurrency=8 above, 3 instances only
      # gives 24 concurrent request slots total, still not much headroom
      # once a handful of slow gwen_tts proxy calls (each holding a slot up
      # to 180s) are in flight alongside ordinary page/chapter traffic.
      # Raised so scaling out actually has room to work; still scale-to-
      # zero when idle, so this only costs anything under real concurrent
      # load, not just by being a bigger number.
      max_instance_count = 10
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
