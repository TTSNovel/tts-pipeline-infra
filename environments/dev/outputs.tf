output "tts_generate_url" {
  value       = google_cloud_run_v2_service.tts_generate.uri
  description = "kokoro-tts-addon extension: TTS Cloud Run endpoint"
}

output "artifact_registry_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.tts_pipeline.repository_id}"
}

output "novel_web_url" {
  value       = google_cloud_run_v2_service.novel_web.uri
  description = "Public reading site (behind HTTP Basic Auth)"
}

output "tts_gpu_url" {
  value       = google_cloud_run_v2_service.tts_gpu.uri
  description = "gwen-tts (GPU) Cloud Run endpoint"
}
