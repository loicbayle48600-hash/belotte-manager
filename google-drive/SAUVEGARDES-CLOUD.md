# ☁️ Sauvegardes cloud : Dropbox, Nextcloud/WebDAV, serveur maison

En plus de **Google Drive** (voir `GOOGLE-DRIVE.md`), l'application peut envoyer la
sauvegarde quotidienne vers d'autres destinations. Tu peux en activer **plusieurs à la
fois** : la sauvegarde automatique part alors vers toutes.

Chaque sauvegarde est un fichier `sauvegarde-haccp-AAAA-MM-JJ.json` complet
(réglages + tous les registres + photos des étiquettes en compressé). Une seule
sauvegarde par jour est conservée par destination (le fichier du jour est remplacé).

> ℹ️ Seul **Google Drive** dépose en plus les photos en vrais fichiers images
> (dossier « Photos étiquettes »), grâce à son script.

---

## 🔵 Dropbox (5 minutes)

1. Va sur **https://www.dropbox.com/developers/apps** (connecté à ton compte).
2. **Create app** → choisis **Scoped access** → **App folder** (l'appli n'aura accès
   qu'à son propre dossier) → donne un nom, ex. *HACCP-Cuisine* → **Create app**.
3. Onglet **Permissions** : coche `files.content.write` → **Submit**.
4. Onglet **Settings** → section *OAuth 2* → **Generated access token** → **Generate**.
   ⚠️ Choisis d'abord *Access token expiration* : **No expiration** si proposé.
5. Copie le jeton (commence par `sl.`) et colle-le dans
   **⚙️ Réglages → Sauvegardes cloud → Dropbox**, coche la case, **Enregistrer**,
   puis **Sauvegarder maintenant** pour tester.

Les sauvegardes arrivent dans `Applications/HACCP-Cuisine/` de ton Dropbox.

> Si Dropbox ne propose que des jetons à expiration courte (4 h), il faudra le
> regénérer de temps en temps — l'application t'alerte sur l'accueil si la
> sauvegarde échoue plus de 3 jours.

---

## 🟠 Nextcloud / ownCloud / WebDAV (5 minutes)

Fonctionne avec n'importe quel serveur WebDAV : Nextcloud, ownCloud, kDrive
(Infomaniak), Synology, etc.

1. Dans ton cloud, crée un dossier, ex. `HACCP`.
2. Crée un **mot de passe d'application** (Nextcloud : *Paramètres → Sécurité →
   Mots de passe d'application*) — n'utilise pas ton mot de passe principal.
3. L'URL WebDAV du dossier ressemble à :
   - Nextcloud : `https://toncloud.fr/remote.php/dav/files/TON_UTILISATEUR/HACCP/`
   - ownCloud : `https://toncloud.fr/remote.php/webdav/HACCP/`
4. Renseigne URL + utilisateur + mot de passe d'application dans
   **⚙️ Réglages → Sauvegardes cloud → Nextcloud/WebDAV**, coche la case,
   **Enregistrer** puis **Sauvegarder maintenant**.

> 💡 Depuis l'**APK Android**, ça fonctionne directement. Si tu utilises l'appli en
> version navigateur (PWA), le serveur doit autoriser les requêtes cross-origin
> (CORS) — sur Nextcloud, c'est le cas avec l'app « WebAppPassword » ou un réglage
> de ton reverse-proxy.

---

## ⚫ Serveur maison (HTTP POST)

L'application envoie la sauvegarde en **POST** (corps JSON) à l'adresse que tu donnes.
Toute réponse HTTP 200 = succès. Exemple de récepteur PHP à déposer sur ton serveur :

```php
<?php
// haccp-backup.php — reçoit et range les sauvegardes de l'application HACCP.
$dossier = __DIR__ . '/sauvegardes-haccp';
if (!is_dir($dossier)) mkdir($dossier, 0770, true);

$contenu = file_get_contents('php://input');
$data = json_decode($contenu, true);
if (!$data || ($data['app'] ?? '') !== 'haccp-cuisine') {
  http_response_code(400);
  exit('sauvegarde invalide');
}

$nom = 'haccp_' . date('Y-m-d_H-i') . '.json';
file_put_contents($dossier . '/' . $nom, $contenu);

// conserver les 60 plus récentes
$fichiers = glob($dossier . '/haccp_*.json');
rsort($fichiers);
foreach (array_slice($fichiers, 60) as $vieux) unlink($vieux);

header('Content-Type: application/json');
echo json_encode(['ok' => true, 'fichier' => $nom]);
```

Colle l'adresse (ex. `https://mon-serveur.fr/haccp-backup.php`) dans
**⚙️ Réglages → Sauvegardes cloud → Serveur maison**, coche la case et teste.

> 🔒 Conseils : mets ce fichier derrière HTTPS, et si le serveur est exposé sur
> Internet, ajoute un secret dans l'URL (ex. `haccp-backup.php?cle=UnLongSecret`)
> et vérifie `$_GET['cle']` dans le script.

---

## Restaurer une sauvegarde (toutes destinations)

1. Récupère le fichier `.json` depuis ton cloud/serveur sur la tablette.
2. **⚙️ Réglages → 💾 Sauvegarde locale → ⬆️ Restaurer une sauvegarde**.
3. Registres, réglages et photos sont réimportés.
