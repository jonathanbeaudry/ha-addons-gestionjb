#!/usr/bin/env sh
# Lance le proxy maison. fetch_server.py lit sa config depuis /data/options.json
# (rempli par l'UI Home Assistant : jeton, liste blanche, egress général, port).
set -e
# Écran virtuel : reCAPTCHA note « robot » un navigateur SANS écran (headless).
# On démarre Xvfb NOUS-MÊMES et on exporte DISPLAY, PLUTÔT que `xvfb-run` — qui
# exige xauth (absent avec --no-install-recommends) et qui, en échouant sous
# `exec`, TUAIT le conteneur (donc /fetch avec : régression vécue en 2.7.0). Ici
# la mise en place de l'écran ne peut JAMAIS empêcher le démarrage : quoi qu'il
# arrive on finit par « exec python3 ». Sans DISPLAY, le code retombe de lui-même
# en Chromium headless (voie 2 de _ouvrir).
if command -v Xvfb >/dev/null 2>&1; then
  Xvfb :99 -screen 0 1366x900x24 -ac -nolisten tcp >/dev/null 2>&1 &
  i=0
  while [ "$i" -lt 20 ]; do
    if [ -S /tmp/.X11-unix/X99 ]; then export DISPLAY=:99; break; fi
    i=$((i + 1)); sleep 0.25
  done
fi
exec python3 /fetch_server.py
