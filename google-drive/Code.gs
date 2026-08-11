/**
 * Sauvegarde HACCP → Google Drive
 * ---------------------------------
 * Ce script reçoit les sauvegardes envoyées par l'application HACCP et les
 * enregistre dans un dossier de TON Google Drive. Il s'exécute avec ton compte,
 * donc aucune connexion Google n'est demandée dans l'application.
 *
 * Installation : voir la notice GOOGLE-DRIVE.md (5 minutes).
 */

// Nom du dossier créé dans ton Drive pour ranger les sauvegardes.
var DOSSIER = 'Sauvegardes HACCP';

// Nombre de sauvegardes à conserver (les plus anciennes sont supprimées).
var MAX_FICHIERS = 60;

function doPost(e) {
  try {
    var contenu = (e && e.postData && e.postData.contents) ? e.postData.contents : '{}';
    var data = JSON.parse(contenu);

    // Dépôt hebdomadaire du PDF lisible des registres (en plus de la sauvegarde JSON)
    if (data && data.type === 'pdf' && data.data) {
      return recevoirPdf_(getDossier_(), data);
    }

    var etab = (data.etablissement || 'cuisine').toString().replace(/[^\w\-À-ÿ ]+/g, '').trim() || 'cuisine';
    var horodatage = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd_HH-mm');
    var nom = 'haccp_' + etab.replace(/\s+/g, '-') + '_' + horodatage + '.json';

    var dossier = getDossier_();
    dossier.createFile(nom, contenu, 'application/json');
    var photos = extrairePhotos_(dossier, data);
    nettoyer_(dossier);

    return json_({ ok: true, fichier: nom, photos: photos });
  } catch (err) {
    return json_({ ok: false, erreur: String(err) });
  }
}

// Nombre maximal de photos écrites par exécution : Apps Script est limité à
// ~6 minutes ; le reliquat éventuel part avec la sauvegarde suivante.
var MAX_PHOTOS_PAR_ENVOI = 100;

/**
 * Enregistre les photos d'étiquettes comme VRAIS fichiers images, rangées
 * comme le classeur : « Photos étiquettes / Semaine 33 2026 / lundi / … ».
 * Le classement suit le JOUR DE DESTINATION du produit (champ choisi à la
 * prise de photo), sinon le jour de la photo. Chaque photo n'est enregistrée
 * qu'une fois : le nom contient une empreinte du contenu, stable même après
 * restauration d'une sauvegarde.
 */
var JOURS_FR = ['dimanche', 'lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi'];

function extrairePhotos_(dossier, data) {
  if (!data.records) return 0;
  var it = dossier.getFoldersByName('Photos étiquettes');
  var racinePhotos = it.hasNext() ? it.next() : dossier.createFolder('Photos étiquettes');

  // caches par chemin (semaine puis jour) : dossier + noms déjà présents
  var dossiers = {};   // "Semaine 33 2026/lundi" -> Folder
  var existants = {};  // même clé -> { nomFichier: true }

  function sousDossier_(parent, nom) {
    var itS = parent.getFoldersByName(nom);
    return itS.hasNext() ? itS.next() : parent.createFolder(nom);
  }

  function dossierDuJour(dateISO) {
    var d = parseDate_(dateISO);
    var nomSemaine = d ? 'Semaine ' + numSemaineISO_(d) + ' ' + anneeSemaineISO_(d) : 'Semaine inconnue';
    var nomJour = d ? JOURS_FR[d.getDay()] : 'jour-inconnu';
    var cle = nomSemaine + '/' + nomJour;
    if (!dossiers[cle]) {
      var semaine = sousDossier_(racinePhotos, nomSemaine);
      var jour = sousDossier_(semaine, nomJour);
      dossiers[cle] = jour;
      var noms = {};
      var files = jour.getFiles();
      while (files.hasNext()) noms[files.next().getName()] = true;
      existants[cle] = noms;
    }
    return { dossier: dossiers[cle], noms: existants[cle] };
  }

  // Les photos de plus de 60 jours sont déposées depuis longtemps : les
  // ignorer évite de relister des dizaines de dossiers à chaque sauvegarde
  // (limite d'exécution Apps Script ~6 min).
  var limite = new Date();
  limite.setDate(limite.getDate() - 60);
  var limiteISO = limite.getFullYear() + '-' + ('0' + (limite.getMonth() + 1)).slice(-2) + '-' + ('0' + limite.getDate()).slice(-2);

  var ajoutees = 0;
  for (var i = 0; i < data.records.length; i++) {
    if (ajoutees >= MAX_PHOTOS_PAR_ENVOI) break;
    var r = data.records[i];
    if (r.type !== 'etiquette' || !r.photo) continue;
    if ((r.destineLe || r.date || '') < limiteISO) continue;
    var m = String(r.photo).match(/^data:image\/(jpeg|jpg|png|webp);base64,(.+)$/);
    if (!m) continue;
    var produit = String(r.produit || 'etiquette').replace(/[^\w\-À-ÿ ]+/g, '').trim().replace(/\s+/g, '-').slice(0, 40) || 'etiquette';
    var nom = (r.date || 'sans-date') + '_' + String(r.time || '').replace(':', 'h') + '_' + produit + '_' + empreinte_(m[2]) + '.jpg';
    var cible = dossierDuJour(r.destineLe || r.date || '');
    if (cible.noms[nom]) continue;
    var blob = Utilities.newBlob(Utilities.base64Decode(m[2]), 'image/jpeg', nom);
    cible.dossier.createFile(blob);
    cible.noms[nom] = true;
    ajoutees++;
  }
  return ajoutees;
}

// Date AAAA-MM-JJ -> Date (midi local), ou null.
function parseDate_(dateISO) {
  var m = String(dateISO).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12, 0, 0);
}

// Numéro de semaine ISO (1-53) et année ISO correspondante.
function numSemaineISO_(d) {
  var j = new Date(d.getTime());
  j.setDate(j.getDate() + 3 - ((j.getDay() + 6) % 7)); // jeudi de la semaine
  var jan4 = new Date(j.getFullYear(), 0, 4, 12);
  return 1 + Math.round(((j - jan4) / 86400000 - 3 + ((jan4.getDay() + 6) % 7)) / 7);
}
function anneeSemaineISO_(d) {
  var j = new Date(d.getTime());
  j.setDate(j.getDate() + 3 - ((j.getDay() + 6) % 7));
  return j.getFullYear();
}

/**
 * Range le PDF hebdomadaire des registres dans « Registres PDF »
 * (les 12 plus récents sont conservés).
 */
function recevoirPdf_(dossier, data) {
  var it = dossier.getFoldersByName('Registres PDF');
  var sousDossier = it.hasNext() ? it.next() : dossier.createFolder('Registres PDF');
  var nom = String(data.filename || 'registres-haccp.pdf').replace(/[^\w\-À-ÿ .]+/g, '').slice(0, 80) || 'registres-haccp.pdf';
  var blob = Utilities.newBlob(Utilities.base64Decode(data.data), 'application/pdf', nom);
  sousDossier.createFile(blob);

  var fichiers = [];
  var files = sousDossier.getFiles();
  while (files.hasNext()) fichiers.push(files.next());
  fichiers.sort(function (a, b) { return b.getDateCreated() - a.getDateCreated(); });
  for (var i = 12; i < fichiers.length; i++) fichiers[i].setTrashed(true);

  return json_({ ok: true, fichier: nom });
}

// Empreinte courte (8 hexa) du contenu d'une photo, indépendante des ids.
function empreinte_(base64) {
  var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, base64);
  var hex = '';
  for (var i = 0; i < 4; i++) {
    var v = (digest[i] + 256) % 256;
    hex += ('0' + v.toString(16)).slice(-2);
  }
  return hex;
}

// Permet de vérifier que le script répond (ouverture de l'URL dans un navigateur).
function doGet() {
  return json_({ ok: true, message: 'Service de sauvegarde HACCP actif.' });
}

function getDossier_() {
  var it = DriveApp.getFoldersByName(DOSSIER);
  return it.hasNext() ? it.next() : DriveApp.createFolder(DOSSIER);
}

// Conserve seulement les MAX_FICHIERS sauvegardes les plus récentes.
function nettoyer_(dossier) {
  var fichiers = [];
  var it = dossier.getFiles();
  while (it.hasNext()) fichiers.push(it.next());
  if (fichiers.length <= MAX_FICHIERS) return;
  fichiers.sort(function (a, b) { return b.getDateCreated() - a.getDateCreated(); });
  for (var i = MAX_FICHIERS; i < fichiers.length; i++) fichiers[i].setTrashed(true);
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
