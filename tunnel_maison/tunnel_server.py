#!/usr/bin/env python3
"""Tunnel maison — relaie du TCP dans un WebSocket, pour sortir par l'IP
résidentielle de Jonathan (Québec) SANS ouvrir le moindre port sur le VPS.

POURQUOI CE SERVICE EXISTE
--------------------------
Le VPS est hébergé chez OVH **en France**. Google n'offre pas Antigravity /
Gemini Code Assist dans l'EEE → `agy` répondait « not available in your
location ». Jonathan, lui, vit au Québec où le service EST offert : on fait donc
simplement sortir la requête par sa vraie maison. Ce n'est pas un contournement
géographique, c'est le trafic qui repart d'où l'utilisateur se trouve vraiment.

POURQUOI UN TUNNEL ET PAS `/fetch` DU PROXY MAISON
--------------------------------------------------
`fetch_server.py` sait aller CHERCHER une page (un GET, il rend le HTML). `agy`
a besoin d'autre chose : POST en HTTPS vers googleapis.com, en-têtes d'auth,
réponses potentiellement streamées. Il faut un vrai tunnel d'octets, donc un
service distinct — et surtout **pas** une greffe risquée sur le proxy maison,
dont dépendent la juste valeur Morningstar (/render) et Waze.

POURQUOI DU WEBSOCKET
---------------------
Règle de Jonathan, non négociable : **aucun port ouvert sur le VPS, tout passe
par Cloudflare**. Le VPS ne peut donc rien écouter — c'est LUI qui se connecte,
en sortant, à `tun.gestionjb.ca` (déjà servi par son tunnel cloudflared). Or
cloudflared relaie du HTTP et des WebSockets, mais PAS la méthode CONNECT d'un
proxy classique. Le WebSocket est donc le seul canal bidirectionnel qui traverse
son infra telle qu'elle est.

SÉCURITÉ — CE QUI GARDE LA PORTE
--------------------------------
1. **Jeton** dans un EN-TÊTE (`X-Tunnel-Token`), jamais dans l'URL : une query
   string finit en clair dans les journaux. Leçon déjà payée ailleurs dans ce
   projet ; on ne la repaye pas ici.
2. **Anti-SSRF** : toutes les IP résolues doivent être publiques. Sans ça, le
   VPS (ou quiconque volerait le jeton) pourrait atteindre le réseau LOCAL de la
   maison — caméras, HA, imprimante. C'est la protection qui compte le plus.
3. **Liste blanche de destinations** : par défaut, seulement ce dont `agy` a
   besoin. Le tunnel n'est pas un proxy ouvert.
4. **Ports** : 443 uniquement par défaut.
5. **Échec fermé** : la moindre condition non remplie ferme la connexion.
"""
import asyncio
import hmac
import ipaddress
import json
import logging
import os
import socket
import sys

from websockets.asyncio.server import serve

LOG = logging.getLogger("tunnel")

# /data/options.json est le chemin de Home Assistant. L'override existe pour
# pouvoir ESSAYER le tunnel hors add-on (test de bout en bout sur le VPS avant
# de déployer chez Jonathan) — sans ça, on ne peut valider le protocole qu'en
# production, ce qui est exactement le moment où on ne veut pas de surprise.
CFG_PATH = os.environ.get("TUNNEL_OPTIONS", "/data/options.json")
DEFAUTS = {
    "token": "",
    # Ce dont `agy` a besoin, et rien d'autre. Élargir est un geste conscient,
    # fait dans l'UI de Home Assistant — pas un effet de bord du code.
    "allowlist": [
        "googleapis.com",
        "google.com",
        "gstatic.com",
        "googleusercontent.com",
    ],
    "ports": [443],
    "port": 8100,
    "max_connexions": 24,
    "log_level": "info",
}


def charger_config() -> dict:
    cfg = dict(DEFAUTS)
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if v not in (None, "")})
    except FileNotFoundError:
        LOG.warning("%s absent — valeurs par défaut (dev/local)", CFG_PATH)
    except Exception as e:  # options illisibles = on ne devine pas, on meurt
        sys.exit(f"❌ options illisibles ({CFG_PATH}) : {e}")
    return cfg


CFG = charger_config()


def _hote_autorise(hote: str) -> bool:
    """Le nom demandé doit être dans la liste blanche (ou un sous-domaine)."""
    hote = hote.lower().rstrip(".")
    for permis in CFG["allowlist"]:
        permis = str(permis).lower().rstrip(".")
        if hote == permis or hote.endswith("." + permis):
            return True
    return False


def _resoudre_publique(hote: str, port: int) -> list:
    """Résout le nom et EXIGE que chaque IP soit publique.

    🔒 Le cœur de l'anti-SSRF. On refuse dès qu'UNE seule réponse DNS pointe
    vers du privé/loopback/lien-local/réservé : un nom peut très bien résoudre
    vers plusieurs adresses, et il suffirait d'une pour viser le LAN de la
    maison. On renvoie les adresses DÉJÀ résolues, et c'est sur elles qu'on se
    connecte — sinon un second appel DNS pourrait rendre autre chose que celui
    qu'on vient de valider (DNS rebinding).
    """
    infos = socket.getaddrinfo(hote, port, proto=socket.IPPROTO_TCP)
    if not infos:
        raise ValueError(f"{hote} ne résout vers rien")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise PermissionError(f"{hote} → {ip} n'est pas une IP publique")
    return infos


_actives = 0
_verrou = asyncio.Lock()


async def _pomper_ws_vers_tcp(ws, writer) -> None:
    async for message in ws:
        if isinstance(message, str):        # le canal est binaire, point.
            continue
        writer.write(message)
        await writer.drain()


async def _pomper_tcp_vers_ws(reader, ws) -> None:
    while True:
        octets = await reader.read(65536)
        if not octets:
            return
        await ws.send(octets)


async def poignee(ws) -> None:
    entetes = ws.request.headers
    chemin = ws.request.path.split("?")[0]

    if chemin != "/tunnel":
        await ws.close(code=4004, reason="chemin inconnu")
        return

    # 1) Jeton — comparaison à temps constant, et refus net s'il n'est PAS
    #    configuré (un add-on sans jeton serait un proxy ouvert sur internet).
    attendu = str(CFG.get("token") or "")
    fourni = entetes.get("X-Tunnel-Token", "")
    if not attendu or not hmac.compare_digest(attendu, fourni):
        LOG.warning("jeton refusé")
        await ws.close(code=4001, reason="jeton refusé")
        return

    # 2) Destination demandée
    hote = (entetes.get("X-Tunnel-Host") or "").strip()
    try:
        port = int(entetes.get("X-Tunnel-Port") or 0)
    except ValueError:
        port = 0
    if not hote or port <= 0:
        await ws.close(code=4002, reason="destination absente")
        return

    if port not in [int(p) for p in CFG["ports"]]:
        LOG.warning("port refusé : %s", port)
        await ws.close(code=4003, reason="port refusé")
        return

    if not _hote_autorise(hote):
        LOG.warning("hôte hors liste blanche : %s", hote)
        await ws.close(code=4003, reason="hôte refusé")
        return

    try:
        infos = await asyncio.get_running_loop().run_in_executor(
            None, _resoudre_publique, hote, port)
    except PermissionError as e:
        LOG.warning("ANTI-SSRF : %s", e)
        await ws.close(code=4003, reason="destination privée refusée")
        return
    except Exception as e:
        await ws.close(code=4005, reason=f"DNS: {e}")
        return

    global _actives
    async with _verrou:
        if _actives >= int(CFG["max_connexions"]):
            await ws.close(code=4008, reason="trop de connexions")
            return
        _actives += 1

    reader = writer = None
    try:
        ip = infos[0][4][0]                  # une IP DÉJÀ validée, pas le nom
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=20)
        await ws.send(json.dumps({"ok": True, "hote": hote, "port": port}))
        LOG.info("ouvert %s:%s (%s) — %d active(s)", hote, port, ip, _actives)

        # Les deux sens tournent ensemble ; le premier qui finit ferme l'autre.
        taches = [asyncio.create_task(_pomper_ws_vers_tcp(ws, writer)),
                  asyncio.create_task(_pomper_tcp_vers_ws(reader, ws))]
        _, restants = await asyncio.wait(taches, return_when=asyncio.FIRST_COMPLETED)
        for t in restants:
            t.cancel()
    except Exception as e:
        LOG.warning("échec vers %s:%s — %s", hote, port, e)
        try:
            await ws.send(json.dumps({"ok": False, "error": str(e)}))
        except Exception:
            pass
    finally:
        if writer is not None:
            writer.close()
        async with _verrou:
            _actives -= 1


async def principal() -> None:
    logging.basicConfig(
        level=getattr(logging, str(CFG["log_level"]).upper(), logging.INFO),
        format="[tunnel] %(levelname)s %(message)s")
    if not CFG.get("token"):
        LOG.error("AUCUN JETON configuré — le tunnel refusera TOUT.")
    port = int(CFG["port"])
    LOG.info("écoute sur 0.0.0.0:%s — destinations: %s | ports: %s",
             port, ", ".join(CFG["allowlist"]), CFG["ports"])
    async with serve(poignee, "0.0.0.0", port, max_size=None,
                     ping_interval=20, ping_timeout=20):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(principal())
    except KeyboardInterrupt:
        pass
