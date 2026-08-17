#!/usr/bin/env sh
# Lance le tunnel maison. tunnel_server.py lit sa config depuis /data/options.json
# (rempli par l'UI Home Assistant : jeton, liste blanche, ports).
set -e
exec python3 /tunnel_server.py
