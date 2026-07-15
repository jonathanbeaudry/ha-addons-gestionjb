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

import http.cookiejar
import ipaddress
import json
import os
import platform
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_VERSION = "2.0.0"

# UA « navigateur » pour que les sites servent une page normale, pas un blocage API.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)

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
                 referer: str = "") -> tuple[int, bytes, str]:
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
    req = urllib.request.Request(url, headers=entetes)
    try:
        with opener.open(req, timeout=CFG["timeout"]) as resp:
            body = resp.read(CFG["max_bytes"] + 1)
            return resp.status, body, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        # Un 403 porte souvent la page de défi dans son corps : on la renvoie.
        return e.code, e.read(CFG["max_bytes"] + 1), e.headers.get("Content-Type", "")


def _fetch(url: str, referer: str = "") -> tuple[int, bytes, str]:
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
                            referer)

    return _browser_get(
        url, opener,
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        referer)


# --------------------------------------------------------------------------- #
# /render — la page avec son JavaScript exécuté (Chromium)
# --------------------------------------------------------------------------- #

def _render(url: str, referer: str = "", attendre: str = "",
            selecteur: str = "", capture: str = "") -> dict:
    """Ouvre `url` dans Chromium, laisse tourner le JS, rend le DOM final.

    Un navigateur NEUF par appel : plus lent (~1-3 s de démarrage) qu'un
    Chromium persistant, mais aucun état qui fuit d'un appel à l'autre et aucun
    processus zombie si une page part en peanut. À l'échelle d'ici (quelques
    appels à l'heure, pas par seconde), le simple gagne.

    `capture` : motif (sous-chaîne) — les réponses réseau de la page dont l'URL
    contient ce motif sont retenues et renvoyées. C'est ce qui permet de lire
    l'appel XHR que la page fait elle-même, jetons d'en-tête inclus, sans jamais
    exécuter de script fourni par le VPS.
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
        nav = pw.chromium.launch(
            headless=True,
            # Sans --no-sandbox, Chromium refuse de démarrer dans un conteneur
            # d'add-on (pas de user namespaces). L'isolation ici, c'est le
            # conteneur lui-même, pas le bac à sable de Chromium.
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            ctx = nav.new_context(
                user_agent=BROWSER_UA,
                locale="fr-CA",
                viewport={"width": 1366, "height": 900},
            )

            # ⚠️ ANTI-SSRF, 2e étage — indispensable et propre à /render.
            # `_verrous()` ne valide que l'URL DEMANDÉE. Une fois la page
            # ouverte, c'est ELLE qui décide quoi charger : images, XHR,
            # iframes, redirections. Un site hostile pourrait donc faire tâter
            # 192.168.x à Chromium — un trou qui n'existe pas avec /fetch.
            # Ici chaque requête du navigateur est vérifiée, pas juste la
            # première.
            def _garde(route, requete) -> None:
                hote = urllib.parse.urlparse(requete.url).hostname or ""
                if hote and not _hote_sur_pour_navigateur(hote):
                    if hote not in bloquees:
                        bloquees.append(hote)
                        print(f"[render] requête bloquée (IP non publique): {hote}",
                              file=sys.stderr)
                    route.abort()
                    return
                route.continue_()

            ctx.route("**/*", _garde)
            page = ctx.new_page()

            if capture:
                def _sur_reponse(rep) -> None:
                    if capture not in rep.url:
                        return
                    entree = {"url": rep.url, "status": rep.status,
                              "headers": dict(rep.headers)}
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
                return {"ok": True, "status": statut, "url": page.url,
                        "html": page.content()[:CFG["max_bytes"]],
                        "captures": captees, "attente_ratee": True,
                        "hotes_bloques": bloquees}

            return {"ok": True, "status": statut, "url": page.url,
                    "html": page.content()[:CFG["max_bytes"]],
                    "captures": captees, "attente_ratee": False,
                    # Remonté au VPS : une page amputée de ressources bloquées
                    # doit être explicable, jamais un mystère silencieux.
                    "hotes_bloques": bloquees}
        finally:
            nav.close()


# --------------------------------------------------------------------------- #
# Serveur HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "ProxyMaison/2.0"

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
                "verbes": ["/fetch"] + (["/render"] if CFG["render_enabled"] else []),
            })
            return

        if parsed.path == "/render":
            self._route_render(qs)
            return

        if parsed.path != "/fetch":
            self._json(404, {"error": "route inconnue"})
            return

        verrouille = self._verrous(qs)
        if verrouille is None:
            return
        target, referer = verrouille

        try:
            status, body, ctype = _fetch(target, referer)
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

        try:
            res = _render(
                target,
                referer=referer,
                attendre=(qs.get("wait") or [""])[0],
                selecteur=(qs.get("selector") or [""])[0],
                capture=(qs.get("capture") or [""])[0],
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
