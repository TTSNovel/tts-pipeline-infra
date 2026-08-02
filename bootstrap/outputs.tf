output "state_bucket" {
  value       = google_storage_bucket.terraform_state.name
  description = "Use as `bucket` in environments/*/main.tf's backend \"gcs\" block."
}
