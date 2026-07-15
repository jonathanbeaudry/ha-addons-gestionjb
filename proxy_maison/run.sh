#!/usr/bin/env sh
# Lance le proxy maison. fetch_server.py lit sa config depuis /data/options.json
# (rempli par l'UI Home Assistant : jeton, liste blanche, egress général, port).
set -e
exec python3 /fetch_server.py
