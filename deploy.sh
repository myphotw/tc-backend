#!/bin/sh

set -eu

PROJECT_NAME="tc-backend"
MAIN_BRANCH="main"
BACKEND_SERVICE="backend"
WORKER_SERVICE="upload-worker"
BACKEND_CONTAINER="tc-backend-backend-1"
WORKER_CONTAINER="tc-backend-upload-worker-1"
POSTGRES_CONTAINER="postgres"
BACKEND_IMAGE="tc-backend-backend:latest"
WORKER_IMAGE="tc-backend-upload-worker:latest"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE="$SCRIPT_DIR/.env"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.nas.yml"
MODE=${1:-deploy}

case "$MODE" in
  deploy|--check)
    ;;
  *)
    echo "Usage: $0 [--check]" >&2
    exit 2
    ;;
esac

compose() {
  sudo docker compose \
    --project-directory "$SCRIPT_DIR" \
    --env-file "$ENV_FILE" \
    --project-name "$PROJECT_NAME" \
    -f "$COMPOSE_FILE" \
    "$@"
}

container_health() {
  sudo docker inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    "$1"
}

require_container_ready() {
  container_name=$1
  health=$(container_health "$container_name")
  case "$health" in
    healthy|running)
      echo "READY: $container_name ($health)"
      ;;
    *)
      echo "NOT READY: $container_name ($health)" >&2
      return 1
      ;;
  esac
}

restore_previous_images() {
  echo "Restoring previous backend and worker images..." >&2
  sudo docker tag "$OLD_BACKEND_IMAGE_ID" "$BACKEND_IMAGE"
  sudo docker tag "$OLD_WORKER_IMAGE_ID" "$WORKER_IMAGE"
  compose up -d \
    --no-deps \
    --no-build \
    --force-recreate \
    --wait \
    --wait-timeout 120 \
    "$BACKEND_SERVICE" "$WORKER_SERVICE"
}

cd "$SCRIPT_DIR"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing required file: $ENV_FILE" >&2
  exit 1
fi

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "Missing required file: $COMPOSE_FILE" >&2
  exit 1
fi

if [ "$(git rev-parse --is-inside-work-tree 2>/dev/null)" != "true" ]; then
  echo "Not a Git working tree: $SCRIPT_DIR" >&2
  exit 1
fi

CURRENT_BRANCH=$(git symbolic-ref --short HEAD)
if [ "$CURRENT_BRANCH" != "$MAIN_BRANCH" ]; then
  echo "Expected branch '$MAIN_BRANCH', found '$CURRENT_BRANCH'" >&2
  exit 1
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Tracked files have local changes; deployment stopped." >&2
  git status --short
  exit 1
fi

sudo -v
sudo docker version >/dev/null
sudo docker compose version >/dev/null
compose config --quiet

ENV_HASH_BEFORE=$(sha256sum "$ENV_FILE" | awk '{print $1}')

require_container_ready "$BACKEND_CONTAINER"
require_container_ready "$WORKER_CONTAINER"
require_container_ready "$POSTGRES_CONTAINER"
sudo docker exec "$POSTGRES_CONTAINER" pg_isready -q
echo "READY: PostgreSQL accepts connections"

echo "Fetching origin/$MAIN_BRANCH..."
git fetch origin "$MAIN_BRANCH"

LOCAL_COMMIT=$(git rev-parse HEAD)
REMOTE_COMMIT=$(git rev-parse "origin/$MAIN_BRANCH")
echo "LOCAL=$LOCAL_COMMIT"
echo "REMOTE=$REMOTE_COMMIT"

if [ "$MODE" = "--check" ]; then
  if [ "$LOCAL_COMMIT" != "$REMOTE_COMMIT" ]; then
    echo "Local and remote commits differ; run ./deploy.sh to deploy." >&2
    exit 2
  fi
  echo "CHECK_OK: repository, Compose, containers, PostgreSQL, and .env"
  exit 0
fi

if ! git merge-base --is-ancestor "$LOCAL_COMMIT" "$REMOTE_COMMIT"; then
  echo "origin/$MAIN_BRANCH is not a fast-forward from the local commit." >&2
  exit 1
fi

git merge --ff-only "$REMOTE_COMMIT"

ENV_HASH_AFTER_PULL=$(sha256sum "$ENV_FILE" | awk '{print $1}')
if [ "$ENV_HASH_BEFORE" != "$ENV_HASH_AFTER_PULL" ]; then
  echo ".env changed during Git update; deployment stopped." >&2
  exit 1
fi

DEPLOY_COMMIT=$(git rev-parse HEAD)
DEPLOY_SHORT=$(git rev-parse --short=7 HEAD)
DEPLOY_STAMP=$(date +%Y%m%d-%H%M%S)

OLD_BACKEND_IMAGE_ID=$(sudo docker inspect --format '{{.Image}}' "$BACKEND_CONTAINER")
OLD_WORKER_IMAGE_ID=$(sudo docker inspect --format '{{.Image}}' "$WORKER_CONTAINER")

sudo docker tag "$OLD_BACKEND_IMAGE_ID" "tc-backend-backend:rollback-$DEPLOY_STAMP"
sudo docker tag "$OLD_WORKER_IMAGE_ID" "tc-backend-upload-worker:rollback-$DEPLOY_STAMP"

echo "Building commit $DEPLOY_COMMIT..."
compose build "$BACKEND_SERVICE" "$WORKER_SERVICE"

sudo docker tag "$BACKEND_IMAGE" "tc-backend-backend:git-$DEPLOY_SHORT"
sudo docker tag "$WORKER_IMAGE" "tc-backend-upload-worker:git-$DEPLOY_SHORT"

if ! compose run --rm --no-deps \
  -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint python \
  "$BACKEND_SERVICE" \
  -c 'from app.main import app; print("BACKEND_IMPORT_OK")'; then
  echo "Backend smoke test failed." >&2
  sudo docker tag "$OLD_BACKEND_IMAGE_ID" "$BACKEND_IMAGE"
  sudo docker tag "$OLD_WORKER_IMAGE_ID" "$WORKER_IMAGE"
  exit 1
fi

if ! compose run --rm --no-deps \
  -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint python \
  "$WORKER_SERVICE" \
  -c 'import worker.background_worker; print("UPLOAD_WORKER_IMPORT_OK")'; then
  echo "Worker smoke test failed." >&2
  sudo docker tag "$OLD_BACKEND_IMAGE_ID" "$BACKEND_IMAGE"
  sudo docker tag "$OLD_WORKER_IMAGE_ID" "$WORKER_IMAGE"
  exit 1
fi

if ! compose up -d \
  --no-deps \
  --no-build \
  --force-recreate \
  --wait \
  --wait-timeout 120 \
  "$BACKEND_SERVICE"; then
  echo "Backend rollout failed." >&2
  restore_previous_images
  exit 1
fi

if ! compose up -d \
  --no-deps \
  --no-build \
  --force-recreate \
  --wait \
  --wait-timeout 120 \
  "$WORKER_SERVICE"; then
  echo "Worker rollout failed." >&2
  restore_previous_images
  exit 1
fi

if ! require_container_ready "$BACKEND_CONTAINER" || \
   ! require_container_ready "$WORKER_CONTAINER" || \
   ! require_container_ready "$POSTGRES_CONTAINER" || \
   ! sudo docker exec "$POSTGRES_CONTAINER" pg_isready -q; then
  echo "Post-deployment verification failed." >&2
  restore_previous_images
  exit 1
fi

ENV_HASH_FINAL=$(sha256sum "$ENV_FILE" | awk '{print $1}')
if [ "$ENV_HASH_BEFORE" != "$ENV_HASH_FINAL" ]; then
  echo "WARNING: .env hash changed during deployment." >&2
  exit 1
fi

echo "DEPLOY_OK: $DEPLOY_COMMIT"
sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
