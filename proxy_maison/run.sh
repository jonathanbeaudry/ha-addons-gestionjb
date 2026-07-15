#!/usr/bin/env sh
# Lance le proxy maison. fetch_server.py lit sa config depuis /data/options.json
# (rempli par l'UI Home Assistant : jeton, liste blanche, egress général, port).
set -e

# Écran virtuel : permet à /render de lancer Chromium en mode GRAPHIQUE sans
# écran physique. Pourquoi ça compte : reCAPTCHA Enterprise note durement tout
# mode « headless » (même le nouveau), et c'est ce qui fait refuser le jeton de
# Waze. Sous Xvfb, Chromium est un vrai navigateur.
#
# Si Xvfb rate, on NE meurt PAS : /render retombe sur headless (fetch_server.py
# ne regarde que DISPLAY) et /fetch — la production — n'a jamais eu besoin de
# navigateur du tout.
if command -v Xvfb > /dev/null 2>&1; then
  Xvfb :99 -screen 0 1366x900x24 -nolisten tcp > /tmp/xvfb.log 2>&1 &
  # Attendre que l'écran existe VRAIMENT : Chromium lancé avant que Xvfb ait
  # fini de s'ouvrir meurt sur « Missing X server ». 20 × 0,25 s = 5 s max.
  i=0
  while [ $i -lt 20 ]; do
    if [ -e /tmp/.X11-unix/X99 ]; then
      export DISPLAY=:99
      echo "[run] écran virtuel :99 prêt — Chromium sera graphique"
      break
    fi
    i=$((i + 1))
    sleep 0.25
  done
  [ -n "$DISPLAY" ] || echo "[run] Xvfb non prêt — Chromium restera headless" >&2
else
  echo "[run] Xvfb absent — Chromium restera headless" >&2
fi

exec python3 /fetch_server.py
