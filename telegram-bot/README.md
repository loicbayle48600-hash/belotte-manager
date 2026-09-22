# 📨 Bot Telegram — transfert vers un groupe

Tout message reçu par le bot (texte, photo, document, vocal, vidéo, contact,
position…) est **transféré automatiquement dans un groupe Telegram**.
Aucune dépendance npm, aucun compte tiers : uniquement Node.js et l'API
officielle de Telegram.

## 1. Créer le bot et le groupe

1. Dans Telegram, ouvrir **@BotFather** → `/newbot` → choisir un nom et un
   identifiant. BotFather répond avec un **jeton** du style
   `123456789:AAH...` — c'est le mot de passe du bot, à ne jamais publier.
2. Créer (ou choisir) le **groupe de destination** et y **ajouter le bot**.
3. Dans ce groupe, envoyer `/id` : le bot répond avec l'identifiant du groupe
   (un nombre négatif, `-100…`). C'est la valeur à mettre dans le `.env`.
   *(Tant que le bot n'est pas encore lancé, on peut aussi ajouter
   @RawDataBot au groupe pour lire cet identifiant, puis le retirer.)*

> **Pour relayer aussi les messages des groupes** où le bot est présent :
> BotFather → `/mybots` → le bot → *Bot Settings* → *Group Privacy* →
> **Turn off**. Sans cela, Telegram ne lui transmet, dans un groupe, que les
> commandes et les réponses qui lui sont adressées. Les messages privés
> envoyés au bot arrivent toujours, quelle que soit ce réglage.

## 2. Configurer

```bash
cd telegram-bot
cp .env.example .env
# puis éditer .env : jeton + identifiant du groupe
```

Le fichier `.env` est ignoré par git (voir `.gitignore`) : le jeton ne part
jamais dans le dépôt.

## 3. Lancer

```bash
node index.js        # ou : npm start
```

Affichage attendu :

```
Bot connecté : @mon_bot (Mon Bot).
Destination : Équipe cuisine (supergroup, -1001234567890).
Mode : transfert direct. Transport : polling.
Écoute des messages (long polling)…
```

Écrire ensuite au bot en privé : le message apparaît dans le groupe.

Tests de la logique (sans réseau ni jeton) : `npm test`.

## Options (fichier `.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | **obligatoire**, jeton @BotFather |
| `TELEGRAM_GROUP_CHAT_ID` | — | **obligatoire**, identifiant du groupe (`-100…`) ou `@nom_public` |
| `FORWARD_MODE` | `forward` | `forward` : message transféré avec le bandeau « Transféré de … ». `copy` : message recopié sans bandeau, précédé d'un en-tête d'identité |
| `FORWARD_HEADER` | `auto` | `always` : toujours annoncer qui écrit et depuis où. `auto` : seulement en mode copy, sur un message modifié, ou en repli. `never` : jamais |
| `TELEGRAM_GROUP_THREAD_ID` | — | Sujet (topic) du groupe où déposer les messages ; `/id` dans le sujet donne son numéro |
| `ONLY_PRIVATE_CHATS` | `false` | `true` : ne relayer que les conversations privées |
| `IGNORE_BOTS` | `true` | Ignorer les messages des autres bots |
| `FORWARD_EDITS` | `true` | Relayer aussi les messages modifiés après envoi |
| `SILENT_FORWARD` | `false` | Déposer les messages sans notification sonore |
| `TRANSPORT` | `polling` | `polling` (aucune URL publique nécessaire) ou `webhook` |
| `WEBHOOK_URL` / `WEBHOOK_SECRET` / `PORT` | — | Uniquement en mode webhook |

### Quel mode choisir ?

- **`forward` (défaut)** conserve le message d'origine et permet de répondre
  directement à l'expéditeur depuis le groupe. Si l'expéditeur a masqué son
  compte dans les transferts, son nom n'apparaît pas : passer alors
  `FORWARD_HEADER=always`.
- **`copy`** convient quand le groupe ne doit voir que le contenu, sans
  bandeau de transfert. L'en-tête d'identité est ajouté automatiquement.

## Comportements utiles

- **Pas de boucle** : ce qui est écrit *dans* le groupe de destination n'est
  jamais renvoyé.
- **Albums groupés** : plusieurs photos envoyées d'un coup arrivent en un seul
  album, pas en photos éparpillées.
- **Ordre préservé** : les messages sont déposés dans l'ordre de réception.
- **Repli automatique** : si Telegram refuse le transfert direct (contenu
  protégé, réglages de confidentialité), le message est recopié avec son
  en-tête d'origine. Si même la copie échoue, le groupe reçoit un avis plutôt
  que rien.
- **Limites de débit** : les erreurs 429 sont respectées (`retry_after`) et les
  coupures réseau réessayées avec un délai croissant.
- **Commandes** : `/id` (identifiant de la conversation courante, utilisable
  partout) et `/start` (message d'accueil en privé).

## Le faire tourner en permanence

Un bot ne relaie que lorsqu'il tourne. Les messages reçus pendant une coupure
sont rattrapés au redémarrage (Telegram les conserve **24 h** en mode polling).

### systemd (VPS, Raspberry Pi…)

`/etc/systemd/system/telegram-forward-bot.service` :

```ini
[Unit]
Description=Bot Telegram de transfert vers un groupe
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/telegram-bot
ExecStart=/usr/bin/node /opt/telegram-bot/index.js
EnvironmentFile=/opt/telegram-bot/.env
Restart=always
RestartSec=5
User=telegram

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now telegram-forward-bot
journalctl -u telegram-forward-bot -f
```

### Docker

```bash
docker build -t telegram-forward-bot telegram-bot/
docker run -d --restart=always --env-file telegram-bot/.env telegram-forward-bot
```

### Hébergeur web (Render, Railway, Fly.io…)

Renseigner les variables d'environnement dans l'interface de l'hébergeur, puis
passer en mode webhook :

```
TRANSPORT=webhook
WEBHOOK_URL=https://mon-service.example.com/telegram
WEBHOOK_SECRET=une-longue-chaine-aleatoire
```

Le service expose aussi `GET /health` pour les sondes de l'hébergeur.

## Dépannage

| Symptôme | Cause probable |
|---|---|
| `Jeton refusé par Telegram` | `TELEGRAM_BOT_TOKEN` erroné ou révoqué |
| `Groupe de destination inaccessible` | Bot absent du groupe, ou identifiant erroné — envoyer `/id` dans le groupe |
| Rien n'arrive depuis un **groupe** | *Group Privacy* encore activée chez @BotFather |
| `Too Many Requests` répétés | Plus de 20 messages/minute vers un même groupe : c'est la limite de Telegram, le bot patiente et réessaie |
| Le bot ne voit pas les messages d'un **canal** | Il doit être **administrateur** du canal |
