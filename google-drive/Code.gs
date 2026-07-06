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
    nettoyer_(dossier);

    return json_({ ok: true, fichier: nom });
  } catch (err) {
    return json_({ ok: false, erreur: String(err) });
  }
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
