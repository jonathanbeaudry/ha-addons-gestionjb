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
