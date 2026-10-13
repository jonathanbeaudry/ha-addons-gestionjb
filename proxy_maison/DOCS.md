# Proxy maison — sortie résidentielle

Cet add-on va chercher des pages web **avec l'IP résidentielle de la maison**,
pour le VPS. Reddit et Morningstar bloquent les IP de centre de données (« robot »)
mais pas une connexion résidentielle (« vrai humain »).

C'est une **coquille à capacités nommées** : elle expose des verbes précis, jamais
un « exécute ce qu'on te demande ». De nouveaux verbes s'ajoutent par une mise à
jour, sans rien réinstaller.

## Les verbes

| Verbe | Ce que ça fait | Coût |
|---|---|---|
| `/fetch` | Le HTML brut, sans exécuter le JavaScript | rapide, ~0 Mo |
| `/render` | La page **avec son JavaScript exécuté** (Chromium) | ~3-10 s, ~300 Mo le temps de l'appel |
| `/health` | État du service, architecture, verbes actifs | instantané |

`/fetch` ne dépend **pas** de Chromium : si le navigateur casse, Reddit et
Morningstar (la production) continuent d'être servis.

## Options

| Option | Défaut | À quoi ça sert |
|---|---|---|
| `token` | *(vide)* | Jeton porteur. **Sans lui, le service refuse tout.** |
| `allowlist` | reddit.com, morningstar.com | Domaines permis, si `general_egress: false` |
| `general_egress` | `true` | `true` = n'importe quel site. L'anti-SSRF reste actif quand même. |
| `port` | 8099 | Port sur le réseau hôte (visé par l'add-on Cloudflared) |
| `render_enabled` | `true` | Coupe-circuit de Chromium. `false` laisse `/fetch` debout. |
| `render_timeout` | 45 | Secondes max pour rendre une page |

## Sécurité — trois verrous, appliqués aux deux verbes

1. **Jeton porteur** `X-Proxy-Token` (256 bits) : sans lui → 401.
2. **Liste blanche** de domaines (sauf `general_egress: true`) → 403.
3. **Anti-SSRF** : les IP privées / loopback / réservées sont refusées. Le proxy
   ne peut jamais servir à atteindre le réseau maison, egress général ou pas.

Pour `/render`, l'anti-SSRF a **deux étages**, et le second n'est pas optionnel :
valider l'URL demandée ne suffit pas, parce qu'une fois la page ouverte c'est
*elle* qui décide quoi charger (images, XHR, iframes, redirections). Chaque
requête du navigateur est donc vérifiée, pas seulement la première. Ce qui est
refusé remonte dans `hotes_bloques` — jamais bloqué en silence.

## Vérifier que ça marche

Onglet **Journal**, après un démarrage :

```
[proxy] démarrage sur 0.0.0.0:8099 — EGRESS GÉNÉRAL
[proxy] /render (x86_64) : prêt
```

Si la 2ᵉ ligne dit `⚠️ Playwright ABSENT`, le build a échoué : `/fetch` marche
encore, `/render` répondra 501.

## Mise à jour

Le repo pousse une nouvelle `version:` → Home Assistant affiche « Mise à jour
disponible » → un bouton. Pas de copier-coller.

## Les sites connectés (option `sites`, 2.11.0)

Une ligne par site où le VPS doit entrer avec TON compte :

```yaml
sites:
  - url: https://exemple.com/        # une page du site
    cookies: '[ ... ]'               # l'export JSON de Cookie-Editor
    clic: false                      # true = le VPS peut aussi cliquer
```

1. **L'adresse** : n'importe quelle page du site, en `https`. Elle dit quel
   site est visé ; ses sous-domaines suivent.
2. **La connexion** : dans TON navigateur connecté au site, extension
   **Cookie-Editor** → **Export** → **JSON**, et colle le tout dans `cookies`.
   Seuls les témoins de ce site sont retenus. Ils ne sont reposés que si tu
   changes la ligne : le profil Chrome de l'add-on garde ensuite sa session.
3. **Lire ou cliquer** : sans `clic: true`, le VPS lit les pages et ne clique
   nulle part.
4. **Redémarre l'add-on** (les options ne sont lues qu'au démarrage).

Dès lors, `/render` sur une adresse du site s'ouvre connecté. Chaque rendu
connecté laisse une ligne dans `/data/navigateur_journal.jsonl`.

## ChatGPT, Grok et Muse : parler par la maison (`/navigateur_dire`)

Le VPS envoie une phrase ; l'add-on la tape dans la conversation réglée ICI et
rapporte la réponse. Le VPS choisit le service et le texte, jamais la
conversation ni le compte. Ces trois services sont des lignes de `sites` comme
les autres, avec deux particularités :

- `url` est la **conversation** où la phrase est tapée : l'adresse de ta
  conversation avec Carcajou (`https://chatgpt.com/c/...`), ou la racine
  (`https://grok.com/`, `https://muse.ai/`) pour un nouveau fil à chaque phrase.
  Pas de ligne = service fermé.
- `message` est ce qui part quand le VPS n'envoie pas de texte : chaque jour à
  21 h 05, celui de ChatGPT (« fait prochaine sur le mcp carcajou »).

`navigateur_garder_min` (120) borne le temps où la page reste ouverte pendant
qu'un service travaille. Chaque phrase est notée dans
`/data/navigateur_journal.jsonl`. Si une session expire, le VPS t'envoie un
push : recolle les témoins sur sa ligne et redémarre l'add-on.

## Relancer Home Assistant à distance (2.10.0)

Cet add-on tourne sous le Supervisor : il reste debout quand HA tombe. Le VPS
peut donc demander `/ha_redemarrer?niveau=core` (relancer HA) ou `machine`
(redémarrer la machine), et lire `/ha_etat` et `/ha_journal`. La mise à jour
demande l'accès au Supervisor (rôle **manager**) : HA te le montrera en
installant — c'est voulu.

Garde-fous : jeton, **refus si HA répond**, une relance par heure, push sur
ton téléphone avant chaque relance. Interrupteur : option `redemarrage_ha`.
Journal : `/data/relances_ha.jsonl`.
