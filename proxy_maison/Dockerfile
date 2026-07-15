# Image de base publique avec Python déjà installé (pas besoin de BUILD_FROM ni
# de build.yaml — Home Assistant ne fournit BUILD_FROM qu'avec un build.yaml).
#
# `slim` (Debian) et NON `alpine` depuis la 2.0.0 : Chromium/Playwright exige la
# glibc, il n'existe pas de build officiel pour la musl d'Alpine. C'est ce qui
# fait passer l'image de ~50 Mo à ~1 Go — la machine maison a 364 Go de libre.
FROM python:3.12-slim

# Chromium hors du HOME : /root n'est pas garanti écrivable dans un add-on.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1

# Version NON épinglée volontairement : le paquet `playwright` et son navigateur
# doivent venir de la même install, sinon Playwright refuse de démarrer
# (« Executable doesn't exist … downloaded by a different version »).
# `--with-deps` tire les libs système de Chromium (fonts, nss, dbus…) — sans
# elles il meurt au lancement.
# xvfb : un écran virtuel, pour lancer Chromium en mode GRAPHIQUE sans écran.
# Ce n'est pas un luxe — reCAPTCHA Enterprise note durement le mode « headless »
# (même le « nouveau »), et c'est ce qui faisait refuser le jeton de Waze. Un
# Chromium headed sous Xvfb est un vrai navigateur, avec son compositeur, son
# GPU logiciel et ses vraies dimensions d'écran.
RUN pip install --no-cache-dir playwright \
 && playwright install --with-deps chromium \
 && apt-get update && apt-get install -y --no-install-recommends xvfb \
 && rm -rf /root/.cache/pip /var/lib/apt/lists/*

COPY fetch_server.py /fetch_server.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
