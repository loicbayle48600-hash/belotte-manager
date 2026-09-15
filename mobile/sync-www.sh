#!/usr/bin/env bash
# Copie l'application web (racine du dépôt) dans mobile/www pour l'empaquetage Capacitor.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WWW="$HERE/www"

rm -rf "$WWW"
mkdir -p "$WWW"
cp "$ROOT/index.html" "$WWW/"
cp "$ROOT/manifest.webmanifest" "$WWW/"
cp "$ROOT/sw.js" "$WWW/"
cp -r "$ROOT/css" "$WWW/"
cp -r "$ROOT/js" "$WWW/"
cp -r "$ROOT/icons" "$WWW/"
echo "www synchronisé depuis $ROOT"
