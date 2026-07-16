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
# `--with-deps` tire les libs système (fonts, nss, dbus…) — sans elles il meurt.
#
# On installe DEUX navigateurs :
#   - `chromium`  : repli headless d'origine (voie 2 de _ouvrir), toujours là.
#   - `chrome`    : le VRAI Google Chrome (channel="chrome", voie 1) — son
#                   empreinte « visiteur normal » est ce qui vise à passer le
#                   score reCAPTCHA de Waze. Va dans /opt/google/chrome (paquet
#                   système), pas /ms-playwright.
# playwright-stealth : patchs d'empreinte supplémentaires (best-effort côté code).
# xvfb : écran virtuel pour lancer Chrome GRAPHIQUE (headless=False) sans écran
#        physique — un navigateur headless se fait flaguer « robot ».
# (+~350 Mo pour Chrome + xvfb ; la machine maison a 364 Go de libre.)
RUN pip install --no-cache-dir playwright playwright-stealth \
 && playwright install --with-deps chromium \
 && ( playwright install --with-deps chrome \
      || echo "[build] Google Chrome indisponible — /render retombera en Chromium headless" ) \
 && apt-get update \
 && apt-get install -y --no-install-recommends xvfb \
 && rm -rf /root/.cache/pip /var/lib/apt/lists/*

COPY fetch_server.py /fetch_server.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
