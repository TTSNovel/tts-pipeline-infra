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

# ~30 new chapters/day (~105,000 VND/day at most). Resets at midnight
# Pacific time (15:00 Vietnam time in winter, 14:00 in summer).
variable "translate_daily_char_limit" {
  description = "Cloud Translation characters per day for the whole project"
  type        = number
  default     = 200000
}

# Billing account is in VND — the Budget API requires the account currency.
variable "monthly_budget_vnd" {
  description = "Monthly budget for this project, in VND"
  type        = number
  default     = 1000000
}

resource "google_project_service" "billing_apis" {
  for_each = toset([
    "billingbudgets.googleapis.com",
    "cloudbilling.googleapis.com",
    "monitoring.googleapis.com",
    "cloudquotas.googleapis.com",
  ])
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

resource "google_cloud_quotas_quota_preference" "translate_daily_chars" {
  parent        = "projects/${var.project_id}"
  name          = "translate-default-per-day"
  service       = "translate.googleapis.com"
  quota_id      = "DefaultPerDayPerProject"
  contact_email = var.billing_alert_email
  justification = "Cap Cloud Translation spend for a personal reading site."
  # Going from unlimited to a cap is a >10% decrease, which the API refuses
  # unless told it's intended.
  ignore_safety_checks = "QUOTA_DECREASE_PERCENTAGE_TOO_HIGH"

  quota_config {
    preferred_value = var.translate_daily_char_limit
  }

  depends_on = [google_project_service.apis, google_project_service.billing_apis]
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
