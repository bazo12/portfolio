#!/usr/bin/env bash
# Diagnostic en lecture seule. Cible par défaut : la machine locale.
# Usage : ./diagnostic_reseau.sh [hôte] [port]
set -euo pipefail

HOTE="${1:-127.0.0.1}"
PORT="${2:-443}"

echo "== Résolution =="
getent hosts "$HOTE" || true

echo "== Joignabilité (3 paquets) =="
ping -c 3 -W 2 "$HOTE" || echo "Pas de réponse ICMP (filtrage possible)."

echo "== Port TCP ${PORT} =="
if timeout 3 bash -c "cat < /dev/null > /dev/tcp/${HOTE}/${PORT}" 2>/dev/null; then
  echo "Port ${PORT} ouvert sur ${HOTE}"
else
  echo "Port ${PORT} fermé ou filtré sur ${HOTE}"
fi
