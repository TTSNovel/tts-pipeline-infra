# tts-pipeline-infra

Terraform infra for the TTS pipeline (GCP project `tts-pipeline-yl`) backing
the kokoro-tts-addon browser extension's quick-read feature. Separate GCP
project, separate repo from the extension code (`kokoro-tts-addon`) and from
the AWS test deployment (`cc-infra-workloads`) — not tied to a specific
model/backend name, since that's already changed once (Kokoro → Piper).

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
direnv allow   # auto-selects the tts-pipeline-yl GCP project (see .envrc)
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
terraform init
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
  -t asia-southeast1-docker.pkg.dev/tts-pipeline-yl/tts-pipeline/tts-generate:latest \
  -f Dockerfile . --load
docker push asia-southeast1-docker.pkg.dev/tts-pipeline-yl/tts-pipeline/tts-generate:latest
```

Then `terraform apply` in `environments/dev` again to roll the Cloud Run
service onto the new image (bump `image_tag` if not reusing `latest`).
