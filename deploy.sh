#!/bin/sh
# Copy this folder to the NAS, rebuild and restart Ferry.
#   FERRY_HOST  ssh host of the NAS (default: nas)
#   FERRY_DIR   folder on the NAS holding .env and data/ (default: /volume1/docker/ferry)
# The code goes to $FERRY_DIR/app, the previous copy to the docker share's #recycle.
# A docker-compose.yml of your own in $FERRY_DIR wins over the one in the code.
set -e
HOST=${FERRY_HOST:-nas}
DIR=${FERRY_DIR:-/volume1/docker/ferry}
cd "$(dirname "$0")"
echo "Copying code to $HOST:$DIR/app"
COPYFILE_DISABLE=1 tar --no-xattrs --exclude .git --exclude tests --exclude data --exclude .env \
  --exclude '__pycache__' --exclude '.DS_Store' -czf - . 2>/dev/null \
  | ssh "$HOST" "rm -rf '$DIR/app.new' && mkdir -p '$DIR/app.new' && tar -xzf - -C '$DIR/app.new' 2>/dev/null"
echo "Building and restarting"
ssh "$HOST" "export PATH=\$PATH:/usr/local/bin
set -e
cd '$DIR'
if [ -d app ]; then mkdir -p \"\$(dirname '$DIR')/#recycle\" && mv app \"\$(dirname '$DIR')/#recycle/ferry-app-\$(date +%Y%m%d-%H%M%S)\"; fi
mv app.new app
DOCKER_BUILDKIT=0 docker build -q -t ferry:local app >/dev/null
if [ -f docker-compose.yml ]; then docker compose up -d --no-build
else docker compose -f app/docker-compose.yml --project-directory . up -d --no-build; fi
sleep 8
curl -s http://127.0.0.1:8420/api/health; echo"
