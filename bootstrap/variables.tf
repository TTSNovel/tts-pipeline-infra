variable "project_id" {
  type    = string
  default = "tts-pipeline-yl"
}

variable "region" {
  type    = string
  default = "asia-southeast1"
}

# GCS bucket location — can be a region ("asia-southeast1") or a multi-region
# ("ASIA"). Single region is fine here: state files are tiny and this isn't
# serving traffic, no need for multi-region redundancy.
variable "state_bucket_location" {
  type    = string
  default = "asia-southeast1"
}
