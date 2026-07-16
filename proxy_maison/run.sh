#!/usr/bin/env sh
# Lance le proxy maison. fetch_server.py lit sa config depuis /data/options.json
# (rempli par l'UI Home Assistant : jeton, liste blanche, egress général, port).
set -e
# Écran virtuel (xvfb) : reCAPTCHA note « robot » un navigateur SANS écran
# (headless). xvfb-run fournit un vrai DISPLAY → /render peut lancer un Chrome
# GRAPHIQUE (voie 1 de _ouvrir). -a = choisit un numéro d'écran libre tout seul.
# Repli : si xvfb-run manque, on démarre quand même et le code retombe en
# Chromium headless (voie 2) — /render ne meurt jamais pour ça.
if command -v xvfb-run >/dev/null 2>&1; then
  exec xvfb-run -a --server-args="-screen 0 1366x900x24 -ac" python3 /fetch_server.py
fi
exec python3 /fetch_server.py
