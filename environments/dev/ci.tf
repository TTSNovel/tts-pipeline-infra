##############################################################################
# environments/dev/ci.tf — GitHub Actions → GCP (Workload Identity Federation)
##############################################################################
# Keyless auth: each workflow trades its GitHub OIDC token for a short-lived
# service-account token, so no JSON key is ever stored in GitHub secrets.
# Only pushes to main of repos under the TTSNovel org can authenticate at all
# (attribute_condition below) — PR/fork runs get nothing.
#
# Chicken-and-egg like bootstrap/: this file has to be applied once by hand
# before tts-pipeline-infra's own workflow can authenticate to apply anything.

locals {
  github_org = "TTSNovel"
}

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  attribute_condition = "assertion.repository_owner == '${local.github_org}' && assertion.ref == 'refs/heads/main'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# ── tts-pipeline-infra: runs terraform apply ────────────────────────────────
# Owner because Terraform here manages IAM bindings, APIs, buckets and Cloud
# Run — anything narrower breaks the first time a new resource type is added.
# Scoped to this one repo's main branch via the WIF binding below.
resource "google_service_account" "github_terraform" {
  project      = var.project_id
  account_id   = "github-terraform"
  display_name = "GitHub Actions — tts-pipeline-infra terraform apply"
}

resource "google_project_iam_member" "github_terraform_owner" {
  project = var.project_id
  role    = "roles/owner"
  member  = "serviceAccount:${google_service_account.github_terraform.email}"
}

resource "google_service_account_iam_member" "github_terraform_wif" {
  service_account_id = google_service_account.github_terraform.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${local.github_org}/tts-pipeline-infra"
}

# ── tts-webnovel / tts-novel-ios: push images, roll Cloud Run, upload files ──
resource "google_service_account" "github_deployer" {
  project      = var.project_id
  account_id   = "github-deployer"
  display_name = "GitHub Actions — app deploys (novel-web, SideStore feed)"
}

resource "google_artifact_registry_repository_iam_member" "github_deployer_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.tts_pipeline.location
  repository = google_artifact_registry_repository.tts_pipeline.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github_deployer.email}"
}

resource "google_cloud_run_v2_service_iam_member" "github_deployer_novel_web" {
  project  = var.project_id
  location = google_cloud_run_v2_service.novel_web.location
  name     = google_cloud_run_v2_service.novel_web.name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.github_deployer.email}"
}

# Deploying a revision that runs as the default compute SA requires actAs on it.
resource "google_service_account_iam_member" "github_deployer_act_as_compute" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${data.google_project.current.number}-compute@developer.gserviceaccount.com"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_deployer.email}"
}

# novel-web's static assets (reader.js, sw.js, …) live on the mounted bucket,
# not in the image — see tts-webnovel's deploy workflow.
resource "google_storage_bucket_iam_member" "github_deployer_novel_web_site" {
  bucket = google_storage_bucket.novel_web_site.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.github_deployer.email}"
}

resource "google_storage_bucket_iam_member" "github_deployer_sidestore" {
  bucket = google_storage_bucket.sidestore_source.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.github_deployer.email}"
}

resource "google_service_account_iam_member" "github_deployer_wif" {
  for_each           = toset(["tts-webnovel", "tts-novel-ios"])
  service_account_id = google_service_account.github_deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${local.github_org}/${each.key}"
}
