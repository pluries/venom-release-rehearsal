#!/usr/bin/env bash
# Sourced by check-site-image.sh. On the docker-e2e agent the site container is a sibling on the shared daemon, so its
# published port is not on the agent's loopback: after the loopback it tries the container's own address on port 8080
# (found by V8, story #3680). Usage: reach_site NAME PORT; prints the URL that answered 200.

reach_site() {
  local name="$1" port="$2" tries="${SITE_IMAGE_TRIES:-20}" nap="${SITE_IMAGE_SLEEP:-0.5}"
  local round url code ip token tried=""
  code=000
  for ((round = 0; round < tries; round++)); do
    url="http://127.0.0.1:$port/"
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" || true)"
    case "$tried" in *"$url"*) ;; *) tried="$tried $url" ;; esac
    if [ "$code" = "200" ]; then
      echo "$url"
      return 0
    fi
    ip=""
    for token in $(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$name" 2>/dev/null || true); do
      if [[ "$token" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
        ip="$token"
        break
      fi
    done
    if [ -n "$ip" ]; then
      url="http://$ip:8080/"
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" || true)"
      case "$tried" in *"$url"*) ;; *) tried="$tried $url" ;; esac
      if [ "$code" = "200" ]; then
        echo "$url"
        return 0
      fi
    fi
    sleep "$nap"
  done
  echo "reach_site: the site answered $code on$tried" >&2
  return 1
}
