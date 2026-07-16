# Vérification de connexion Fameswap

Petit script qui teste si des identifiants permettent de se connecter à
[app.fameswap.com](https://app.fameswap.com/) et vous dit **réussi** ou **échoué**.

Il utilise un vrai navigateur (Chromium via Playwright) car le site est protégé
(Cloudflare + application JavaScript), ce qu'un simple appel HTTP ne peut pas gérer.

## 1. Installation (une seule fois)

Prérequis : [Node.js](https://nodejs.org/) 18 ou plus récent.

```bash
npm install
```

Cela installe Playwright et télécharge le navigateur Chromium automatiquement.

## 2. Préparer vos identifiants

Copiez le modèle et mettez vos vraies valeurs :

```bash
cp credentials.example.txt credentials.txt
```

Puis éditez `credentials.txt`. Le plus simple, deux lignes :

```
mon.email@exemple.com
monMotDePasse
```

> `credentials.txt` est ignoré par git (voir `.gitignore`) : vos identifiants
> ne partent jamais dans le dépôt.

## 3. Lancer le test

```bash
node check_fameswap_login.mjs credentials.txt
```

Options utiles :

| Option     | Effet                                                        |
|------------|-------------------------------------------------------------|
| `--show`   | Affiche le navigateur (utile pour voir ce qui se passe)     |
| `--debug`  | Écrit `fameswap_debug.png` et `fameswap_debug.html`         |

Exemple :

```bash
node check_fameswap_login.mjs credentials.txt --show --debug
```

## 4. Résultat

Le script affiche clairement le verdict et renvoie un **code de sortie** (pratique
pour automatiser) :

| Code | Signification                                             |
|------|----------------------------------------------------------|
| `0`  | ✅ Connexion réussie                                      |
| `1`  | ❌ Connexion échouée (identifiants refusés)               |
| `2`  | ❔ Indéterminé (site inaccessible, Cloudflare, page inattendue) |
| `3`  | ⚠️ Erreur d'utilisation (fichier manquant / mauvais format) |

## Notes

- **Cloudflare** : si le site présente un challenge anti-bot, le résultat sera
  « indéterminé ». Relancez avec `--show` pour valider manuellement, ou réessayez
  un peu plus tard.
- Si le site change la structure de son formulaire, ajustez les sélecteurs dans
  `check_fameswap_login.mjs` (fonctions autour de `idField` / `pwField`).
  L'option `--debug` fournit le HTML de la page pour repérer les bons champs.
- Ce script n'est prévu que pour tester **vos propres** identifiants.
