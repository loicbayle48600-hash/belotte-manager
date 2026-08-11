# ☁️ Sauvegarde automatique sur Google Drive

L'application peut déposer **chaque jour une sauvegarde** de tous tes registres HACCP
dans **ton** Google Drive. Aucune connexion Google n'est demandée dans l'application :
c'est un petit script hébergé sur ton compte Google qui reçoit et range les fichiers.

Configuration en **5 minutes**, une seule fois.

## Étape 1 — Créer le script

1. Va sur **https://script.google.com** (connecté avec le compte Google de l'établissement).
2. Clique sur **Nouveau projet**.
3. Efface le contenu affiché, puis **copie-colle tout le contenu du fichier [`Code.gs`](./Code.gs)**.
4. En haut, donne un nom au projet, par exemple *Sauvegarde HACCP*, puis **enregistre** (icône disquette).

## Étape 2 — Déployer en application web

1. En haut à droite, clique sur **Déployer → Nouveau déploiement**.
2. Clique sur la roue dentée ⚙️ à côté de *Sélectionner le type* → choisis **Application web**.
3. Règle :
   - **Exécuter en tant que** : *Moi* (ton compte)
   - **Qui a accès** : **Tout le monde**
4. Clique sur **Déployer**.
5. Google demande une autorisation : *Autoriser* → choisis ton compte → si un écran « Google n'a pas validé cette application » apparaît, clique **Paramètres avancés → Accéder à … (non sécurisé)** ; c'est normal, c'est **ton propre** script.
6. Copie l'**URL de l'application web** affichée (elle se termine par `/exec`).

> ⚠️ Chaque fois que tu **modifies** le script, refais **Déployer → Gérer les déploiements → Modifier → Nouvelle version**, sinon l'ancienne URL reste inchangée.

## Étape 3 — Renseigner l'application

1. Dans l'appli HACCP, ouvre **⚙️ Réglages → ☁️ Sauvegarde automatique Google Drive**.
2. Colle l'URL (`…/exec`) dans le champ **Adresse du script Google Drive**.
3. Coche **Sauvegarde automatique quotidienne**.
4. Clique **Enregistrer**, puis **☁️ Sauvegarder maintenant** pour tester.

## Ce qui est sauvegardé

**Tout** : réglages, tous les registres **et les photos des étiquettes** (compressées).
Avec beaucoup de photos, le fichier peut atteindre plusieurs Mo — c'est normal, et
les 60 sauvegardes conservées tournent automatiquement.

En plus du fichier JSON, le script dépose chaque photo d'étiquette en **vrai fichier
image**, rangée **par semaine** — la même organisation que le classeur de traçabilité :

```
📁 Sauvegardes HACCP
├── haccp_…_2026-07-13_07-32.json          ← une sauvegarde complète par jour (60 conservées)
├── haccp_…_2026-07-12_07-30.json
├── 📁 Registres PDF                        ← PDF lisible des 30 derniers jours, déposé chaque semaine (12 conservés)
│   └── registres-haccp-30j-2026-07-13.pdf
└── 📁 Photos étiquettes
    ├── 📁 Semaine 32 2026
    │   ├── 📁 lundi
    │   │   └── 2026-08-03_09h12_escalopes-de-dinde_a3f19c42.jpg
    │   ├── 📁 mardi
    │   └── …
    └── 📁 Semaine 33 2026
        └── …
```

Les photos sont classées selon le **jour de destination du produit** (choisi à la
prise de photo), comme le classeur papier. Conservation illimitée (≥ 6 mois
réglementaires) — rien n'est supprimé automatiquement. Chaque photo n'est déposée
qu'une seule fois ; au maximum 100 nouvelles photos par sauvegarde (le reste part
avec la suivante).

> ⚠️ **Si tu avais déjà installé le script** : recopie le nouveau `Code.gs`, puis
> **Déployer → Gérer les déploiements → ✏️ Modifier → Version : Nouvelle version →
> Déployer** (l'URL ne change pas).

## Résultat

- Un dossier **« Sauvegardes HACCP »** apparaît dans ton Google Drive.
- À l'intérieur, un fichier `haccp_…_AAAA-MM-JJ_HH-mm.json` par sauvegarde.
- L'application envoie automatiquement une sauvegarde **une fois par jour**, au premier
  démarrage sur la tablette (si Internet est disponible). Tu peux aussi lancer une
  sauvegarde à tout moment avec le bouton **Sauvegarder maintenant**.
- Les 60 dernières sauvegardes sont conservées ; les plus anciennes passent à la corbeille.

## Restaurer une sauvegarde

En cas de changement de tablette ou de perte de données :

1. Télécharge le fichier `.json` voulu depuis le dossier Drive vers la tablette.
2. Dans l'appli : **⚙️ Réglages → 💾 Sauvegarde locale (fichier) → ⬆️ Restaurer une sauvegarde**.
3. Sélectionne le fichier : les enregistrements et les réglages sont réimportés.

## Confidentialité

Les sauvegardes ne contiennent que tes données HACCP (relevés, menus, agents, réglages).
Elles restent dans **ton** Drive ; l'application ne les envoie nulle part ailleurs.
