#!/usr/bin/env bash
# Install the public seller details page into the established Timeweb Compose project.
set -euo pipefail
umask 077
project_dir=/opt/kwazar-site
release_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"
for file in index.html styles.css server.py Dockerfile requisites.html; do
  test -s "$release_dir/$file"
done
test -s compose.yaml
test -s .env
docker compose exec -T app python3 -c 'import urllib.request; assert urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:8000/healthz", timeout=5).status == 200'
backup_dir=$(mktemp -d /opt/kwazar-before-requisites-XXXXXXXX)
rollback_image="kwazar-site:before-requisites-$(date -u +%Y%m%d%H%M%S)"
trap 'echo "Установка не завершена. Каталог копии: $backup_dir. Пришлите текст ошибки." >&2' ERR
tar -czf "$backup_dir/site.tar.gz" .
docker compose exec -T app python3 backup_store.py --data-dir /var/lib/kwazar --output-dir /tmp/before-requisites
docker compose exec -T app sh -c 'if [ -f /var/lib/kwazar/admin-token ]; then cp /var/lib/kwazar/admin-token /tmp/before-requisites/admin-token; fi'
docker compose exec -T app tar -C /tmp/before-requisites -cf - . > "$backup_dir/data.tar"
tar -tf "$backup_dir/data.tar" >/dev/null
# Read the running image rather than assuming that the tag still points to it.
container_id=$(docker compose ps -q app)
running_image=$(docker inspect --format '{{.Image}}' "$container_id")
docker image tag "$running_image" "$rollback_image"
{
  printf '#!/usr/bin/env bash\nset -euo pipefail\ncd %q\n' "$project_dir"
  printf 'tar -xzf %q -C %q ./index.html ./styles.css ./server.py ./Dockerfile\n' "$backup_dir/site.tar.gz" "$project_dir"
  printf 'docker image tag %q kwazar-site:local\n' "$rollback_image"
  printf 'docker compose up -d --no-build --no-deps --wait --wait-timeout 120 app\n'
} > "$backup_dir/rollback.sh"
chmod 700 "$backup_dir/rollback.sh"
for file in index.html styles.css server.py Dockerfile requisites.html; do
  install -m 644 "$release_dir/$file" "$project_dir/$file"
done
docker compose build app
docker compose up -d --no-deps --wait --wait-timeout 120 app
docker compose ps
curl --fail --silent --show-error --max-time 30 https://kwazar-led.ru/healthz
curl --fail --silent --show-error --max-time 30 https://kwazar-led.ru/requisites.html >/dev/null
printf '\nСтраница реквизитов установлена. Копия: %s\n' "$backup_dir"
printf 'При необходимости откат: bash %s/rollback.sh\n' "$backup_dir"
