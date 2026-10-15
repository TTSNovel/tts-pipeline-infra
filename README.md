# tts-pipeline-infra

Terraform infra for the TTS pipeline backing the tts-reader-addon browser
extension's quick-read feature. Separate GCP project, separate repo from the
extension code (`tts-reader-addon`) and from the AWS test deployment
(`cc-infra-workloads`) — not tied to a specific model/backend name, since
that's already changed once (Kokoro → Piper).

GCP project id is not committed — set your own via `TF_VAR_project_id` (see
`.envrc`, gitignored).

## Layout

- `bootstrap/` — one-time setup, run manually, local state (chicken-and-egg:
  creates the GCS bucket that `environments/*` then use as their remote
  backend, so it can't use that backend itself).
- `environments/dev/` — the actual infra (Cloud Run, Artifact Registry, IAM),
  remote state in the bucket bootstrap created.
- `modules/` — shared modules, once there's more than one environment or
  something worth factoring out. Empty for now.

## Local setup

```bash
cd tts-pipeline-infra
direnv allow   # loads TF_VAR_project_id and friends from .envrc (gitignored, not committed)
gcloud auth application-default login   # one-time per machine, see project history
```

## First-time bootstrap (creates the Terraform state bucket — already run once)

```bash
cd bootstrap
terraform init
terraform apply
```

## Deploying environments/dev

```bash
cd environments/dev
terraform init -backend-config="bucket=${TF_VAR_project_id}-terraform-state"
terraform plan
terraform apply
```

## Building & pushing the tts-generate image

The Piper voice model (~63MB) lives in GCS (`${project_id}-tts-assets`
bucket), not in git — fetch it before building:

```bash
cd src/tts-generate
./fetch-model.sh   # downloads piper_models/*.onnx{,.json} from GCS
gcloud auth configure-docker asia-southeast1-docker.pkg.dev --quiet
docker buildx build --platform linux/amd64 --provenance=false \
  -t asia-southeast1-docker.pkg.dev/${TF_VAR_project_id}/tts-pipeline/tts-generate:latest \
  -f Dockerfile . --load
docker push asia-southeast1-docker.pkg.dev/${TF_VAR_project_id}/tts-pipeline/tts-generate:latest
```

Then `terraform apply` in `environments/dev` again to roll the Cloud Run
service onto the new image (bump `image_tag` if not reusing `latest`).

## Building & pushing the tts-gpu image

gwen-tts (Qwen3-TTS-0.6B Vietnamese voice-clone finetune), GPU-backed,
separate Cloud Run service from tts-generate (see `src/tts-gpu/main.py`
for why). The CUDA/torch image is too slow to cross-build via
`docker buildx --platform linux/amd64` QEMU emulation from an arm64 Mac,
so this one builds natively on Cloud Build instead:

```bash
cd src/tts-gpu
./fetch-model.sh   # pulls gwen-tts's built-in reference speakers from GitHub into gwen_data/
gcloud builds submit --project=${TF_VAR_project_id} --region=asia-southeast1 \
  --tag=asia-southeast1-docker.pkg.dev/${TF_VAR_project_id}/tts-pipeline/tts-gpu:latest \
  --timeout=1800s --machine-type=e2-highcpu-8 .
```

Then `terraform apply` in `environments/dev` to roll the Cloud Run GPU
service onto the new image. First deploy of an `nvidia-l4` (no zonal
redundancy) service auto-grants 3 GPUs of quota in-region — no manual
quota request needed for this tier.

## CI/CD (GitHub Actions)

Every push to `main` deploys automatically. Auth is keyless via Workload
Identity Federation (`environments/dev/ci.tf`), and only pushes to `main` of
`TTSNovel/*` repos can authenticate.

| Repo | Workflow does |
|---|---|
| `tts-pipeline-infra` | `terraform apply`, then a Cloud Build + Cloud Run roll for `tts-generate` / `tts-gpu` if `src/<service>/` changed |
| `tts-webnovel` | pytest → build/push `novel-web:<sha>` → `gcloud run deploy` → upload static assets to the site bucket |
| `tts-novel-ios` | on the self-hosted Mac runner: archive + export .ipa → upload it + `repo.json` to the SideStore bucket |

Cloud Run images are rolled by CI (`:<git sha>` tags), so Terraform ignores
`image` on those services (`lifecycle.ignore_changes`).

`ci.tf` was applied once by hand (this repo's own workflow needs it to log
in at all). Changing `ci.tf` itself goes through CI like anything else.
