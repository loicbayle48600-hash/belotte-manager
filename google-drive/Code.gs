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
 * Enregistre les photos d'étiquettes comme VRAIS fichiers images dans le
 * sous-dossier « Photos étiquettes » (visibles/consultables directement dans
 * Drive). Chaque photo n'est enregistrée qu'une fois : le nom contient une
 * empreinte du contenu, stable même après restauration d'une sauvegarde.
 */
function extrairePhotos_(dossier, data) {
  if (!data.records) return 0;
  var it = dossier.getFoldersByName('Photos étiquettes');
  var sousDossier = it.hasNext() ? it.next() : dossier.createFolder('Photos étiquettes');

  // noms déjà présents (pour ne pas dupliquer d'une sauvegarde à l'autre)
  var existants = {};
  var files = sousDossier.getFiles();
  while (files.hasNext()) existants[files.next().getName()] = true;

  var ajoutees = 0;
  for (var i = 0; i < data.records.length; i++) {
    if (ajoutees >= MAX_PHOTOS_PAR_ENVOI) break;
    var r = data.records[i];
    if (r.type !== 'etiquette' || !r.photo) continue;
    var m = String(r.photo).match(/^data:image\/(jpeg|jpg|png|webp);base64,(.+)$/);
    if (!m) continue;
    var produit = String(r.produit || 'etiquette').replace(/[^\w\-À-ÿ ]+/g, '').trim().replace(/\s+/g, '-').slice(0, 40) || 'etiquette';
    var nom = (r.date || 'sans-date') + '_' + String(r.time || '').replace(':', 'h') + '_' + produit + '_' + empreinte_(m[2]) + '.jpg';
    if (existants[nom]) continue;
    var blob = Utilities.newBlob(Utilities.base64Decode(m[2]), 'image/jpeg', nom);
    sousDossier.createFile(blob);
    existants[nom] = true;
    ajoutees++;
  }
  return ajoutees;
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
