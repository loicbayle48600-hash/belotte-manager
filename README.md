# 🌡️ HACCP Cuisine — EHPAD / FAM

Application HACCP pour tablette Android, conçue pour une cuisine collective (EHPAD, FAM).
C'est une **PWA** (application web progressive) : elle s'installe comme une vraie application, fonctionne **100 % hors ligne** et garde toutes les données **sur la tablette** (aucun serveur, aucun abonnement).

## Fonctionnalités

| Module | Contenu |
|---|---|
| ❄️ **Enceintes froides** | Relevés matin/soir des frigos, chambres froides, congélateurs — conformité automatique selon les consignes de chaque équipement |
| 🚚 **Réceptions** | Contrôle à la livraison : fournisseur, produit, T°, état — seuils frais ≤ 4 °C / surgelé ≤ −15 °C |
| 📉 **Refroidissement / remise en T°** | Suivi chronométré : +63 → +10 °C en 2 h max, +10 → +63 °C en 1 h max, alerte si délai dépassé |
| 🍽️ **Températures de service** | Liaison chaude ≥ 63 °C, liaison froide ≤ 10 °C, suivi des plats témoins |
| 🍲 **Menu** | Catalogue de plats + menu du jour (midi/soir) ; **import Excel/CSV** du menu à l'année ; les plats alimentent les listes de Refroidissement, Remise en T° et Service |
| 🏷️ **Traçabilité étiquettes** | Photo des étiquettes avec l'appareil photo de la tablette (produit, lot, DLC) |
| 🧽 **Plan de nettoyage** | Checklist quotidienne / hebdomadaire / mensuelle, traçabilité date + agent |
| 🍟 **Huiles de friture** | Contrôle visuel, filtration, changement |
| ⚠️ **Non-conformités** | Signalement manuel + remontée automatique de tous les relevés non conformes, actions correctives obligatoires |
| 📋 **Historique & export** | Consultation par registre et par période, **export CSV** (Excel) pour les contrôles sanitaires, impression |
| ⚙️ **Réglages** | Équipements, agents, plan de nettoyage, friteuses personnalisables + **sauvegarde/restauration JSON** |

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

## Seuils réglementaires utilisés (GBPH restauration collective)

- Froid positif : 0 à +4 °C · Froid négatif : ≤ −18 °C
- Réception : frais ≤ +4 °C, surgelés ≤ −15 °C (tolérance ponctuelle)
- Liaison chaude ≥ +63 °C · Liaison froide ≤ +10 °C
- Refroidissement rapide : +63 → +10 °C en moins de 2 h
- Remise en température : +10 → +63 °C en moins d'1 h
- Plat témoin : 100 g conservés 5 jours entre 0 et 3 °C

Les consignes de chaque enceinte sont modifiables dans les Réglages pour coller au plan de maîtrise sanitaire (PMS) de l'établissement.

## Technique

- HTML / CSS / JavaScript pur, sans dépendance ni étape de build — ouvrir `index.html` suffit pour développer.
- `sw.js` : service worker (cache hors ligne) · `js/db.js` : stockage IndexedDB · `js/app.js` : modules métier.
- Interface optimisée tablette (paysage et portrait), gros boutons utilisables en cuisine.
