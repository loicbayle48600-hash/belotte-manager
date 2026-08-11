# 🌡️ HACCP Cuisine — EHPAD / FAM

Application HACCP pour tablette Android, conçue pour une cuisine collective (EHPAD, FAM).
C'est une **PWA** (application web progressive) : elle s'installe comme une vraie application, fonctionne **100 % hors ligne** et garde toutes les données **sur la tablette** (aucun serveur, aucun abonnement).

## Fonctionnalités

| Module | Contenu |
|---|---|
| ❄️ **Enceintes froides** | Relevé quotidien du matin (frigos, chambres froides, congélateurs) — conformité automatique, relevés « à la chaîne », enceinte à l'arrêt traçable |
| 🚚 **Réceptions** | Contrôle à la livraison : fournisseur, produit, T°, état — seuils frais ≤ 4 °C / surgelé ≤ −15 °C |
| 📉 **Refroidissement / remise en T°** | Suivi chronométré : +63 → +10 °C en 2 h max, +10 → +63 °C en 1 h max, alerte si délai dépassé |
| 🍽️ **Températures de service** | Liaison chaude ≥ 63 °C, liaison froide ≤ 10 °C, suivi des plats témoins |
| 🍲 **Menu** | Catalogue de plats + menu du jour (midi/soir) ; **import Excel/CSV** du menu à l'année (le format « grille hebdomadaire » de l'établissement — une feuille par semaine — est reconnu automatiquement) ; les plats alimentent les listes de Refroidissement, Remise en T° et Service |
| 🏷️ **Traçabilité étiquettes** | Photo des étiquettes avec l'appareil photo de la tablette (produit, lot, DLC) |
| 🧽 **Plan de nettoyage** | Checklist quotidienne / hebdomadaire / mensuelle, traçabilité date + agent |
| 🍟 **Huiles de friture** | Contrôle visuel, test de composés polaires (≤ 25 %), traçabilité de l'huile usagée (volume, collecteur, bon) |
| ⚠️ **Non-conformités** | Signalement manuel + remontée automatique de tous les relevés non conformes, actions correctives obligatoires |
| 📋 **Historique & export** | Calendrier de complétude (jours vides justifiables par fermeture), recherche de lot multi-registres, **annulation tracée** d'une erreur de saisie (ligne barrée avec motif, jamais de suppression), **exports CSV et PDF** avec en-tête officiel, impression |
| ⚙️ **Réglages** | Équipements, agents, fournisseurs, plan de nettoyage, instruments de mesure personnalisables, **code PIN optionnel** + **sauvegarde/restauration JSON** et **sauvegarde cloud automatique** (Google Drive avec photos en fichiers images, Dropbox, Nextcloud/WebDAV, serveur maison — voir `google-drive/`) |

Chaque enregistrement trace l'**agent**, la **date** et l'**heure**. Toute mesure hors consigne exige une **action corrective** avant enregistrement.

## Installation sur la tablette

### 1. Mettre l'application en ligne (une seule fois)

Le plus simple : **GitHub Pages**.

1. Sur GitHub, ouvrir **Settings → Pages** du dépôt.
2. Dans *Build and deployment*, choisir **Deploy from a branch**, sélectionner la branche principale et le dossier `/ (root)`.
3. Enregistrer : l'application est disponible à l'adresse `https://<compte>.github.io/<dépôt>/`.

(Tout autre hébergement statique fonctionne aussi : Netlify, OVH, serveur de l'établissement…)

### 2. Installer sur la tablette Android

1. Ouvrir **Chrome** sur la tablette et aller à l'adresse de l'application.
2. Menu ⋮ → **« Ajouter à l'écran d'accueil »** (ou « Installer l'application »).
3. L'icône 🌡️ apparaît sur l'écran d'accueil : l'application s'ouvre en plein écran et fonctionne ensuite **sans connexion Internet**.

### 3. Premier démarrage

1. Ouvrir **⚙️ Réglages** : renseigner le nom de l'établissement, la liste des agents, les enceintes froides réelles (avec leurs consignes) et adapter le plan de nettoyage.
2. Sur l'accueil, sélectionner l'**agent en poste** — il sera proposé par défaut dans tous les formulaires.

## Données et sauvegardes

- Les données sont stockées **localement** sur la tablette (IndexedDB). Rien ne sort de l'appareil.
- ⚠️ Ne pas effacer les « données de navigation » de Chrome pour ce site, sous peine de perdre l'historique.
- Penser à faire régulièrement **⚙️ Réglages → Exporter la sauvegarde** (fichier JSON) et à la conserver ailleurs (ordinateur, clé USB, e-mail).
- Pour les contrôles sanitaires : **📋 Historique → Exporter ce registre (CSV)**, ouvrable dans Excel/LibreOffice.

## Conformité au Plan de Maîtrise Sanitaire (PMS)

L'application est **préconfigurée d'après le PMS FAM/EHPAD 2025 de l'établissement** (Cuisine EHPAD Nostr'Oustaou, Grandrieu) — arrêté du 21 décembre 2009 et règlement (CE) n° 852/2004 :

- **Enceintes froides** : les 12 enceintes réelles du PMS (chambres froides négative/fruits-légumes/produits laitiers/viandes, armoires froides, frigo jour, frigo plats témoins, table réfrigérée, frigos économat…). Positif : cible 3 °C, limite critique 6 °C · Négatif : cible −18 °C, tolérance −15 °C. Relevé quotidien en début de journée.
- **Réception** : frais cible 3 °C (limite 6 °C), viandes hachées/abats ≤ 2 °C, contrôle à cœur obligatoire au-delà du seuil, refus > 10 °C ; surgelés ≤ −15 °C ; température obligatoire hors épicerie ; contrôle DLC/étiquetage/emballage ; n° de lot / bon de livraison ; les 13 fournisseurs du PMS préchargés avec leurs jours de livraison.
- **Refroidissement** : +63 → +10 °C en moins de 2 h (pas de tolérance) · **Remise en température** : +10 → +63 °C en moins d'1 h, avec actions correctives du PMS.
- **Service / expédition** : liaison chaude ≥ 63 °C (pas de tolérance) ; liaison froide cible 3 °C, limite 6 °C, tolérée jusqu'à 10 °C si consommation dans les 2 h ; suivi des plats témoins (100 g, 5 jours à 3 °C).
- **Décongélation** : registre dédié (enceinte à 3 °C uniquement, 48 h max, jamais de recongélation) avec alerte de dépassement.
- **Produits entamés** : registre dédié avec les DLC internes du PMS (lait 2-3 j, mayonnaise 3 semaines, IV gamme 1-2 j, charcuterie tranchée 2 j, plats cuisinés 3 j, excédents 1 j…) et alertes de péremption.
- **Nettoyage & désinfection** : les fiches de suivi réelles du PMS par zone (préparation froide, cuisson, légumerie, plonge, office, économat/réception — 57 tâches).
- **Non-conformités** : fiche complète (lieu d'incident, n° de lot, date de péremption, description, action corrective, visa) + remontée automatique de tout relevé hors limites.
- **Équipe HACCP 2025** préchargée (coordinateur : BAYLE Loïc) — chaque enregistrement porte le visa de l'agent.

Tout reste modifiable dans ⚙️ Réglages pour suivre les mises à jour du PMS.

## Technique

- HTML / CSS / JavaScript pur, sans dépendance ni étape de build — ouvrir `index.html` suffit pour développer.
- `sw.js` : service worker (cache hors ligne) · `js/db.js` : stockage IndexedDB · `js/app.js` : modules métier.
- Interface optimisée tablette (paysage et portrait), gros boutons utilisables en cuisine.
