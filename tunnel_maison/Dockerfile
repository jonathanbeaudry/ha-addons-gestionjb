# Alpine suffit : ce service n'a PAS besoin de Chromium (contrairement au proxy
# maison, qui pèse ~1 Go pour cette raison). Ici l'image fait quelques dizaines
# de Mo, et le build est quasi instantané.
#
# ⚠️ FROM en dur, PAS ${BUILD_FROM} : Home Assistant ne fournit BUILD_FROM
# qu'avec un build.yaml, et sans lui le build échoue sur « base name should not
# be blank ». Piège déjà payé sur l'add-on proxy_maison.
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1

# Version ÉPINGLÉE : l'API de `websockets` a changé de forme (websockets.serve
# hérité vs websockets.asyncio.server). Le code utilise l'API moderne ; laisser
# la version flotter, c'est accepter que l'add-on casse tout seul un matin.
RUN pip install --no-cache-dir "websockets==16.1" \
 && rm -rf /root/.cache/pip

COPY tunnel_server.py /tunnel_server.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
