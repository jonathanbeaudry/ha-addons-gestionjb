#!/usr/bin/env python3
"""Proxy « maison » — sortie résidentielle pour un serveur distant.

Petit serveur HTTP qui tourne sur un ordi à la maison (Home Assistant OS). Un
serveur distant (IP de centre de données, souvent bloquée par Reddit,
Morningstar…) lui demande d'aller chercher une URL ; la maison la récupère avec
SON IP résidentielle (vue comme « un vrai humain ») et renvoie le contenu.

Chaîne complète :
    serveur distant ──► <hostname public> (Cloudflare Access, jeton de service)
              │  tunnel cloudflared (add-on)
              ▼
        CE serveur (réseau hôte, port 8099)
              │  IP résidentielle
              ▼  Reddit / Morningstar → répondent normalement

DEUX VERBES — l'add-on est une COQUILLE à capacités nommées :
  • `/fetch`  : HTML brut (urllib). Rapide, ~0 Mo de RAM. **Zéro dépendance à
    Chromium** — volontaire : si le navigateur casse, /fetch continue de servir
    Reddit/Morningstar, qui sont en production.
  • `/render` : la page **avec son JavaScript exécuté** (Chromium/Playwright).
    Pour les sites qui n'existent pas sans JS, ou qui exigent un jeton qu'un
    vrai navigateur seul sait produire (ex. `X-Recaptcha-Token` de Waze).

⚠️ JAMAIS d'exécution de code arbitraire. Il serait « pratique » d'accepter un
`?script=` que le VPS enverrait — ce serait une porte d'en arrière dans la
maison. Les verbes sont fixes et lisibles ici. Pour capter un appel réseau de la
page (le cas Waze), `/render` offre `?capture=<motif>`, qui filtre des réponses
XHR — pas un `eval`.

⚠️ SÉCURITÉ — un proxy « va chercher n'importe quoi » est dangereux s'il est
ouvert. Trois verrous, appliqués par `_verrous()` aux DEUX verbes (une seule
copie à corriger) :
  1. jeton porteur `X-Proxy-Token` (en plus du jeton de service Cloudflare) ;
  2. liste blanche de domaines (sauf `general_egress: true`) ;
  3. blocage des IP privées/loopback/réservées (anti-SSRF / DNS-rebinding).

`/fetch` n'utilise que la lib standard. `/render` importe Playwright **en
paresseux** (dans la fonction) pour la même raison qu'au point 1.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import http.cookiejar
import ipaddress
import json
import os
import platform
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_VERSION = "2.10.1"

# UA « navigateur » pour /fetch (urllib, sites type Reddit/Morningstar) : qu'ils
# servent une page normale, pas un blocage API. NON utilisé par /render depuis la
# 2.7.0 — /render présente désormais un vrai Chrome avec sa propre identité (voir
# `_ouvrir_navigateur`).
#
# ⚠️ Historique à ne pas oublier : un essai du 2026-07-15 « UA Chrome cohérent +
# navigateur graphique » a fait 0/7, d'où l'ancien verdict « le levier c'est le
# NOMBRE d'appels, pas le déguisement ». MAIS le test décisif du 2026-07-16 (le
# cell de Jonathan, vrai Chrome, SUR LA MÊME IP maison, passe pendant que le
# Chromium headless se fait jeter) prouve que l'empreinte COMPTE. Réconciliation :
# le 15, l'essai « corrigé » était (a) partiel — pas de vrai Chrome, pas de patch
# webdriver, pas d'écran, pas de stealth — et (b) probablement fait pendant que
# l'IP était déjà en pénitence (bloquée quoi qu'il arrive). La 2.7.0 empile la
# version COMPLÈTE. Reste à vérifier UNE fois, hors pénitence.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)

# Petits patchs JS injectés AVANT tout chargement de page (add_init_script) pour
# gommer les « tells » qu'un moteur d'automatisation laisse et que reCAPTCHA
# Enterprise lit. C'est ce que fait aussi la lib playwright-stealth ; on le garde
# à la main comme socle FIABLE (la lib change d'API selon les versions). Usage
# perso, 1-2x/jour, affichage de données publiques — pas d'évasion de sécurité.
_STEALTH_JS = r"""
(() => {
  // 1) navigator.webdriver : le drapeau nº1 que lit reCAPTCHA (true = piloté).
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  // 2) Un headless annonce 0 plugin ; un vrai navigateur en a. On en simule.
  try { Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]}); } catch (e) {}
  // 3) window.chrome : présent sur un vrai Chrome, absent en automation nue.
  try { if (!window.chrome) { window.chrome = { runtime: {} }; } } catch (e) {}
  // 4) permissions.query « notifications » : un headless répond de façon
  //    incohérente (denied alors que Notification.permission dit default).
  try {
    const q = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) => (
      p && p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : q(p)
    );
  } catch (e) {}
  // 5) Langues cohérentes avec la locale fr-CA du contexte.
  try { Object.defineProperty(navigator, 'languages', {get: () => ['fr-CA', 'fr', 'en-US', 'en']}); } catch (e) {}
})();
"""


def _stealth_lib(ctx, page) -> None:
    """Applique playwright-stealth s'il est installé — best-effort, JAMAIS fatal.
    L'API a changé entre versions (v2 = classe `Stealth` sur le contexte ; v1 =
    `stealth_sync(page)`). On tente les deux et on avale toute erreur : le socle
    `_STEALTH_JS` (add_init_script) reste le garant. « Fait les 3 » = ceci EN PLUS
    du Chrome graphique et des patchs maison, pas à leur place."""
    try:
        from playwright_stealth import Stealth  # v2.x
        try:
            Stealth().apply_stealth_sync(ctx)
            return
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 - lib absente ou API différente
        pass
    try:
        from playwright_stealth import stealth_sync  # v1.x (par page)
        stealth_sync(page)
    except Exception:  # noqa: BLE001
        pass

# --------------------------------------------------------------------------- #
# Configuration (lue depuis /data/options.json en add-on HA, ou l'environnement)
# --------------------------------------------------------------------------- #

def _charger_config() -> dict:
    cfg = {
        "token": os.environ.get("PROXY_TOKEN", ""),
        "allowlist": [],
        "general_egress": os.environ.get("GENERAL_EGRESS", "").lower()
        in ("1", "true", "yes"),
        "port": int(os.environ.get("PORT", "8099")),
        "max_bytes": int(os.environ.get("MAX_BYTES", str(5_000_000))),
        "timeout": int(os.environ.get("TIMEOUT", "25")),
        # /render : Chromium. Coupable par option si jamais il fait des siennes —
        # /fetch (la production) reste debout dans ce cas.
        "render_enabled": os.environ.get("RENDER_ENABLED", "true").lower()
        not in ("0", "false", "no"),
        "render_timeout": int(os.environ.get("RENDER_TIMEOUT", "45")),
        # /navigateur_dire — voir la section du même nom. Conversation vide =
        # service fermé.
        "chatgpt_conversation": "",
        "chatgpt_message": "fait prochaine sur le mcp carcajou",
        "chatgpt_cookies": "",
        "grok_conversation": "https://grok.com/",
        "grok_message": "",
        "grok_cookies": "",
        "navigateur_max_par_jour": 30,
        "navigateur_garder_min": 120,
        "redemarrage_ha": True,
    }
    env_allow = os.environ.get("ALLOWLIST", "")
    if env_allow:
        cfg["allowlist"] = [d.strip().lower() for d in env_allow.split(",") if d.strip()]

    # En add-on Home Assistant, les options de l'UI arrivent ici :
    options_path = os.environ.get("OPTIONS_PATH", "/data/options.json")
    if os.path.exists(options_path):
        try:
            with open(options_path, encoding="utf-8") as f:
                opts = json.load(f)
            if opts.get("token"):
                cfg["token"] = opts["token"]
            if opts.get("allowlist"):
                cfg["allowlist"] = [str(d).strip().lower() for d in opts["allowlist"]]
            if "general_egress" in opts:
                cfg["general_egress"] = bool(opts["general_egress"])
            if opts.get("port"):
                cfg["port"] = int(opts["port"])
            if "render_enabled" in opts:
                cfg["render_enabled"] = bool(opts["render_enabled"])
            if opts.get("render_timeout"):
                cfg["render_timeout"] = int(opts["render_timeout"])
            for cle in ("chatgpt_conversation", "chatgpt_message",
                        "chatgpt_cookies", "grok_conversation",
                        "grok_message", "grok_cookies"):
                if opts.get(cle):
                    cfg[cle] = str(opts[cle]).strip()
            if "redemarrage_ha" in opts:
                cfg["redemarrage_ha"] = bool(opts["redemarrage_ha"])
            for cle in ("navigateur_max_par_jour", "navigateur_garder_min"):
                if opts.get(cle) is not None:
                    cfg[cle] = int(opts[cle])
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"[config] options.json illisible: {e}", file=sys.stderr)

    return cfg


CFG = _charger_config()


# --------------------------------------------------------------------------- #
# Garde-fous
# --------------------------------------------------------------------------- #

def _host_autorise(host: str) -> bool:
    """Le domaine est-il dans la liste blanche (ou egress général activé) ?"""
    if CFG["general_egress"]:
        return True
    host = host.lower()
    for d in CFG["allowlist"]:
        if host == d or host.endswith("." + d):
            return True
    return False


def _ip_publique(host: str) -> tuple[bool, str]:
    """Résout l'hôte et vérifie que TOUTES ses IP sont publiques (anti-SSRF).

    Bloque loopback (127.x, ::1), privé (10.x, 192.168.x, 172.16-31.x, fc00::),
    lien-local (169.254.x, fe80::) et réservé. Empêche d'utiliser le proxy pour
    scanner le réseau maison.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"résolution DNS impossible: {e}"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"IP invalide: {ip_str}"
        if not ip.is_global or ip.is_multicast:
            return False, f"IP non publique refusée: {ip_str}"
    return True, ""


# Cache de résolution pour le garde de Chromium : une page tire des dizaines de
# requêtes, souvent vers 3-4 hôtes. Sans cache, on refait un getaddrinfo à
# chaque image. Vidé à chaque appel de /render (voir _render) : il ne vit que le
# temps d'une page, donc pas de DNS périmé qui traîne.
_CACHE_HOTES: dict[str, bool] = {}


def _hote_sur_pour_navigateur(host: str) -> bool:
    """Version cachée de `_ip_publique`, pour le garde de requêtes de Chromium."""
    if host not in _CACHE_HOTES:
        ok, _ = _ip_publique(host)
        _CACHE_HOTES[host] = ok
    return _CACHE_HOTES[host]


# --------------------------------------------------------------------------- #
# Défi JS de Reddit (« Please wait for verification »)
# --------------------------------------------------------------------------- #

def _solve_challenge(html: str, opener: urllib.request.OpenerDirector) -> None:
    """Résout le petit défi JS de Reddit : solution = seed + seed.

    (Repris du gist Richard-Weiss. Depuis une IP résidentielle le défi n'apparaît
    souvent même pas, mais on le garde pour être robuste.)
    """
    seed_m = re.search(r'\(async e=>e\+e\)\("([0-9a-f]+)"\)', html)
    token_m = re.search(r'name="token"\s+value="([0-9a-f]+)"', html)
    action_m = re.search(r'<form[^>]*action="([^"]+)"', html)
    if not (seed_m and token_m and action_m):
        return
    params = urllib.parse.urlencode({
        "solution": seed_m.group(1) * 2,
        "js_challenge": "1",
        "token": token_m.group(1),
        "jsc_orig_r": "",
    })
    submit_url = "https://www.reddit.com" + action_m.group(1) + "?" + params
    req = urllib.request.Request(submit_url, headers={"User-Agent": BROWSER_UA})
    try:
        opener.open(req, timeout=CFG["timeout"]).read()
    except urllib.error.URLError:
        pass


def _browser_get(url: str, opener, accept: str,
                 referer: str = "", extra: dict | None = None
                 ) -> tuple[int, bytes, str]:
    """GET « navigateur ». Renvoie (status, corps, content_type)."""
    entetes = {
        "User-Agent": BROWSER_UA,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.5",
    }
    # Certaines API « de carte » (ex. Waze live-map) refusent toute requête
    # sans Referer de leur propre site, même d'une IP résidentielle.
    if referer:
        entetes["Referer"] = referer
    # En-têtes sur mesure du VPS (jeton rejoué, Accept exotique…) — après les
    # nôtres, donc ils gagnent. Host et Content-Length restent à urllib.
    for cle, val in (extra or {}).items():
        if cle.lower() not in ("host", "content-length", "connection"):
            entetes[cle] = val
    req = urllib.request.Request(url, headers=entetes)
    try:
        with opener.open(req, timeout=CFG["timeout"]) as resp:
            body = resp.read(CFG["max_bytes"] + 1)
            return resp.status, body, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        # Un 403 porte souvent la page de défi dans son corps : on la renvoie.
        return e.code, e.read(CFG["max_bytes"] + 1), e.headers.get("Content-Type", "")


def _fetch(url: str, referer: str = "",
           extra: dict | None = None) -> tuple[int, bytes, str]:
    """Récupère l'URL. Gère le défi Reddit (warm-up HTML puis .json, même jar)."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    parsed = urllib.parse.urlparse(url)
    est_reddit_json = parsed.netloc.endswith("reddit.com") and ".json" in parsed.path

    if est_reddit_json:
        # Le défi simple est servi sur la page HTML : on gagne les cookies là,
        # puis on frappe le .json avec le même jar (même IP, même clearance).
        html_url = url.split(".json")[0]
        _, page, _ = _browser_get(
            html_url, opener,
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        page_txt = page.decode("utf-8", "replace")
        if "js_challenge" in page_txt or "e=>e+e" in page_txt:
            _solve_challenge(page_txt, opener)
        return _browser_get(url, opener, "application/json, text/plain, */*",
                            referer, extra)

    return _browser_get(
        url, opener,
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        referer, extra)


# --------------------------------------------------------------------------- #
# Le navigateur — partagé par /render et /navigateur_dire
# --------------------------------------------------------------------------- #

# Sans --no-sandbox, Chromium/Chrome refuse de démarrer dans un conteneur
# d'add-on (pas de user namespaces). L'isolation ici, c'est le conteneur
# lui-même. --disable-blink-features=AutomationControlled éteint côté moteur
# le drapeau navigator.webdriver que reCAPTCHA lit.
ARGS_NAV = ["--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"]


def _ouvrir_navigateur(pw, profil: str = ""):
    """(nav, ctx, mode). Voie 1 = vrai Chrome + écran (xvfb) : l'identité
    d'un visiteur normal pour que reCAPTCHA Enterprise NOTE « humain » et
    laisse passer georss (note en tête + mémoire waze-usage-perso). Voie 2
    (repli) = Chromium headless d'origine — pour que /render ne meure JAMAIS
    si Chrome ou l'écran virtuel manquent dans l'image."""
    # DISPLAY présent = run.sh a lancé xvfb → on peut faire du graphique.
    if os.environ.get("DISPLAY"):
        try:
            opts = {"locale": "fr-CA",
                    "viewport": {"width": 1366, "height": 900}}
            # Pas de user_agent forcé : un vrai Chrome présente SON UA et ses
            # client-hints cohérents — moins « menteur » qu'un UA plaqué.
            base = dict(headless=False, channel="chrome", args=ARGS_NAV,
                        ignore_default_args=["--enable-automation"])
            if profil:
                # Profils Chrome à part : un dossier écrit par Chromium puis
                # rouvert par Chrome peut coincer. On repart propre.
                c = pw.chromium.launch_persistent_context(
                    os.path.join("/data/profils_chrome", profil),
                    **base, **opts)
                return c, c, "chrome-graphique"
            n = pw.chromium.launch(**base)
            return n, n.new_context(**opts), "chrome-graphique"
        except Exception as e:  # noqa: BLE001 - Chrome/xvfb absent → repli
            print(f"[render] Chrome graphique indisponible "
                  f"({type(e).__name__}: {e}) — repli Chromium headless",
                  file=sys.stderr)
    # Voie 2 : l'ancien comportement — UA Firefox plaqué, headless.
    opts = {"user_agent": BROWSER_UA, "locale": "fr-CA",
            "viewport": {"width": 1366, "height": 900}}
    if profil:
        # /data = stockage persistant de l'add-on (survit aux mises à jour).
        c = pw.chromium.launch_persistent_context(
            os.path.join("/data/profils", profil),
            headless=True, args=ARGS_NAV, **opts)
        return c, c, "chromium-headless"
    n = pw.chromium.launch(headless=True, args=ARGS_NAV)
    return n, n.new_context(**opts), "chromium-headless"


def _garder_anti_ssrf(ctx, bloquees: list) -> None:
    """⚠️ ANTI-SSRF, 2e étage — indispensable dès qu'un navigateur tourne.
    `_verrous()` ne valide que l'URL DEMANDÉE. Une fois la page ouverte, c'est
    ELLE qui décide quoi charger : images, XHR, iframes, redirections. Un site
    hostile pourrait donc faire tâter 192.168.x à Chromium — un trou qui
    n'existe pas avec /fetch. Ici chaque requête du navigateur est vérifiée,
    pas juste la première. Partagé par /render et /navigateur_dire."""
    def _garde(route, requete) -> None:
        hote = urllib.parse.urlparse(requete.url).hostname or ""
        if hote and not _hote_sur_pour_navigateur(hote):
            if hote not in bloquees:
                bloquees.append(hote)
                print(f"[navigateur] requête bloquée (IP non publique): {hote}",
                      file=sys.stderr)
            route.abort()
            return
        route.continue_()

    ctx.route("**/*", _garde)


# --------------------------------------------------------------------------- #
# /render — la page avec son JavaScript exécuté (Chromium)
# --------------------------------------------------------------------------- #

def _render(url: str, referer: str = "", attendre: str = "",
            selecteur: str = "", capture: str = "", clic: str = "",
            clic_n: int = 1, profil: str = "") -> dict:
    """Ouvre `url` dans Chromium, laisse tourner le JS, rend le DOM final.

    Un navigateur NEUF par appel : plus lent (~1-3 s de démarrage) qu'un
    Chromium persistant, mais aucun état qui fuit d'un appel à l'autre et aucun
    processus zombie si une page part en peanut. À l'échelle d'ici (quelques
    appels à l'heure, pas par seconde), le simple gagne.

    `capture` : motif (sous-chaîne) — les réponses réseau de la page dont l'URL
    contient ce motif sont retenues et renvoyées. C'est ce qui permet de lire
    l'appel XHR que la page fait elle-même, jetons d'en-tête inclus, sans jamais
    exécuter de script fourni par le VPS.

    `clic` + `clic_n` : APRÈS l'attente, cliquer `clic_n` fois (max 15) sur ce
    sélecteur CSS, puis laisser le réseau retomber. C'est ce qui permet de
    dézoomer une carte (le « − » de Waze) pour que la page refasse ses appels
    XHR sur une zone plus large — toujours SES appels, jamais un script à nous.

    `profil` : nom ([a-z0-9_-]) → contexte PERSISTANT sous /data/profils/<nom>.
    Les cookies survivent d'un appel à l'autre : pour les sites qui notent la
    « fraîcheur » du navigateur (reCAPTCHA de Waze), une session habituée passe
    là où un navigateur tout neuf se fait montrer la porte. Sans profil, on
    garde le navigateur jetable — zéro état qui fuit entre deux appels.
    """
    # Import PARESSEUX : /fetch ne doit jamais dépendre de Chromium (voir le
    # docstring en tête). Un add-on sans navigateur sert encore /fetch.
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    ms = CFG["render_timeout"] * 1000
    captees: list[dict] = []
    bloquees: list[str] = []
    _CACHE_HOTES.clear()

    with sync_playwright() as pw:
        nav, ctx, mode_nav = _ouvrir_navigateur(pw, profil)
        try:
            # Socle stealth : injecté AVANT toute navigation, sur chaque page.
            try:
                ctx.add_init_script(_STEALTH_JS)
            except Exception:  # noqa: BLE001
                pass

            _garder_anti_ssrf(ctx, bloquees)
            page = ctx.new_page()
            # playwright-stealth EN PLUS du socle maison (best-effort, jamais
            # fatal) — la 3ᵉ couche de « fait les 3 ».
            _stealth_lib(ctx, page)

            if capture:
                def _sur_reponse(rep) -> None:
                    if capture not in rep.url:
                        return
                    entree = {"url": rep.url, "status": rep.status,
                              "headers": dict(rep.headers),
                              # En-têtes de la REQUÊTE aussi : c'est là que
                              # vivent les jetons négociés par la page
                              # (X-Recaptcha-Token de Waze) — capturés pour
                              # pouvoir REJOUER l'appel avec d'autres
                              # paramètres tant que le jeton vit.
                              "request_headers": dict(rep.request.headers)}
                    try:
                        corps = rep.body()
                        entree["body"] = corps[:CFG["max_bytes"]].decode(
                            "utf-8", "replace")
                    except Exception as e:  # noqa: BLE001 - corps illisible ≠ échec
                        entree["body_error"] = f"{type(e).__name__}: {e}"
                    captees.append(entree)

                page.on("response", _sur_reponse)

            entetes = {"Referer": referer} if referer else {}
            if entetes:
                page.set_extra_http_headers(entetes)

            rep = page.goto(url, wait_until="domcontentloaded", timeout=ms)
            statut = rep.status if rep else 0

            # « Fini de charger » n'existe pas vraiment sur une page moderne :
            # on laisse le choix au VPS plutôt que de deviner.
            attente_ratee = False
            try:
                if selecteur:
                    page.wait_for_selector(selecteur, timeout=ms)
                elif attendre == "networkidle":
                    page.wait_for_load_state("networkidle", timeout=ms)
                elif attendre.isdigit():
                    page.wait_for_timeout(min(int(attendre), 30) * 1000)
            except PWTimeout:
                # L'attente rate ≠ la page est inutile : on rend ce qu'on a, en
                # le disant. Au VPS de juger.
                attente_ratee = True

            # Les clics viennent APRÈS l'attente : la cible (un contrôle de
            # carte, un bouton) n'existe qu'une fois la page construite. 700 ms
            # entre deux clics = le temps d'animation d'un zoom Leaflet ; sans
            # ce répit, les clics s'empilent et la carte n'en applique qu'un.
            # On essaie même si l'attente a raté (la page peut être utilisable
            # quand même) : chaque clic a son propre garde-fou de 5 s.
            if clic:
                for _ in range(max(1, min(clic_n, 15))):
                    try:
                        page.click(clic, timeout=5000)
                    except PWTimeout:
                        break            # cible absente : on rend ce qu'on a
                    page.wait_for_timeout(700)
                # Laisser les XHR déclenchés retomber — BORNÉ à 8 s : une page
                # qui réessaie en boucle (un 403 têtu) n'atteint jamais l'idle
                # et gèlerait tout le render_timeout pour rien.
                try:
                    page.wait_for_load_state("networkidle",
                                             timeout=min(ms, 8000))
                except PWTimeout:
                    pass

            return {"ok": True, "status": statut, "url": page.url,
                    "html": page.content()[:CFG["max_bytes"]],
                    "captures": captees, "attente_ratee": attente_ratee,
                    # Quelle voie a servi (chrome-graphique / chromium-headless) :
                    # au VPS de savoir si l'humanisation a bien pris, sans deviner.
                    "navigateur": mode_nav,
                    # Remonté au VPS : une page amputée de ressources bloquées
                    # doit être explicable, jamais un mystère silencieux.
                    "hotes_bloques": bloquees}
        finally:
            nav.close()


# --------------------------------------------------------------------------- #
# /navigateur_dire et /navigateur_lire — parler à ChatGPT ou à Grok
# --------------------------------------------------------------------------- #
#
# Jonathan, 2026-10-07 :
#  1. « fait prochaine sur le mcp carcajou » tapé chaque jour dans sa
#     conversation ChatGPT (les tâches planifiées de ChatGPT ne le font pas) ;
#  2. puis, le même jour : « fais gérer Grok aussi et permets que ce soit toi
#     [Ti-Coq] qui lances une phrase d'ici ».
#
# Ce que le VPS peut choisir : le SERVICE (liste fermée ci-dessous) et le
# TEXTE. Ce qu'il ne choisit JAMAIS : la conversation (option de l'add-on) ni
# le compte (témoins collés en option par Jonathan). Le texte libre est un
# geste explicite de Jonathan ; avant lui, seul le texte en option partait.
#
# ⚠️ Conditions d'utilisation : OpenAI et xAI n'autorisent pas le pilotage de
# leur site par un robot. Jonathan l'a su avant de trancher (2026-10-07) ; ce
# sont ses comptes. Le plafond `navigateur_max_par_jour` existe pour que ça
# reste un usage d'humain, jamais une boucle.
#
# La connexion : un profil Chrome PERSISTANT par service sous /data, amorcé par
# les témoins exportés de SON Chrome (extension Cookie-Editor → Export JSON) et
# collés dans `<service>_cookies`. Réinjectés seulement quand l'option CHANGE :
# sinon on écraserait la session que le profil a rafraîchie lui-même.

NAV_DIR = "/data"
NAV_JOURNAL = os.path.join(NAV_DIR, "navigateur_journal.jsonl")
TEXTE_MAX = 4000

# Les sélecteurs de chaque site — en un seul endroit : le jour où un site
# change, c'est ici qu'on regarde, et l'échec le dit (capture d'écran rendue).
SERVICES = {
    "chatgpt": {
        "hotes": ("chatgpt.com", "www.chatgpt.com"),
        "domaines_cookies": ("chatgpt.com", "openai.com"),
        "composeur": "#prompt-textarea",
        "envoyer": '[data-testid="send-button"], #composer-submit-button',
        "arreter": '[data-testid="stop-button"]',
        # La dernière réponse a un conteneur à elle ; sinon, repli générique.
        "reponses": '[data-message-author-role="assistant"]',
        "saisie": "",
        "connexion": '[data-testid="login-button"]',
    },
    "grok": {
        "hotes": ("grok.com", "www.grok.com"),
        "domaines_cookies": ("grok.com", "x.ai"),
        "composeur": 'form[data-composer="true"] textarea',
        "envoyer": 'form[data-composer="true"] button[type="submit"]',
        "arreter": ('form[data-composer="true"] button[aria-label*="Arrêter"], '
                    'form[data-composer="true"] button[aria-label*="Stop"]'),
        "reponses": "",
        # Repli générique : la zone de saisie est retirée du texte lu, sinon
        # ses libellés (« Envoyer »…) finissent collés à la réponse.
        "saisie": 'form[data-composer="true"]',
        "connexion": 'a[href*="sign-in"]',
    },
}

_VERROUS = {s: threading.Lock() for s in SERVICES}
_DERNIERE: dict = {}          # service -> dernier état connu de l'échange


def conversation_valide(service: str, url: str) -> bool:
    """Une page du site du service, en https, hôte exact — rien d'autre."""
    p = urllib.parse.urlparse(url or "")
    return p.scheme == "https" and p.hostname in SERVICES[service]["hotes"]


def cookies_pour_playwright(brut: str, domaines: tuple) -> list[dict]:
    """Export Cookie-Editor (liste JSON, ou {"cookies": [...]}) → format
    Playwright. Seuls les témoins des `domaines` passent : un export trop
    large ne doit pas semer d'autres sessions dans ce profil. Lève ValueError
    si l'export est illisible ou ne contient rien d'utile."""
    try:
        data = json.loads(brut)
    except json.JSONDecodeError as e:
        raise ValueError(f"témoins illisibles (pas du JSON) : {e}") from e
    if isinstance(data, dict):
        data = data.get("cookies", [])
    if not isinstance(data, list):
        raise ValueError("les témoins doivent être une liste")
    sites = {"no_restriction": "None", "none": "None", "lax": "Lax",
             "strict": "Strict"}
    sortie = []
    for c in data:
        if not isinstance(c, dict) or not c.get("name") or "value" not in c:
            continue
        domaine = str(c.get("domain", "")).lower()
        hote = domaine.lstrip(".")
        if not any(hote == d or hote.endswith("." + d) for d in domaines):
            continue
        ck = {"name": str(c["name"]), "value": str(c["value"]),
              "domain": domaine, "path": c.get("path") or "/",
              "secure": bool(c.get("secure", True)),
              "httpOnly": bool(c.get("httpOnly", False))}
        expire = c.get("expirationDate") or c.get("expires")
        if isinstance(expire, (int, float)) and expire > 0:
            ck["expires"] = float(expire)
        site = sites.get(str(c.get("sameSite") or "").lower())
        if site:
            ck["sameSite"] = site
            if site == "None":
                ck["secure"] = True
        sortie.append(ck)
    if not sortie:
        raise ValueError(f"aucun témoin de {domaines[0]} dans l'export")
    return sortie


def envois_du_jour(lignes: list, service: str, jour: str) -> int:
    """Combien de phrases ce service a reçues ce jour-là (envois réussis)."""
    n = 0
    for brut in lignes:
        try:
            e = json.loads(brut)
        except (ValueError, TypeError):
            continue
        if e.get("service") == service and e.get("ok") and \
                str(e.get("quand", "")).startswith(jour):
            n += 1
    return n


def reponse_apres(texte_page: str, envoye: str, rang: int, saisie: str = "") -> str:
    """Repli générique : ce qui suit la (rang+1)-ième occurrence de la phrase
    envoyée — NOTRE message, compté avant l'envoi — et précède la zone de
    saisie. Pas « la dernière occurrence » : une réponse qui cite la question
    se faisait couper au milieu (vu au banc)."""
    cle = envoye.strip()[:200]
    if not cle:
        return ""
    i = -1
    for _ in range(rang + 1):
        i = texte_page.find(cle, i + 1)
        if i < 0:
            return ""
    reste = texte_page[i + len(cle):]
    saisie = saisie.strip()
    if saisie:
        j = reste.rfind(saisie)
        if j >= 0:
            reste = reste[:j]
    return reste.strip()


def _journaliser(entree: dict) -> None:
    try:
        with open(NAV_JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(entree, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[navigateur] journal non écrit : {e}", file=sys.stderr)


def _lire_journal() -> list:
    try:
        with open(NAV_JOURNAL, encoding="utf-8") as f:
            return f.readlines()
    except OSError:
        return []


def _etat_cookies(service: str) -> dict:
    try:
        with open(os.path.join(NAV_DIR, f"navigateur_{service}.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _noter_cookies(service: str, empreinte: str) -> None:
    try:
        with open(os.path.join(NAV_DIR, f"navigateur_{service}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"cookies_sha": empreinte}, f)
    except OSError as e:
        print(f"[navigateur] état non écrit : {e}", file=sys.stderr)


def _capture(page) -> str:
    """Capture d'écran JPEG en base64 — pour qu'un échec soit VU, pas deviné."""
    try:
        return base64.b64encode(
            page.screenshot(type="jpeg", quality=45)).decode("ascii")
    except Exception:  # noqa: BLE001 - une capture ratée ne masque pas l'erreur
        return ""


def _texte_de(page, selecteur: str) -> str:
    try:
        return page.locator(selecteur).inner_text(timeout=3000)
    except Exception:  # noqa: BLE001
        return ""


def _lire_reponse(page, sv: dict, envoye: str, avant_n: int, avant_occ: int) -> str:
    if sv["reponses"]:
        loc = page.locator(sv["reponses"])
        n = loc.count()
        if n > avant_n:
            try:
                return loc.nth(n - 1).inner_text(timeout=3000).strip()
            except Exception:  # noqa: BLE001
                pass
        return ""
    saisie = _texte_de(page, sv["saisie"]) if sv["saisie"] else ""
    return reponse_apres(_texte_de(page, "main") or _texte_de(page, "body"),
                         envoye, avant_occ, saisie)


def _session(service: str, texte: str, res: dict, termine: threading.Event) -> None:
    """Tourne dans son propre fil (l'API sync de Playwright est liée au fil qui
    l'ouvre). Met `res` à jour au fil de l'eau ; `termine` = réponse finie ou
    échec. La page reste ouverte tant que le service travaille, au plus
    `navigateur_garder_min` minutes : fermer en plein travail ne doit pas
    pouvoir couper une boucle d'outils de ChatGPT."""
    sv = SERVICES[service]
    quand = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            nav, ctx, mode_nav = _ouvrir_navigateur(pw, service)
            res["navigateur"] = mode_nav
            try:
                try:
                    ctx.add_init_script(_STEALTH_JS)
                except Exception:  # noqa: BLE001
                    pass
                bloquees: list = []
                _garder_anti_ssrf(ctx, bloquees)

                brut = CFG[f"{service}_cookies"]
                empreinte = hashlib.sha256(brut.encode()).hexdigest() if brut else ""
                if brut and empreinte != _etat_cookies(service).get("cookies_sha"):
                    ctx.add_cookies(cookies_pour_playwright(brut, sv["domaines_cookies"]))
                    _noter_cookies(service, empreinte)
                    res["cookies_reinjectes"] = True

                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                _stealth_lib(ctx, page)
                page.goto(CFG[f"{service}_conversation"],
                          wait_until="domcontentloaded", timeout=40_000)
                try:
                    page.wait_for_selector(sv["composeur"], timeout=25_000)
                except PWTimeout:
                    deconnecte = page.locator(sv["connexion"]).count() > 0
                    res.update(ok=False, etape="composeur", url=page.url,
                               capture_jpeg=_capture(page),
                               error=(f"pas connecté à {service} : recoller les "
                                      f"témoins dans l'option {service}_cookies")
                               if deconnecte else
                               "zone de saisie introuvable (le site a changé ?)")
                    return

                cle = texte.strip()[:200]
                avant_occ = (_texte_de(page, "main") or _texte_de(page, "body")).count(cle)
                avant_n = page.locator(sv["reponses"]).count() if sv["reponses"] else 0
                page.click(sv["composeur"])
                page.keyboard.insert_text(texte)
                page.wait_for_timeout(800)
                bouton = page.locator(sv["envoyer"])
                if bouton.count() and bouton.first.is_enabled():
                    bouton.first.click()
                else:
                    page.keyboard.press("Enter")

                # La phrase est partie quand elle apparaît UNE fois de plus dans
                # la page (la zone de saisie, elle, s'est vidée).
                limite_envoi = time.monotonic() + 15
                while time.monotonic() < limite_envoi:
                    page.wait_for_timeout(1000)
                    occ = (_texte_de(page, "main") or _texte_de(page, "body")).count(cle)
                    if occ > avant_occ:
                        break
                else:
                    res.update(ok=False, etape="envoi", url=page.url,
                               capture_jpeg=_capture(page),
                               error="phrase tapée mais jamais apparue dans "
                                     "la conversation")
                    return

                res.update(ok=True, etape="envoye", fini=False, reponse="",
                           url=page.url, hotes_bloques=bloquees)
                _journaliser({"quand": quand, "service": service, "ok": True,
                              "texte": texte[:500]})

                # Fini = bouton « arrêter » absent ET réponse non vide et
                # inchangée deux relevés de suite (entre deux appels d'outils,
                # le bouton disparaît un instant ; la réponse, elle, bouge).
                limite = time.monotonic() + CFG["navigateur_garder_min"] * 60
                debut = time.monotonic()
                precedent, stable = None, 0
                while time.monotonic() < limite:
                    page.wait_for_timeout(3000 if time.monotonic() - debut < 120
                                          else 15000)
                    rep = _lire_reponse(page, sv, texte, avant_n, avant_occ)
                    occupe = page.locator(sv["arreter"]).count() > 0
                    res["reponse"] = rep[:20000]
                    _DERNIERE[service] = {"texte": texte[:500], "reponse": rep[:20000],
                                          "fini": False, "quand": quand}
                    stable = stable + 1 if (rep and rep == precedent and not occupe) else 0
                    precedent = rep
                    if stable >= 2:
                        res["fini"] = True
                        _DERNIERE[service]["fini"] = True
                        break
                termine.set()
                print(f"[navigateur] {service} : page fermée ("
                      + ("réponse finie" if res.get("fini") else "limite atteinte")
                      + ")", file=sys.stderr)
            finally:
                nav.close()
    except Exception as e:  # noqa: BLE001 - l'erreur remonte au VPS, jamais muette
        if "ok" not in res:
            res.update(ok=False, etape="exception",
                       error=f"{type(e).__name__}: {e}")
        print(f"[navigateur] {service} : {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        if not res.get("ok"):
            _journaliser({"quand": quand, "service": service, "ok": False,
                          "texte": texte[:500], "erreur": res.get("error", "")})
        termine.set()
        _VERROUS[service].release()


def navigateur_dire(service: str, texte: str) -> tuple[int, dict]:
    """(code HTTP, corps). 4xx = demande refusée ; 200 = la tentative a eu
    lieu : `ok` dit si la phrase est partie, `fini` si la réponse est complète
    (sinon /navigateur_lire la donnera plus tard)."""
    if service not in SERVICES:
        return 400, {"error": f"service inconnu (permis : {', '.join(SERVICES)})"}
    if not conversation_valide(service, CFG[f"{service}_conversation"]):
        return 412, {"error": f"option {service}_conversation absente ou invalide"}
    texte = (texte or CFG.get(f"{service}_message") or "").strip()
    if not texte:
        return 400, {"error": "texte vide (et aucun message par défaut en option)"}
    if len(texte) > TEXTE_MAX:
        return 413, {"error": f"texte trop long (> {TEXTE_MAX} caractères)"}
    jour = datetime.date.today().isoformat()
    deja = envois_du_jour(_lire_journal(), service, jour)
    if deja >= CFG["navigateur_max_par_jour"]:
        return 429, {"error": f"plafond du jour atteint ({deja} phrases à {service})"}
    if not _VERROUS[service].acquire(blocking=False):
        return 409, {"error": f"{service} travaille encore sur la phrase précédente "
                              "(voir /navigateur_lire)"}
    res: dict = {}
    termine = threading.Event()
    threading.Thread(target=_session, args=(service, texte, res, termine),
                     daemon=True).start()
    # Sous les 100 s de Cloudflare. Pas fini à temps = on rend le partiel.
    termine.wait(timeout=88)
    corps = dict(res)
    if not corps:
        corps = {"ok": False, "etape": "attente", "error": "toujours en cours après 88 s"}
    return 200, corps


def navigateur_lire(service: str) -> tuple[int, dict]:
    """Le dernier échange connu de ce service depuis le démarrage de l'add-on."""
    if service not in SERVICES:
        return 400, {"error": f"service inconnu (permis : {', '.join(SERVICES)})"}
    d = _DERNIERE.get(service)
    if not d:
        return 404, {"error": "aucun échange en mémoire depuis le démarrage"}
    return 200, {"ok": True, "service": service,
                 "en_cours": _VERROUS[service].locked(), **d}


# --------------------------------------------------------------------------- #
# Serveur HTTP
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Relancer Home Assistant à distance (2.10.0)
# --------------------------------------------------------------------------- #
# Jonathan, 2026-10-07 (« go 1 ») : HA est tombé à 10 h 10 pendant qu'il était
# loin, Nabu Casa ET le tunnel morts avec lui, et personne à la maison. Cet
# add-on tourne sous le SUPERVISOR, pas sous HA : il restait debout. Il gagne
# donc le droit de demander au Supervisor de relancer le cœur, ou la machine.
#
# Garde-fous, tous ici (le VPS n'en porte aucun qui compte) :
#   · jeton porteur, comme tous les verbes ;
#   · option `redemarrage_ha` = coupe-circuit ;
#   · REFUS si HA répond encore localement — ce n'est pas un bouton de confort ;
#   · UNE relance par heure au plus (journal sous /data, survit au redémarrage).
# `machine` (reboot de l'hôte) exige `hassio_role: manager` ; `core` suffirait
# avec `homeassistant`. Mesuré nulle part encore : premier vrai essai = la
# prochaine panne.

SUPERVISEUR = "http://supervisor"
HA_LOCAL = "http://127.0.0.1:8123/manifest.json"   # host_network : HA est là
RELANCE_JOURNAL = os.path.join(NAV_DIR, "relances_ha.jsonl")
RELANCE_ECART_S = 3600
RELANCE_CHEMINS = {"core": "/core/restart", "machine": "/host/reboot"}


def ha_repond(timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(HA_LOCAL, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _superviseur(methode: str, chemin: str, timeout: float = 30) -> tuple[int, bytes]:
    jeton = os.environ.get("SUPERVISOR_TOKEN", "")
    if not jeton:
        return 503, b"SUPERVISOR_TOKEN absent (hassio_api off ?)"
    req = urllib.request.Request(SUPERVISEUR + chemin, method=methode,
                                 headers={"Authorization": "Bearer " + jeton})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        return 502, str(e).encode()


def derniere_relance(lignes: list) -> float:
    """L'horodatage (epoch) de la dernière relance LANCÉE, 0 si aucune.
    Une relance que le Supervisor a REFUSÉE (4xx/5xx sauf 502 : un reboot coupe
    la connexion) ne compte pas : on doit pouvoir tenter `machine` juste après."""
    entrees = []
    for brut in lignes:
        try:
            entrees.append(json.loads(brut))
        except ValueError:
            continue
    refusees = {e.get("lancement") for e in entrees
                if isinstance(e.get("resultat"), int) and e["resultat"] >= 400
                and e["resultat"] != 502}
    for e in reversed(entrees):
        if e.get("lancee") and e.get("t") not in refusees:
            return float(e.get("t", 0))
    return 0.0


def _lire_relances() -> list:
    try:
        with open(RELANCE_JOURNAL, encoding="utf-8") as f:
            return f.readlines()
    except OSError:
        return []


def _noter_relance(entree: dict) -> None:
    try:
        with open(RELANCE_JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(entree, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[relance] journal non écrit : {e}", file=sys.stderr)


def ha_etat() -> tuple[int, dict]:
    code, brut = _superviseur("GET", "/core/info", 10)
    info = {}
    if code == 200:
        try:
            info = json.loads(brut).get("data", {})
        except ValueError:
            pass
    return 200, {
        "ok": True,
        "ha_repond": ha_repond(),
        "superviseur": code,
        "core": {k: info.get(k) for k in ("version", "version_latest",
                                          "update_available", "boot",
                                          "watchdog", "state") if k in info},
        "derniere_relance": derniere_relance(_lire_relances()) or None,
    }


def ha_journal(lignes: int) -> tuple[int, dict]:
    code, brut = _superviseur("GET", "/core/logs", 20)
    if code != 200:
        return 502, {"ok": False, "error": f"superviseur {code}: {brut[:300]!r}"}
    texte = brut.decode("utf-8", "replace").splitlines()
    return 200, {"ok": True, "lignes": texte[-max(1, min(lignes, 2000)):]}


def ha_redemarrer(niveau: str, maintenant: float | None = None) -> tuple[int, dict]:
    maintenant = time.time() if maintenant is None else maintenant
    if not CFG["redemarrage_ha"]:
        return 503, {"ok": False, "error": "relance désactivée (option redemarrage_ha)"}
    if niveau not in RELANCE_CHEMINS:
        return 400, {"ok": False, "error": "niveau = core ou machine"}
    if ha_repond():
        return 409, {"ok": False, "error": "HA répond localement : rien à relancer"}
    avant = derniere_relance(_lire_relances())
    if avant and maintenant - avant < RELANCE_ECART_S:
        reste = int(RELANCE_ECART_S - (maintenant - avant)) // 60 + 1
        return 429, {"ok": False, "error": f"une relance par heure — encore {reste} min"}
    _noter_relance({"t": maintenant, "niveau": niveau, "lancee": True})

    # Le Supervisor ne rend la main qu'une fois HA relancé (minutes), et un
    # reboot coupe la réponse : on répond 202 tout de suite, le résultat va
    # au journal.
    def _lancer() -> None:
        code, brut = _superviseur("POST", RELANCE_CHEMINS[niveau], 600)
        _noter_relance({"t": time.time(), "lancement": maintenant,
                        "niveau": niveau, "resultat": code,
                        "detail": brut[:300].decode("utf-8", "replace")})
        print(f"[relance] {niveau} → superviseur {code}", file=sys.stderr)

    threading.Thread(target=_lancer, daemon=True).start()
    return 202, {"ok": True, "lancee": niveau}


class Handler(BaseHTTPRequestHandler):
    server_version = "ProxyMaison/2.1"

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A002 - garder le log court
        sys.stderr.write("[proxy] " + (fmt % args) + "\n")

    def _verrous(self, qs: dict) -> tuple[str, str] | None:
        """Les 3 verrous + la validation d'URL — partagés par /fetch et /render.

        UNE seule copie : un verbe neuf ne peut pas oublier un verrou, et un
        correctif de sécurité se fait ici une fois pour tous les verbes.
        Rend (url, referer), ou None après avoir déjà répondu l'erreur.
        """
        # 1) Jeton porteur
        if not CFG["token"] or self.headers.get("X-Proxy-Token") != CFG["token"]:
            self._json(401, {"error": "jeton invalide"})
            return None

        target = (qs.get("url") or [""])[0]
        if not target:
            self._json(400, {"error": "paramètre ?url= manquant"})
            return None

        referer = (qs.get("referer") or [""])[0]
        if referer and not referer.startswith(("http://", "https://")):
            self._json(400, {"error": "referer invalide (http/https requis)"})
            return None

        p = urllib.parse.urlparse(target)
        if p.scheme not in ("http", "https") or not p.netloc:
            self._json(400, {"error": "URL invalide (http/https requis)"})
            return None

        # 2) Liste blanche
        if not _host_autorise(p.hostname or ""):
            self._json(403, {"error": f"domaine non autorisé: {p.hostname}"})
            return None

        # 3) Anti-SSRF : IP publiques seulement
        ok, why = _ip_publique(p.hostname or "")
        if not ok:
            self._json(403, {"error": why})
            return None

        return target, referer

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/health":
            self._json(200, {
                "ok": True,
                "service": "proxy-maison",
                "version": SERVICE_VERSION,
                # L'arch de la machine maison : la seule façon simple de la
                # savoir depuis le VPS (le proxy Supervisor de HA refuse les
                # jetons longue durée). Dit aussi si Chromium est bien là.
                "arch": platform.machine(),
                "verbes": ["/fetch"] + (["/render", "/navigateur_dire",
                                       "/navigateur_lire"]
                                      if CFG["render_enabled"] else [])
                          + ["/ha_etat", "/ha_journal", "/ha_redemarrer"],
            })
            return

        if parsed.path in ("/ha_etat", "/ha_journal", "/ha_redemarrer"):
            if not CFG["token"] or self.headers.get("X-Proxy-Token") != CFG["token"]:
                self._json(401, {"error": "jeton invalide"})
                return
            if parsed.path == "/ha_etat":
                code, corps = ha_etat()
            elif parsed.path == "/ha_journal":
                n = (qs.get("lignes") or ["200"])[0]
                code, corps = ha_journal(int(n) if n.isdigit() else 200)
            else:
                code, corps = ha_redemarrer((qs.get("niveau") or [""])[0])
            self._json(code, corps)
            return

        if parsed.path == "/render":
            self._route_render(qs)
            return

        if parsed.path in ("/navigateur_dire", "/navigateur_lire"):
            # Pas d'URL ici : seul le jeton porteur s'applique. La conversation
            # est une OPTION de l'add-on, jamais un paramètre (voir la section).
            if not CFG["token"] or self.headers.get("X-Proxy-Token") != CFG["token"]:
                self._json(401, {"error": "jeton invalide"})
                return
            if not CFG["render_enabled"]:
                self._json(503, {"error": "navigateur désactivé (option render_enabled)"})
                return
            service = (qs.get("service") or [""])[0]
            if parsed.path == "/navigateur_lire":
                code, corps = navigateur_lire(service)
            else:
                code, corps = navigateur_dire(service, (qs.get("texte") or [""])[0])
            self._json(code, corps)
            return

        if parsed.path != "/fetch":
            self._json(404, {"error": "route inconnue"})
            return

        verrouille = self._verrous(qs)
        if verrouille is None:
            return
        target, referer = verrouille

        # En-têtes sur mesure (JSON) : rejouer un appel capturé par /render
        # avec son jeton (X-Recaptcha-Token de Waze) mais d'autres paramètres.
        extra = None
        brut = (qs.get("headers") or [""])[0]
        if brut:
            try:
                extra = json.loads(brut)
                assert isinstance(extra, dict)
                extra = {str(k): str(v) for k, v in extra.items()}
            except (json.JSONDecodeError, AssertionError):
                self._json(400, {"error": "headers doit être un objet JSON"})
                return

        try:
            status, body, ctype = _fetch(target, referer, extra)
        except Exception as e:  # noqa: BLE001 - renvoyer une erreur propre au VPS
            self._json(502, {"error": f"échec fetch: {type(e).__name__}: {e}"})
            return

        if len(body) > CFG["max_bytes"]:
            body = body[: CFG["max_bytes"]]
            tronque = "1"
        else:
            tronque = "0"

        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Upstream-Status", str(status))
        self.send_header("X-Truncated", tronque)
        self.end_headers()
        self.wfile.write(body)

    def _route_render(self, qs: dict) -> None:
        """La page avec son JS exécuté. Mêmes verrous que /fetch, via _verrous()."""
        if not CFG["render_enabled"]:
            self._json(503, {"error": "render désactivé (option render_enabled)"})
            return

        verrouille = self._verrous(qs)
        if verrouille is None:
            return
        target, referer = verrouille

        # `profil` finit dans un chemin sous /data : liste blanche stricte,
        # jamais le texte reçu tel quel.
        profil = (qs.get("profil") or [""])[0]
        if profil and not re.fullmatch(r"[a-z0-9_-]{1,32}", profil):
            self._json(400, {"error": "profil invalide ([a-z0-9_-]{1,32})"})
            return

        try:
            clic_n = (qs.get("clic_n") or ["1"])[0]
            res = _render(
                target,
                referer=referer,
                attendre=(qs.get("wait") or [""])[0],
                selecteur=(qs.get("selector") or [""])[0],
                capture=(qs.get("capture") or [""])[0],
                clic=(qs.get("clic") or [""])[0],
                clic_n=int(clic_n) if clic_n.isdigit() else 1,
                profil=profil,
            )
        except ImportError as e:
            # Chromium absent de l'image : /fetch marche encore, on le dit sans
            # faire semblant que tout va bien.
            self._json(501, {"error": f"Playwright indisponible: {e}"})
            return
        except Exception as e:  # noqa: BLE001 - erreur propre au VPS
            self._json(502, {"error": f"échec render: {type(e).__name__}: {e}"})
            return

        self._json(200, res)


def main():
    port = CFG["port"]
    if not CFG["token"]:
        print("[proxy] ⚠️ AUCUN jeton configuré — le service refusera tout.",
              file=sys.stderr)
    mode = "EGRESS GÉNÉRAL" if CFG["general_egress"] else \
        f"liste blanche ({', '.join(CFG['allowlist']) or 'vide'})"
    print(f"[proxy] démarrage sur 0.0.0.0:{port} — {mode}", file=sys.stderr)

    # Dire tout de suite si Chromium est là : le journal de l'add-on est le seul
    # endroit où ça se voit après un « Reconstruire ».
    if CFG["render_enabled"]:
        try:
            import playwright  # noqa: F401
            etat = "prêt"
        except ImportError:
            etat = "⚠️ Playwright ABSENT de l'image — /render répondra 501"
        print(f"[proxy] /render ({platform.machine()}) : {etat}", file=sys.stderr)
    else:
        print("[proxy] /render : désactivé par option", file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
