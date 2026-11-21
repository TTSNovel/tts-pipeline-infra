##############################################################################
# environments/dev/billing.tf — spend guards (Translate daily cap + budget)
##############################################################################
# The free trial credit ran out on 2026-11-01; from then Cloud Translation
# (novel-web /api/translate, ~6,300 chars ≈ 3,300 VND per new chapter) is
# ~96% of the bill. Two guards:
#   - a hard per-day character cap on Translation, so a long reading session
#     can't run up more than a known amount (over the cap Google answers 429
#     and the web reader shows the error / falls back to another engine);
#   - a monthly budget that emails at 50% / 90% / 100%.
# The Gemini engine needs neither: its key lives in tts-gemini-free-yl, a
# project with no billing account, so it can only use the free tier —
# Google's own free limits (15 req/min, 500 req/day for
# gemini-3.5-flash-lite) are the hard cap and there is nothing to bill.
#
# Chicken-and-egg like ci.tf: the budget lives on the billing account, not
# the project, so github-terraform needs roles/billing.costsManager there.
# That grant can't come from this config (the SA can't grant itself billing
# roles) — it was added once by hand by the billing admin:
#   gcloud billing accounts add-iam-policy-binding 01B881-F7E303-B316E3 \
#     --member=serviceAccount:github-terraform@tts-pipeline-yl.iam.gserviceaccount.com \
#     --role=roles/billing.costsManager

variable "billing_account_id" {
  description = "Billing account linked to project_id"
  type        = string
  default     = "01B881-F7E303-B316E3"
}

variable "billing_alert_email" {
  description = "Email address to receive budget alert notifications"
  type        = string
  default     = "yanglong.ph@gmail.com"
}

# Hard cap, ~3 new chapters/day: at most ~600k chars/month, i.e. at most
# ~100k past the 500k free chars (~52,000 VND). The monthly soft cap in
# novel-web (GOOGLE_TRANSLATE_MONTHLY_CHARS, translation.py) normally stops
# well before that, just past the free tier. Resets at midnight Pacific
# time (15:00 Vietnam time in winter, 14:00 in summer).
variable "translate_daily_char_limit" {
  description = "Cloud Translation characters per day for the whole project"
  type        = number
  default     = 20000
}

# Billing account is in VND — the Budget API requires the account currency.
variable "monthly_budget_vnd" {
  description = "Monthly budget for this project, in VND"
  type        = number
  default     = 200000
}

resource "google_project_service" "billing_apis" {
  for_each = toset([
    "billingbudgets.googleapis.com",
    "cloudbilling.googleapis.com",
    "monitoring.googleapis.com",
    "cloudquotas.googleapis.com",
    "serviceusage.googleapis.com",
  ])
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# Service Usage override rather than google_cloud_quotas_quota_preference:
# going from unlimited to a cap trips two safety checks at once (>10%
# decrease, and below recent usage — 1.25M chars on 2026-11-20) and that
# resource can only waive one; `force` here waives both.
resource "google_service_usage_consumer_quota_override" "translate_daily_chars" {
  provider       = google-beta
  project        = var.project_id
  service        = "translate.googleapis.com"
  metric         = urlencode("translate.googleapis.com/default")
  limit          = urlencode("/d/project")
  override_value = var.translate_daily_char_limit
  force          = true

  depends_on = [google_project_service.apis]
}

resource "google_monitoring_notification_channel" "billing_alert_email" {
  project      = var.project_id
  display_name = "Billing alert email"
  type         = "email"

  labels = {
    email_address = var.billing_alert_email
  }

  depends_on = [google_project_service.billing_apis]
}

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account_id
  display_name    = "${var.project_id}-monthly-budget"

  budget_filter {
    projects = ["projects/${data.google_project.current.number}"]
  }

  amount {
    specified_amount {
      currency_code = "VND"
      units         = tostring(var.monthly_budget_vnd)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.9
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  # Heads-up before it happens, from Google's month-end projection.
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.billing_alert_email.id]
    disable_default_iam_recipients   = false
  }

  depends_on = [google_project_service.billing_apis]
}
