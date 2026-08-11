/* ===== HACCP Cuisine — application =====
 * Seuils réglementaires (GBPH restauration collective) :
 *  - Froid positif : 0 à +4 °C  |  Froid négatif : ≤ −18 °C
 *  - Réception produits frais ≤ +4 °C ; surgelés ≤ −15 °C (tolérance ponctuelle)
 *  - Liaison chaude ≥ +63 °C  |  Liaison froide ≤ +10 °C
 *  - Refroidissement rapide : +63 °C → +10 °C en moins de 2 h
 *  - Remise en température : +10 °C → +63 °C en moins d'1 h
 */
/* Seuils issus du PMS de l'établissement (PR suivi T°C stockage, IN accept/refus,
 * PR suivi temp service, PR refroidissement/réchauffage — arrêté du 21/12/2009). */
const RULES = {
  fraisMax: 6,        // réception frais : tolérance jusqu'à 6 °C (cible 3) ; >10 °C = refus
  fraisRefus: 10,
  hacheMax: 2,        // viandes hachées ≤ 2 °C (abats ≤ 3 °C) — arrêté du 21/12/2009
  surgeleMax: -15,    // réception surgelés : tolérance jusqu'à −15 °C (cible −18)
  chaudMin: 63,       // liaison chaude : ≥ 63 °C, pas de tolérance
  froidCible: 3,      // liaison froide : cible 3 °C
  froidLimite: 6,     // limite 6 °C…
  froidMax: 10,       // …jusqu'à 10 °C si consommation dans les 2 heures
  refroidTarget: 10,  // refroidissement : 63 → 10 °C en moins de 2 h
  refroidMinutes: 120,
  remiseTarget: 63,   // remise en température : 10 → 63 °C en moins d'1 h
  remiseMinutes: 60,
  decongelHeures: 48, // décongélation : à 3 °C en enceinte, utiliser sous 48 h
};

/* Préconfiguration issue du PMS FAM/EHPAD Nostr'Oustaou (Grandrieu) — tout reste modifiable dans Réglages. */
const POS = { type: 'positif', min: 0, max: 6, cible: 3 };    // cible 3 °C, limite critique 6 °C
const NEG = { type: 'negatif', min: -30, max: -15, cible: -18 }; // cible −18 °C, tolérance −15 °C

const DEFAULT_SETTINGS = {
  etablissement: 'Cuisine EHPAD Nostr\'Oustaou — Grandrieu',
  agents: [
    'BAYLE Loïc',
    'CARDONA Andrée-Noëlle',
    'PLANCHON Roselyne',
    'BERINGUER Benoit',
    'MALIGE-MAURIN Ludivine',
    'FERNANDEZ Sylvan',
  ],
  plats: [],
  driveUrl: '',
  driveAuto: false,
  dropbox: { on: false, token: '' },
  webdav: { on: false, url: '', user: '', pass: '' },
  httpPost: { on: false, url: '' },
  pin: '', // vide = Réglages non protégés
  equipements: [
    { id: 'e1', name: 'Chambre froide négative', ...NEG },
    { id: 'e2', name: 'Chambre froide fruits et légumes', ...POS },
    { id: 'e3', name: 'Chambre froide produits laitiers (BOF)', ...POS },
    { id: 'e4', name: 'Chambre froide viandes', ...POS },
    { id: 'e5', name: 'Armoire froide double porte', ...POS },
    { id: 'e6', name: 'Armoire froide produits finis', ...POS },
    { id: 'e7', name: 'Frigo jour (zone cuisson)', ...POS },
    { id: 'e8', name: 'Frigo plats témoins', ...POS },
    { id: 'e9', name: 'Table réfrigérée (office)', ...POS },
    { id: 'e10', name: 'Frigo légumes (économat)', ...POS },
    { id: 'e11', name: 'Frigo B.O.F (économat)', ...POS },
    { id: 'e12', name: 'Congélateur (réception)', ...NEG },
  ],
  friteuses: ['Friteuse'],
  thermometres: ['Thermomètre à sonde', 'Thermomètre infrarouge (réception)'],
  fournisseurs: [
    { name: 'Languedoc Lozère Viande', produits: 'Viandes fraîches sous vide', jours: 'Mercredi' },
    { name: 'Saveurs d\'Antoine', produits: 'Charcuterie, produits frais', jours: 'Mardi' },
    { name: 'Charrade Marcel', produits: 'Fromages, lait, produits laitiers, œufs', jours: 'Jeudi' },
    { name: 'Gel 43', produits: 'Produits surgelés', jours: 'Mercredi' },
    { name: 'Pro à Pro', produits: 'Épicerie', jours: 'Lundi (tous les 15 jours)' },
    { name: 'EuroFruit', produits: 'Fruits et légumes', jours: 'Lundi et jeudi' },
    { name: 'Volailles Vey', produits: 'Volailles', jours: 'Mercredi' },
    { name: 'Boulangerie Falcon', produits: 'Pain', jours: 'Tous les jours' },
    { name: 'Café Chapuis', produits: 'Café, confiture, biscuits', jours: 'Mensuel' },
    { name: 'Bonnet Hygiène', produits: 'Produits d\'entretien', jours: 'Toutes les 5 semaines' },
    { name: 'ISATIS', produits: 'B.O.F', jours: 'Mercredi' },
    { name: 'GAEC Martin', produits: 'Yaourts', jours: 'Vendredi (quinzaine)' },
    { name: 'Sanipousse', produits: 'Consommables', jours: 'Au besoin' },
  ],
  // Plan de nettoyage : fiches de suivi réelles (3 classeurs, 6 zones)
  cleaningTasks: [
    // Zone préparation froide
    { id: 'n1', name: 'Cellules de refroidissement', zone: 'Préparation froide', freq: 'quotidien' },
    { id: 'n2', name: 'Plans de travail', zone: 'Préparation froide', freq: 'quotidien' },
    { id: 'n3', name: 'Plan de travail avec évier', zone: 'Préparation froide', freq: 'quotidien' },
    { id: 'n4', name: 'Trancheuse', zone: 'Préparation froide', freq: 'quotidien' },
    { id: 'n5', name: 'Petit batteur', zone: 'Préparation froide', freq: 'quotidien' },
    { id: 'n6', name: 'Gros batteur', zone: 'Préparation froide', freq: 'quotidien' },
    { id: 'n7', name: 'Placard double porte', zone: 'Préparation froide', freq: 'hebdomadaire' },
    { id: 'n8', name: 'Échelle de rangement', zone: 'Préparation froide', freq: 'hebdomadaire' },
    { id: 'n9', name: 'Étagère inox', zone: 'Préparation froide', freq: 'hebdomadaire' },
    // Zone cuisson
    { id: 'n10', name: 'Plans de travail', zone: 'Cuisson', freq: 'quotidien' },
    { id: 'n11', name: 'Piano', zone: 'Cuisson', freq: 'quotidien' },
    { id: 'n12', name: 'Sauteuse', zone: 'Cuisson', freq: 'quotidien' },
    { id: 'n13', name: 'Four', zone: 'Cuisson', freq: 'quotidien' },
    { id: 'n14', name: 'Évier', zone: 'Cuisson', freq: 'quotidien' },
    { id: 'n15', name: 'Presse purée', zone: 'Cuisson', freq: 'quotidien' },
    { id: 'n16', name: 'Mixeurs plongeants', zone: 'Cuisson', freq: 'quotidien' },
    { id: 'n17', name: 'Cutter', zone: 'Cuisson', freq: 'quotidien' },
    { id: 'n18', name: 'Poubelles', zone: 'Cuisson', freq: 'quotidien' },
    { id: 'n19', name: 'Frigo jour', zone: 'Cuisson', freq: 'hebdomadaire' },
    { id: 'n20', name: 'Frigo plats témoins', zone: 'Cuisson', freq: 'hebdomadaire' },
    { id: 'n21', name: 'Barre d\'ustensiles', zone: 'Cuisson', freq: 'hebdomadaire' },
    { id: 'n22', name: 'Friteuse', zone: 'Cuisson', freq: 'hebdomadaire' },
    { id: 'n23', name: 'Hotte', zone: 'Cuisson', freq: 'hebdomadaire' },
    // Zone légumerie
    { id: 'n24', name: 'Plan de travail', zone: 'Légumerie', freq: 'quotidien' },
    { id: 'n25', name: 'Coupe légumes', zone: 'Légumerie', freq: 'quotidien' },
    { id: 'n26', name: 'Évier', zone: 'Légumerie', freq: 'quotidien' },
    { id: 'n27', name: 'Table + ouvre-boîte', zone: 'Légumerie', freq: 'quotidien' },
    { id: 'n28', name: 'Frigos', zone: 'Légumerie', freq: 'hebdomadaire' },
    // Les trois zones
    { id: 'n29', name: 'Lave-mains', zone: 'Les trois zones', freq: 'quotidien' },
    { id: 'n30', name: 'Sols', zone: 'Les trois zones', freq: 'quotidien' },
    { id: 'n31', name: 'Encadrements de portes', zone: 'Les trois zones', freq: 'hebdomadaire' },
    { id: 'n32', name: 'Caillebotis', zone: 'Les trois zones', freq: 'hebdomadaire' },
    // Zone plonge
    { id: 'n33', name: 'Bac plonge', zone: 'Plonge', freq: 'quotidien' },
    { id: 'n34', name: 'Table avec évier', zone: 'Plonge', freq: 'quotidien' },
    { id: 'n35', name: 'Table égouttoir', zone: 'Plonge', freq: 'quotidien' },
    { id: 'n36', name: 'Chariots', zone: 'Plonge', freq: 'quotidien' },
    { id: 'n37', name: 'Machines à laver', zone: 'Plonge', freq: 'quotidien' },
    { id: 'n38', name: 'Étagère à paniers', zone: 'Plonge', freq: 'hebdomadaire' },
    { id: 'n39', name: 'Distributeur papier', zone: 'Plonge', freq: 'hebdomadaire' },
    { id: 'n40', name: 'Hotte plonge', zone: 'Plonge', freq: 'hebdomadaire' },
    { id: 'n41', name: 'Vitres', zone: 'Plonge', freq: 'mensuel' },
    // Zone office
    { id: 'n42', name: 'Table réfrigérée', zone: 'Office', freq: 'quotidien' },
    { id: 'n43', name: 'Bac à pain', zone: 'Office', freq: 'quotidien' },
    { id: 'n44', name: 'Coupe pain', zone: 'Office', freq: 'quotidien' },
    { id: 'n45', name: 'Frigo office', zone: 'Office', freq: 'hebdomadaire' },
    { id: 'n46', name: 'Étagère inox office', zone: 'Office', freq: 'hebdomadaire' },
    { id: 'n47', name: 'Placards inox', zone: 'Office', freq: 'mensuel' },
    // Économat / réception
    { id: 'n48', name: 'Lave-main réception', zone: 'Économat / Réception', freq: 'quotidien' },
    { id: 'n49', name: 'Balance', zone: 'Économat / Réception', freq: 'hebdomadaire' },
    { id: 'n50', name: 'Chariot réception', zone: 'Économat / Réception', freq: 'hebdomadaire' },
    { id: 'n51', name: 'Buffet inox', zone: 'Économat / Réception', freq: 'hebdomadaire' },
    { id: 'n52', name: 'Frigo légumes', zone: 'Économat / Réception', freq: 'hebdomadaire' },
    { id: 'n53', name: 'Frigo B.O.F', zone: 'Économat / Réception', freq: 'hebdomadaire' },
    { id: 'n54', name: 'Étagère fruits/légumes', zone: 'Économat / Réception', freq: 'hebdomadaire' },
    { id: 'n55', name: 'Étagères inox économat', zone: 'Économat / Réception', freq: 'mensuel' },
    { id: 'n56', name: 'Étagères de rangement', zone: 'Économat / Réception', freq: 'mensuel' },
    { id: 'n57', name: 'Congélateur réception', zone: 'Économat / Réception', freq: 'mensuel' },
  ],
};

const TYPE_LABELS = {
  temp: 'Température enceinte',
  reception: 'Réception livraison',
  refroid: 'Refroidissement / remise en T°',
  service: 'Température de service / expédition',
  decongel: 'Décongélation',
  entame: 'Produit entamé',
  verif: 'Vérification thermomètre',
  fermeture: 'Jour de fermeture',
  etiquette: 'Étiquette produit',
  nettoyage: 'Nettoyage',
  huile: 'Huile de friture',
  nonconf: 'Non-conformité',
};

let SETTINGS = null;

function getCurrentAgent() { return localStorage.getItem('haccp-agent') || ''; }
function setCurrentAgent(a) { localStorage.setItem('haccp-agent', a || ''); }

async function loadSettings() {
  const saved = await DB.getSetting('config', null);
  SETTINGS = saved ? Object.assign({}, JSON.parse(JSON.stringify(DEFAULT_SETTINGS)), saved) : JSON.parse(JSON.stringify(DEFAULT_SETTINGS));

  // Migration des anciennes installations vers la préconfiguration PMS.
  // Ne s'applique qu'aux listes jamais personnalisées : une liste volontairement
  // vidée par l'utilisateur ne doit pas être ressuscitée (d'où les tests length > 0).
  if (saved) {
    let dirty = false;
    if (!('agents' in saved)) { SETTINGS.agents = [...DEFAULT_SETTINGS.agents]; dirty = true; }
    if (!saved.fournisseurs) { SETTINGS.fournisseurs = JSON.parse(JSON.stringify(DEFAULT_SETTINGS.fournisseurs)); dirty = true; }
    const oldEquips = ['Frigo 1', 'Frigo 2', 'Chambre froide', 'Congélateur'];
    if (saved.equipements && saved.equipements.length > 0 && saved.equipements.length <= 4 && saved.equipements.every(e => oldEquips.includes(e.name))) {
      SETTINGS.equipements = JSON.parse(JSON.stringify(DEFAULT_SETTINGS.equipements)); dirty = true;
    }
    if (saved.cleaningTasks && saved.cleaningTasks.length > 0 && saved.cleaningTasks.length <= 8 && saved.cleaningTasks.every(t => /^n[1-8]$/.test(t.id))) {
      SETTINGS.cleaningTasks = JSON.parse(JSON.stringify(DEFAULT_SETTINGS.cleaningTasks)); dirty = true;
    }
    if (saved.etablissement === 'Cuisine EHPAD / FAM') { SETTINGS.etablissement = DEFAULT_SETTINGS.etablissement; dirty = true; }
    if (dirty) await DB.setSetting('config', SETTINGS);
  }
  document.getElementById('etab-name').textContent = SETTINGS.etablissement;
}

function saveSettings() {
  return DB.setSetting('config', SETTINGS).then(() => {
    document.getElementById('etab-name').textContent = SETTINGS.etablissement;
  });
}

function uid() { return 'x' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7); }

/* ---------- Navigation ---------- */
const VIEWS = {};
let currentView = 'dashboard';

let _pinOkUntil = 0; // déverrouillage des Réglages valable 5 min

function navigate(view) {
  // Réglages protégeables par PIN (tablette partagée en cuisine)
  if (view === 'parametres' && SETTINGS.pin && Date.now() > _pinOkUntil) {
    openPinModal(() => { _pinOkUntil = Date.now() + 5 * 60 * 1000; navigate('parametres'); });
    return;
  }
  currentView = view;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  render();
}

function openPinModal(onOk) {
  UI.modal(
    '<h2>🔒 Réglages protégés</h2>' +
    '<label class="field"><span class="lbl">Code PIN (4 chiffres)</span>' +
    '<input type="password" inputmode="numeric" maxlength="4" data-f="pin" class="temp-input" placeholder="••••" autocomplete="off"></label>' +
    '<p class="muted" style="font-size:12.5px">PIN oublié ? Il est lisible dans le fichier de sauvegarde JSON (champ « pin »).</p>' +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="ok">Déverrouiller</button></div>',
    (m, close) => {
      const input = m.querySelector('[data-f="pin"]');
      setTimeout(() => input.focus(), 80);
      const tryPin = () => {
        if (input.value === SETTINGS.pin) { close(); onOk(); }
        else { UI.toast('Code incorrect', 'bad'); input.value = ''; input.focus(); }
      };
      input.addEventListener('input', () => { if (input.value.length === 4) tryPin(); });
      m.querySelector('[data-x="ok"]').onclick = tryPin;
      m.querySelector('[data-x="cancel"]').onclick = close;
    }
  );
}

async function render() {
  const el = document.getElementById('view');
  el.innerHTML = '<div class="empty">Chargement…</div>';
  try {
    await VIEWS[currentView](el);
  } catch (e) {
    console.error(e);
    el.innerHTML = '<div class="empty">⚠️ Erreur : ' + UI.esc(e.message) + '</div>';
  }
}

/* ---------- Blocs réutilisables ---------- */

function headerHTML(title, sub, extraHTML) {
  return '<div class="view-header"><div><h1>' + UI.esc(title) + '</h1>' +
    (sub ? '<div class="sub">' + UI.esc(sub) + '</div>' : '') + '</div>' +
    (extraHTML || '') + '</div>';
}

function agentField(selected) {
  return UI.agentSelectHTML(SETTINGS.agents, selected !== undefined ? selected : getCurrentAgent());
}

function requireAgent(modal) {
  const sel = modal.querySelector('[data-f="agent"]');
  const agent = sel ? sel.value.trim() : '';
  if (!agent) { UI.toast('Choisis l’agent qui effectue le contrôle', 'bad'); return null; }
  setCurrentAgent(agent);
  return agent;
}

/** Champ action corrective, affiché quand non conforme. */
function actionFieldHTML(value) {
  return '<label class="field" data-action-field style="display:none">' +
    '<span class="lbl">Action corrective (obligatoire si non conforme)</span>' +
    '<textarea data-f="action" placeholder="Ex. : produit isolé/jeté, maintenance appelée, nouveau contrôle prévu…">' + UI.esc(value || '') + '</textarea></label>';
}

/** Service (midi/soir) d'un contrôle : champ dédié, ou déduit de l'heure pour
 *  les anciens enregistrements. */
function svcOf(r) { return r.service || ((r.time || '') < '14:00' ? 'midi' : 'soir'); }

/** Fournisseurs dont c'est le jour de livraison aujourd'hui (d'après le PMS). */
function fournisseursAttendusAujourdhui() {
  const jourNom = new Date().toLocaleDateString('fr-FR', { weekday: 'long' }).toLowerCase();
  return (SETTINGS.fournisseurs || []).filter(f => {
    const j = (f.jours || '').toLowerCase();
    return j.includes(jourNom) || j.includes('tous les jours');
  }).map(f => f.name);
}

/* ---------- Menu : plats & suggestions ---------- */
const PLAT_CATS = [
  { value: 'entree', label: 'Entrée' },
  { value: 'plat', label: 'Plat principal' },
  { value: 'garniture', label: 'Garniture' },
  { value: 'dessert', label: 'Dessert' },
  { value: 'autre', label: 'Autre' },
];
const PLAT_CAT_LABEL = Object.fromEntries(PLAT_CATS.map(c => [c.value, c.label]));

/** Noms des plats prévus au menu du jour (midi + soir), pour aujourd'hui. */
async function getTodayMenuNames() {
  const today = UI.todayISO();
  const menus = await DB.getByTypeAndRange('menu', today, today);
  const names = [];
  menus.forEach(mn => (mn.items || []).forEach(n => { if (!names.includes(n)) names.push(n); }));
  return names;
}

/** Champ de saisie d'un plat avec suggestions (menu du jour d'abord, puis catalogue).
 *  L'agent peut choisir dans la liste OU taper librement. */
function dishInputHTML(field, label, placeholder, menuNames, value) {
  const catalogue = SETTINGS.plats.map(p => p.name);
  const ordered = [...menuNames, ...catalogue.filter(n => !menuNames.includes(n))];
  const listId = 'dl-' + field;
  return '<label class="field"><span class="lbl">' + UI.esc(label) + '</span>' +
    '<input type="text" data-f="' + field + '" list="' + listId + '" placeholder="' + UI.esc(placeholder) + '" value="' + UI.esc(value || '') + '" autocomplete="off">' +
    '<datalist id="' + listId + '">' + ordered.map(n => '<option value="' + UI.esc(n) + '">').join('') + '</datalist>' +
    (menuNames.length ? '<span class="muted" style="font-size:12.5px">🍲 Menu du jour : ' + menuNames.map(UI.esc).join(', ') + '</span>' : '') +
    '</label>';
}

/* ---------- Sauvegarde Google Drive (via Apps Script) ---------- */
function lastAutoBackupDate() { return localStorage.getItem('haccp-drive-last') || ''; }
function setLastAutoBackupDate(d) { localStorage.setItem('haccp-drive-last', d || ''); }

async function buildBackup() {
  const records = await DB.getAllRecords();
  return { app: 'haccp-cuisine', version: 1, exportedAt: new Date().toISOString(), etablissement: SETTINGS.etablissement, settings: SETTINGS, records };
}

/** Google Drive (script Apps Script). Un POST text/plain est une « simple
 *  request » CORS dont la réponse est lisible — on vérifie réellement la
 *  réussite (pas de no-cors qui afficherait un faux succès). */
async function sendToDrive(payload) {
  const url = (SETTINGS.driveUrl || '').trim();
  if (!url) return { ok: false, message: 'URL du script manquante' };
  try {
    const resp = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'text/plain;charset=utf-8' }, body: payload });
    if (!resp.ok) return { ok: false, message: 'erreur serveur ' + resp.status + ' — vérifie l’URL du script' };
    const txt = await resp.text().catch(() => '');
    try {
      const json = JSON.parse(txt);
      if (json && json.ok === false) return { ok: false, message: json.erreur || 'erreur du script' };
    } catch { /* réponse non JSON : on garde le statut HTTP comme critère */ }
    return { ok: true };
  } catch (e) {
    return { ok: false, message: e.message };
  }
}

/** Dropbox : dépôt direct via un jeton d'accès (app « scoped », dossier dédié). */
async function sendToDropbox(payload) {
  const cfg = SETTINGS.dropbox || {};
  const token = (cfg.token || '').trim();
  if (!token) return { ok: false, message: 'jeton d’accès manquant' };
  try {
    const resp = await fetch('https://content.dropboxapi.com/2/files/upload', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + token,
        'Dropbox-API-Arg': JSON.stringify({ path: '/sauvegarde-haccp-' + UI.todayISO() + '.json', mode: 'overwrite', mute: true }),
        'Content-Type': 'application/octet-stream',
      },
      body: payload,
    });
    if (!resp.ok) return { ok: false, message: 'erreur ' + resp.status + (resp.status === 401 ? ' — jeton invalide ou expiré' : '') };
    return { ok: true };
  } catch (e) {
    return { ok: false, message: e.message };
  }
}

/** Nextcloud / ownCloud / tout serveur WebDAV : PUT avec authentification basique. */
async function sendToWebdav(payload) {
  const cfg = SETTINGS.webdav || {};
  let url = (cfg.url || '').trim();
  if (!url) return { ok: false, message: 'URL du dossier manquante' };
  if (!url.endsWith('/')) url += '/';
  const auth = 'Basic ' + btoa(unescape(encodeURIComponent((cfg.user || '') + ':' + (cfg.pass || ''))));
  try {
    const resp = await fetch(url + 'sauvegarde-haccp-' + UI.todayISO() + '.json', {
      method: 'PUT',
      headers: { 'Authorization': auth, 'Content-Type': 'application/json' },
      body: payload,
    });
    if (!resp.ok) return { ok: false, message: 'erreur ' + resp.status + (resp.status === 401 ? ' — identifiants refusés' : '') };
    return { ok: true };
  } catch (e) {
    return { ok: false, message: e.message };
  }
}

/** Serveur maison : simple POST vers l'adresse fournie (voir SAUVEGARDES-CLOUD.md).
 *  Envoyé en text/plain pour rester une « simple request » CORS (pas de préflight
 *  OPTIONS que l'exemple PHP ne saurait pas servir) — le corps reste du JSON. */
async function sendToHttp(payload) {
  const cfg = SETTINGS.httpPost || {};
  const url = (cfg.url || '').trim();
  if (!url) return { ok: false, message: 'adresse du serveur manquante' };
  try {
    const resp = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'text/plain;charset=utf-8' }, body: payload });
    if (!resp.ok) return { ok: false, message: 'erreur serveur ' + resp.status };
    return { ok: true };
  } catch (e) {
    return { ok: false, message: e.message };
  }
}

/** Liste des destinations activées et configurées. */
function backupTargets() {
  const t = [];
  if ((SETTINGS.driveUrl || '').trim()) t.push({ name: 'Google Drive', send: sendToDrive });
  if (SETTINGS.dropbox && SETTINGS.dropbox.on) t.push({ name: 'Dropbox', send: sendToDropbox });
  if (SETTINGS.webdav && SETTINGS.webdav.on) t.push({ name: 'Serveur WebDAV', send: sendToWebdav });
  if (SETTINGS.httpPost && SETTINGS.httpPost.on) t.push({ name: 'Serveur HTTP', send: sendToHttp });
  return t;
}

/** Dernière réussite d'une destination donnée (suivi par destination : une
 *  destination cassée reste visible même si les autres fonctionnent). */
function lastBackupFor(name) { return localStorage.getItem('haccp-backup-last:' + name) || ''; }

/** Envoie la sauvegarde vers toutes les destinations configurées. */
async function sendBackupAll() {
  const targets = backupTargets();
  if (!targets.length) return { ok: false, results: [], message: 'Aucune destination de sauvegarde configurée' };
  if (!navigator.onLine) return { ok: false, results: [], message: 'Pas de connexion Internet' };
  const payload = JSON.stringify(await buildBackup());
  const results = [];
  for (const t of targets) {
    const r = await t.send(payload);
    if (r.ok) localStorage.setItem('haccp-backup-last:' + t.name, UI.todayISO());
    results.push({ name: t.name, ok: r.ok, message: r.message || '' });
  }
  return { ok: results.some(r => r.ok), allOk: results.every(r => r.ok), results };
}

/** Sauvegarde automatique quotidienne (si activée et connectée). */
async function maybeAutoBackup() {
  if (!SETTINGS.driveAuto || !backupTargets().length) return;
  if (!navigator.onLine) return;
  const today = UI.todayISO();
  if (lastAutoBackupDate() === today) return;
  const res = await sendBackupAll();
  if (res.ok) {
    // le PDF hebdomadaire lisible part avec la sauvegarde (Drive uniquement)
    maybeWeeklyPdfToDrive().catch(e => console.warn('pdf drive', e));
  }
  if (res.allOk) {
    // Toutes les destinations ont réussi : plus de tentative aujourd'hui.
    setLastAutoBackupDate(today);
    UI.toast('Sauvegarde cloud effectuée ✔', 'ok');
  } else if (res.ok) {
    // Succès partiel : on NE pose PAS la date du jour, le retry horaire
    // repassera (ré-envoyer vers une destination déjà réussie est inoffensif :
    // le fichier du jour est simplement remplacé).
    UI.toast('Sauvegarde cloud PARTIELLE — échec : ' + res.results.filter(r => !r.ok).map(r => r.name).join(', ') + ' (nouvel essai dans 1 h)', 'bad');
  }
}

/* ================================================================
   TABLEAU DE BORD
================================================================ */
VIEWS.dashboard = async function (el) {
  const today = UI.todayISO();
  const [temps, receptions, services, nettoyages, refroids, decongels, entames, nonconfs, servicesJ5, verifs, menusJour] = (await Promise.all([
    DB.getByTypeAndRange('temp', today, today),
    DB.getByTypeAndRange('reception', today, today),
    DB.getByTypeAndRange('service', today, today),
    DB.getByTypeAndRange('nettoyage', today, today),
    DB.getByType('refroid'),
    DB.getByType('decongel'),
    DB.getByType('entame'),
    DB.getByType('nonconf'),
    DB.getByTypeAndRange('service', UI.addDays(today, -8), UI.addDays(today, -5)),
    DB.getByType('verif'),
    DB.getByTypeAndRange('menu', today, today),
  ])).map(alive);
  const decDepasse = decongels.filter(isDecongelDepasse);
  const entPerimes = entames.filter(r => r.statut !== 'termine' && r.dlc && r.dlc < today);
  const entAujourdhui = entames.filter(r => r.statut !== 'termine' && r.dlc === today);
  const ncOuvertes = nonconfs.filter(r => r.statut === 'ouverte');
  // Plats témoins en fin de conservation (J−5 à J−8, tant que non retirés le rappel reste)
  const temoinsARetirer = servicesJ5.filter(r => r.platTemoin);

  // Thermomètres jamais vérifiés ou vérifiés il y a plus d'un an
  const lastVerifByInstr = {};
  verifs.forEach(v => { if (!lastVerifByInstr[v.instrument] || v.date > lastVerifByInstr[v.instrument]) lastVerifByInstr[v.instrument] = v.date; });
  const thermosEnRetard = (SETTINGS.thermometres || []).filter(t => {
    const last = lastVerifByInstr[t];
    return !last || last < UI.addDays(today, -365);
  });

  // Fournisseurs attendus aujourd'hui (jour de livraison PMS) sans réception saisie
  const dejaRecus = new Set(receptions.map(r => (r.fournisseur || '').toLowerCase()));
  const attendusSansReception = fournisseursAttendusAujourdhui().filter(f => !dejaRecus.has(f.toLowerCase()));

  const dailyTasks = SETTINGS.cleaningTasks.filter(t => t.freq === 'quotidien');
  const doneTasks = new Set(nettoyages.map(n => n.taskId));
  const dailyDone = dailyTasks.filter(t => doneTasks.has(t.id)).length;
  const ncToday = [...temps, ...receptions, ...services, ...refroids.filter(r => r.date === today)].filter(r => r.conforme === false).length;
  const enCours = refroids.filter(r => r.status === 'encours');

  const equipsDone = new Set(temps.map(t => t.equipId));
  const equipsMissing = SETTINGS.equipements.filter(e => !equipsDone.has(e.id));

  // ---- Bilan de la journée (mis en avant après 16 h) ----
  const bilanLignes = [];
  const ligne = (ok, texte, go) => bilanLignes.push({ ok, texte, go });
  ligne(equipsMissing.length === 0 && SETTINGS.equipements.length > 0,
    'Enceintes relevées : ' + (SETTINGS.equipements.length - equipsMissing.length) + '/' + SETTINGS.equipements.length, 'temperatures');
  ligne(dailyTasks.length > 0 && dailyDone === dailyTasks.length,
    'Nettoyage quotidien : ' + dailyDone + '/' + dailyTasks.length, 'nettoyage');
  ['midi', 'soir'].forEach(sv => {
    const menu = menusJour.find(mn => mn.service === sv);
    if (!menu || !(menu.items || []).length) return;
    const faits = new Set(services.filter(r => svcOf(r) === sv).map(r => (r.plat || '').trim().toLowerCase()));
    const n = menu.items.filter(it => faits.has(it.trim().toLowerCase())).length;
    ligne(n >= menu.items.length, 'Service ' + sv + ' : ' + n + '/' + menu.items.length + ' plats du menu contrôlés', 'service');
    const temoin = services.some(r => svcOf(r) === sv && r.platTemoin);
    ligne(temoin, 'Plat témoin du ' + sv + ' : ' + (temoin ? 'prélevé' : 'à prélever'), 'service');
  });
  if (enCours.length) ligne(false, enCours.length + ' refroidissement(s) encore en cours à clôturer', 'refroidissement');
  if (ncOuvertes.length) ligne(false, ncOuvertes.length + ' non-conformité(s) ouverte(s) à traiter', 'nonconformites');
  const bilanOk = bilanLignes.every(l => l.ok);
  const apres16h = new Date().getHours() >= 16;
  const bilanHTML = '<div class="card"' + (apres16h && !bilanOk ? ' style="border-color:var(--orange)"' : '') + '>' +
    '<h2>' + (bilanOk ? '✅' : '🌙') + ' Bilan de la journée ' + (bilanOk ? '<span class="pill ok">tout est fait !</span>' : (apres16h ? '<span class="pill warn">à terminer avant la fin de journée</span>' : '')) + '</h2>' +
    '<div class="rec-list">' + bilanLignes.map(l =>
      '<div class="rec-item ' + (l.ok ? 'ok' : '') + '"><div class="big">' + (l.ok ? '✔' : '·') + '</div>' +
      '<div class="body"><div class="title" style="font-weight:600">' + UI.esc(l.texte) + '</div></div>' +
      (l.ok ? '' : '<button class="btn small secondary" data-go="' + l.go + '">Y aller</button>') + '</div>').join('') +
    '</div></div>';

  // Sauvegarde cloud en échec silencieux ? — vérifiée destination par destination
  let driveWarn = '';
  if (SETTINGS.driveAuto && backupTargets().length) {
    const enRetard = backupTargets().map(t => {
      const last = lastBackupFor(t.name);
      const jours = last ? Math.round((new Date(today + 'T12:00:00') - new Date(last + 'T12:00:00')) / 86400000) : null;
      return { name: t.name, last, jours };
    }).filter(t => !t.last || t.jours > 3);
    if (enRetard.length) {
      driveWarn = '<div class="card" style="border-color:var(--orange)"><div class="row">' +
        enRetard.map(t => '<span class="pill warn">☁️ ' + UI.esc(t.name) + ' : ' + (t.last ? 'dernière sauvegarde il y a ' + t.jours + ' jours' : 'jamais sauvegardé') + '</span>').join(' ') +
        '<span class="muted" style="font-size:13px">Vérifie cette destination dans les Réglages puis « Sauvegarder maintenant ».</span>' +
        '<button class="btn small secondary" data-go="parametres">Réglages</button></div></div>';
    }
  }

  el.innerHTML =
    headerHTML(SETTINGS.etablissement, 'Aujourd’hui — ' + new Date().toLocaleDateString('fr-FR', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })) +

    '<div class="card"><div class="row">' +
    '<div class="grow"><label class="field" style="margin:0"><span class="lbl">Agent en poste</span>' +
    '<select id="dash-agent"><option value="">— Choisir —</option>' +
    SETTINGS.agents.map(a => '<option' + (a === getCurrentAgent() ? ' selected' : '') + '>' + UI.esc(a) + '</option>').join('') +
    '</select></label></div>' +
    (SETTINGS.agents.length === 0 ? '<div class="pill warn">Ajoute les agents dans ⚙️ Réglages</div>' : '') +
    '</div></div>' +

    '<div class="stat-tiles">' +
    '<div class="stat ' + (equipsMissing.length === 0 && SETTINGS.equipements.length ? 'ok' : '') + '"><div class="n">' + (SETTINGS.equipements.length - equipsMissing.length) + '/' + SETTINGS.equipements.length + '</div><div class="t">Enceintes relevées (quotidien, début de journée)</div></div>' +
    '<div class="stat ' + (dailyDone === dailyTasks.length && dailyTasks.length ? 'ok' : '') + '"><div class="n">' + dailyDone + '/' + dailyTasks.length + '</div><div class="t">Nettoyage quotidien</div></div>' +
    '<div class="stat"><div class="n">' + receptions.length + '</div><div class="t">Réceptions du jour</div></div>' +
    '<div class="stat ' + (ncToday ? 'bad' : 'ok') + '"><div class="n">' + ncToday + '</div><div class="t">Non-conformités du jour</div></div>' +
    '</div>' +
    driveWarn +
    bilanHTML +

    (enCours.length ? '<div class="card"><h2>⏱️ En cours</h2><div class="rec-list">' + enCours.map(r => {
      const mins = Math.round((Date.now() - new Date(r.startISO).getTime()) / 60000);
      const limit = r.mode === 'remise' ? RULES.remiseMinutes : RULES.refroidMinutes;
      return '<div class="rec-item ' + (mins > limit ? 'bad' : '') + '" data-refroid-item="' + r.id + '">' +
        '<div class="big" data-refroid-mins="' + r.id + '">' + mins + ' min</div>' +
        '<div class="body"><div class="title">' + UI.esc(r.produit) + '</div>' +
        '<div class="meta">' + (r.mode === 'remise' ? 'Remise en température' : 'Refroidissement') + ' — départ ' + UI.esc(r.timeStart) + ' à ' + UI.fmtTemp(r.tempStart) + (mins > limit ? ' — ⚠️ délai dépassé !' : '') + '</div></div>' +
        '<button class="btn small" data-go="refroidissement">Terminer</button></div>';
    }).join('') + '</div></div>' : '') +

    ((decDepasse.length || entPerimes.length || entAujourdhui.length || ncOuvertes.length || temoinsARetirer.length) ?
      '<div class="card" style="border-color:var(--red)"><h2>🚨 Alertes</h2><div class="rec-list">' +
      decDepasse.map(r => '<div class="rec-item bad"><div class="big">🧊</div><div class="body"><div class="title">' + UI.esc(r.produit) + '</div><div class="meta">Décongélation : délai de 48 h dépassé (limite ' + UI.frDate(r.limite) + ') — produit à détruire</div></div><button class="btn small danger" data-go="decongel">Traiter</button></div>').join('') +
      entPerimes.map(r => '<div class="rec-item bad"><div class="big">📦</div><div class="body"><div class="title">' + UI.esc(r.produit) + '</div><div class="meta">Produit entamé : DLC interne dépassée (' + UI.frDate(r.dlc) + ') — à jeter</div></div><button class="btn small danger" data-go="entames">Traiter</button></div>').join('') +
      entAujourdhui.map(r => '<div class="rec-item"><div class="big">📦</div><div class="body"><div class="title">' + UI.esc(r.produit) + '</div><div class="meta">Produit entamé : à consommer aujourd’hui</div></div><button class="btn small secondary" data-go="entames">Voir</button></div>').join('') +
      ncOuvertes.map(r => '<div class="rec-item bad"><div class="big">⚠️</div><div class="body"><div class="title">' + UI.esc(r.objet) + '</div><div class="meta">Non-conformité ouverte depuis le ' + UI.frDate(r.date) + (r.lieu ? ' — ' + UI.esc(r.lieu) : '') + '</div></div><button class="btn small secondary" data-go="nonconformites">Traiter</button></div>').join('') +
      (temoinsARetirer.length ? '<div class="rec-item"><div class="big">🥡</div><div class="body"><div class="title">Plats témoins en fin de conservation (5 jours) à retirer</div><div class="meta">' + temoinsARetirer.map(r => UI.esc(r.plat) + ' (' + UI.frDate(r.date) + ')').join(', ') + '</div></div></div>' : '') +
      '</div></div>' : '') +

    ((equipsMissing.length || attendusSansReception.length || thermosEnRetard.length) ? '<div class="card"><h2>À faire</h2>' +
      (equipsMissing.length ? '<p class="muted" style="margin-bottom:10px">Enceintes sans relevé aujourd’hui :</p><div class="row">' +
        equipsMissing.map(e => '<span class="pill warn">🌡️ ' + UI.esc(e.name) + '</span>').join('') +
        '</div><div class="spacer"></div><button class="btn" data-go="temperatures">Faire les relevés</button>' : '') +
      (attendusSansReception.length ? '<div class="spacer"></div><div class="row"><span class="muted" style="font-size:13.5px">🚚 Livraison prévue aujourd’hui, pas encore contrôlée :</span>' +
        attendusSansReception.map(f => '<span class="pill info">' + UI.esc(f) + '</span>').join('') +
        '<button class="btn small secondary" data-go="reception">Réception</button></div>' : '') +
      (thermosEnRetard.length ? '<div class="spacer"></div><div class="row"><span class="muted" style="font-size:13.5px">🌡️ Vérification annuelle des thermomètres à faire :</span>' +
        thermosEnRetard.map(t => '<span class="pill warn">' + UI.esc(t) + '</span>').join('') +
        '<button class="btn small secondary" data-go="parametres">Vérifier</button></div>' : '') +
      '</div>' : '') +

    '<div class="card"><h2>Accès rapide</h2><div class="grid cols-3">' +
    '<button class="btn secondary" data-go="temperatures">❄️ Relevé température</button>' +
    '<button class="btn secondary" data-go="reception">🚚 Nouvelle réception</button>' +
    '<button class="btn secondary" data-go="tracabilite">🏷️ Photo étiquette</button>' +
    '<button class="btn secondary" data-go="nettoyage">🧽 Plan de nettoyage</button>' +
    '<button class="btn secondary" data-go="service">🍽️ T° de service</button>' +
    '<button class="btn secondary" data-go="historique">📋 Historique / export</button>' +
    '</div></div>';

  el.querySelector('#dash-agent').addEventListener('change', e => setCurrentAgent(e.target.value));
  el.querySelectorAll('[data-go]').forEach(b => b.addEventListener('click', () => navigate(b.dataset.go)));
};

/* ================================================================
   ENCEINTES FROIDES
================================================================ */
VIEWS.temperatures = async function (el) {
  const today = UI.todayISO();
  const recs = alive(await DB.getByTypeAndRange('temp', today, today));

  const doneIds = new Set(recs.map(r => r.equipId));
  const missing = SETTINGS.equipements.filter(e => !doneIds.has(e.id)).map(e => e.id);

  el.innerHTML = headerHTML('Enceintes froides', 'Relevés du ' + UI.frDate(today) + ' — PMS : relevé quotidien en début de journée (avant la reprise du travail)',
      missing.length ? '<button class="btn" id="chain-btn">▶ Relevés à la chaîne (' + missing.length + ')</button>' : '') +
    '<div class="grid cols-3" id="equip-grid">' +
    SETTINGS.equipements.map(eq => {
      const rEq = recs.filter(r => r.equipId === eq.id).sort((a, b) => a.time < b.time ? -1 : 1);
      const hasBad = rEq.some(r => r.conforme === false);
      const cls = hasBad ? 'alert' : (rEq.length ? 'done' : '');
      return '<button class="equip-tile ' + cls + '" data-eq="' + eq.id + '">' +
        '<div class="name">' + (eq.type === 'negatif' ? '🧊' : '❄️') + ' ' + UI.esc(eq.name) + '</div>' +
        '<div class="range">' + (eq.cible != null ? 'Cible ' + eq.cible + ' °C · ' : '') + 'limites ' + eq.min + ' à ' + eq.max + ' °C</div>' +
        '<div class="last">' + (rEq.length
          ? rEq.map(r => r.statut === 'hs'
              ? '<span class="pill">⏸ ' + UI.esc(r.motif || 'à l’arrêt') + '</span>'
              : '<span class="pill ' + (r.conforme === false ? 'bad' : 'ok') + '">' + UI.esc(r.time || r.moment || '') + ' ' + UI.fmtTemp(r.temp) + '</span>').join(' ')
          : '<span class="pill warn">Aucun relevé aujourd’hui</span>') + '</div>' +
        '</button>';
    }).join('') + '</div>' +
    (SETTINGS.equipements.length === 0 ? '<div class="empty"><span class="e-ico">⚙️</span>Ajoute tes équipements dans les Réglages.</div>' : '');

  el.querySelectorAll('[data-eq]').forEach(tile => tile.addEventListener('click', () => openTempModal(tile.dataset.eq)));
  const chainBtn = el.querySelector('#chain-btn');
  if (chainBtn) chainBtn.addEventListener('click', () => openTempModal(missing[0], missing.slice(1)));
};

/** Modale de relevé. `queue` (optionnel) : ids des enceintes suivantes pour un relevé à la chaîne. */
function openTempModal(equipId, queue) {
  const eq = SETTINGS.equipements.find(e => e.id === equipId);
  if (!eq) { if (queue && queue.length) openTempModal(queue[0], queue.slice(1)); return; }
  const hasNext = queue && queue.length > 0;

  UI.modal(
    '<h2>' + UI.esc(eq.name) + (queue ? ' <span class="pill info">' + (queue.length + 1) + ' restante' + (queue.length ? 's' : '') + '</span>' : '') + '</h2>' +
    '<p class="muted" style="margin-bottom:14px">' + (eq.cible != null ? 'Valeur cible : ' + eq.cible + ' °C · ' : '') + 'Limites critiques : ' + eq.min + ' à ' + eq.max + ' °C — relevé quotidien du matin (PMS)</p>' +
    '<label class="field"><span class="lbl">Température relevée (°C)</span>' +
    UI.tempInputHTML('temp', { placeholder: eq.type === 'negatif' ? '-18.0' : '3.0', hint: eq.type === 'negatif' ? 'Enceinte négative : pense au signe − (bouton ±)' : '' }) + '</label>' +
    agentField() +
    '<div data-verdict></div>' +
    actionFieldHTML() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">' + (queue ? 'Arrêter' : 'Annuler') + '</button>' +
    '<button class="btn ghost" data-x="hs">⏸ À l’arrêt</button>' +
    '<button class="btn" data-x="save">' + (hasNext ? 'Enregistrer → suivante' : 'Enregistrer') + '</button></div>',
    (m, close) => {
      UI.segWire(m);
      const tempInput = m.querySelector('[data-f="temp"]');
      const verdict = m.querySelector('[data-verdict]');
      const actionField = m.querySelector('[data-action-field]');

      // Enceinte à l'arrêt aujourd'hui (dégivrage, vide, panne) : trace un
      // relevé « HS » qui compte comme fait, sans température.
      m.querySelector('[data-x="hs"]').onclick = () => {
        const agent = requireAgent(m); if (!agent) return;
        UI.modal(
          '<h2>⏸ ' + UI.esc(eq.name) + ' à l’arrêt</h2>' +
          '<label class="field"><span class="lbl">Motif</span>' +
          UI.segHTML('motif', [
            { value: 'dégivrage', label: '❄️ Dégivrage' },
            { value: 'vide / non utilisée', label: '📭 Vide' },
            { value: 'panne', label: '🔧 Panne', bad: true },
          ], 'dégivrage') + '</label>' +
          '<div class="actions"><button class="btn ghost" data-x="c2">Annuler</button><button class="btn" data-x="s2">Enregistrer</button></div>',
          (m2, close2) => {
            UI.segWire(m2);
            m2.querySelector('[data-x="c2"]').onclick = close2;
            m2.querySelector('[data-x="s2"]').onclick = async () => {
              const motif = UI.segValue(m2, 'motif') || 'dégivrage';
              await DB.addRecord({
                type: 'temp', date: UI.todayISO(), time: UI.nowHM(),
                equipId: eq.id, equipName: eq.name, statut: 'hs', motif, agent,
              });
              if (motif === 'panne') {
                await DB.addRecord({
                  type: 'nonconf', date: UI.todayISO(), time: UI.nowHM(),
                  objet: 'Panne : ' + eq.name, lieu: '', lot: '', peremption: '',
                  description: 'Enceinte à l’arrêt (panne) — denrées à contrôler et déplacer (IN rupture de froid)',
                  action: '', statut: 'ouverte', agent,
                });
                UI.toast('Panne signalée — non-conformité ouverte', 'bad');
              } else {
                UI.toast(eq.name + ' : à l’arrêt (' + motif + ') ✔', 'ok');
              }
              close2(); close(); render();
              if (hasNext) openTempModal(queue[0], queue.slice(1));
            };
          }
        );
      };

      const check = () => {
        const v = parseFloat(tempInput.value);
        if (isNaN(v)) { verdict.innerHTML = ''; actionField.style.display = 'none'; return null; }
        const ok = v >= eq.min && v <= eq.max;
        verdict.innerHTML = ok
          ? '<p class="pill ok" style="margin-bottom:12px">✔ Conforme</p>'
          : '<p class="pill bad" style="margin-bottom:12px">✘ NON CONFORME (' + eq.min + ' à ' + eq.max + ' °C)</p>' +
            '<p class="muted" style="font-size:13px;margin-bottom:10px">Conduite PMS : vérifier le réglage (tenir compte du dégivrage), contrôler la T° à cœur de 3 produits, appeler la maintenance si panne. Frais : 6–10 °C à cœur → utiliser sous 2 h ; &gt;10 °C → détruire. Surgelés : −15/−5 °C → décongélation à utiliser sous 48 h ; &gt;−5 °C → détruire.</p>';
        actionField.style.display = ok ? 'none' : 'block';
        return ok;
      };
      tempInput.addEventListener('input', check);
      setTimeout(() => tempInput.focus(), 60);

      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const v = parseFloat(tempInput.value);
        if (isNaN(v)) { UI.toast('Saisis la température', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        const ok = v >= eq.min && v <= eq.max;
        const action = m.querySelector('[data-f="action"]').value.trim();
        if (!ok && !action) { UI.toast('Indique l’action corrective', 'bad'); return; }
        await DB.addRecord({
          type: 'temp', date: UI.todayISO(), time: UI.nowHM(),
          equipId: eq.id, equipName: eq.name, temp: v,
          conforme: ok, action: ok ? '' : action, agent,
        });
        close();
        UI.toast(ok ? 'Relevé enregistré ✔' : 'Non-conformité enregistrée', ok ? 'ok' : 'bad');
        render();
        if (hasNext) openTempModal(queue[0], queue.slice(1));
        else if (queue) UI.toast('🎉 Tous les relevés du jour sont faits !', 'ok');
      };
    }
  );
}

/* ================================================================
   RÉCEPTIONS
================================================================ */
VIEWS.reception = async function (el) {
  const today = UI.todayISO();
  const recs = alive(await DB.getByTypeAndRange('reception', UI.addDays(today, -6), today)).sort((a, b) => (b.date + b.time).localeCompare(a.date + a.time));

  el.innerHTML = headerHTML('Réceptions de marchandises', '7 derniers jours', '<button class="btn" id="new-rec">➕ Nouvelle réception</button>') +
    (recs.length ? '<div class="rec-list">' + recs.map(r =>
      '<div class="rec-item ' + (r.conforme === false ? 'bad' : 'ok') + '">' +
      '<div class="big">' + (r.temp != null && r.temp !== '' ? UI.fmtTemp(r.temp) : '—') + '</div>' +
      '<div class="body"><div class="title">' + UI.esc(r.produit) + ' <span class="muted">· ' + UI.esc(r.fournisseur) + '</span></div>' +
      '<div class="meta">' + UI.frDate(r.date) + ' ' + UI.esc(r.time) + ' — ' + UI.esc(r.famille) + (r.lot ? ' — lot ' + UI.esc(r.lot) : '') + ' — ' + UI.esc(r.agent) +
      (r.action ? ' — ' + UI.esc(r.action) : '') + '</div></div>' +
      '<span class="pill ' + (r.conforme === false ? 'bad' : (r.tolere ? 'warn' : 'ok')) + '">' + (r.conforme === false ? 'Non conforme' : (r.tolere ? 'Contrôlé à cœur' : 'Conforme')) + '</span></div>'
    ).join('') + '</div>' : '<div class="empty"><span class="e-ico">🚚</span>Aucune réception enregistrée cette semaine.</div>');

  el.querySelector('#new-rec').addEventListener('click', openReceptionModal);
};

async function openReceptionModal() {
  const past = await DB.getByType('reception');
  // Fournisseurs attendus aujourd'hui (champ « jours de livraison ») proposés en tête
  const jourNom = new Date().toLocaleDateString('fr-FR', { weekday: 'long' }).toLowerCase();
  const attendus = (SETTINGS.fournisseurs || []).filter(f => {
    const j = (f.jours || '').toLowerCase();
    return j.includes(jourNom) || j.includes('tous les jours');
  }).map(f => f.name);
  const connus = (SETTINGS.fournisseurs || []).map(f => f.name).sort((a, b) => (attendus.includes(b) ? 1 : 0) - (attendus.includes(a) ? 1 : 0));
  const fournisseurs = [...connus, ...[...new Set(past.map(r => r.fournisseur).filter(Boolean))].filter(f => !connus.includes(f))];

  UI.modal(
    '<h2>🚚 Nouvelle réception</h2>' +
    '<label class="field"><span class="lbl">Fournisseur</span>' +
    '<input type="text" data-f="fournisseur" list="dl-fourn" placeholder="Nom du fournisseur" autocomplete="off">' +
    '<datalist id="dl-fourn">' + fournisseurs.map(f => '<option value="' + UI.esc(f) + '">').join('') + '</datalist>' +
    (attendus.length ? '<span class="muted" style="font-size:12.5px">🚚 Attendus aujourd’hui : ' + attendus.map(UI.esc).join(', ') + '</span>' : '') +
    '</label>' +
    '<label class="field"><span class="lbl">Produit / livraison</span>' +
    '<input type="text" data-f="produit" placeholder="Ex. : viande hachée, produits laitiers…"></label>' +
    '<label class="field"><span class="lbl">N° de lot / bon de livraison (optionnel)</span>' +
    '<input type="text" data-f="lot" placeholder="Ex. : BL 12345, lot 2026-07"></label>' +
    '<label class="field"><span class="lbl">Famille de produits</span>' +
    UI.segHTML('famille', [
      { value: 'frais', label: '❄️ Frais (≤ 6 °C)' },
      { value: 'hache', label: '🥩 Viande hachée / abats (≤ 2 °C)' },
      { value: 'surgele', label: '🧊 Surgelé (≤ −15 °C)' },
      { value: 'epicerie', label: '📦 Épicerie / sec' },
    ], 'frais') + '</label>' +
    '<p class="muted" style="font-size:12.5px;margin:-6px 0 12px">Cibles PMS : frais 3 °C, viandes hachées 2 °C, surgelés −18 °C. Au-dessus du seuil : contrôle à cœur ; &gt; 10 °C : refus.</p>' +
    '<label class="field"><span class="lbl">Température à réception (°C)</span>' +
    UI.tempInputHTML('temp', { hint: 'Surgelés : pense au signe − (bouton ±)' }) + '</label>' +
    '<label class="field"><span class="lbl">État (emballage, étiquetage, DLC, propreté camion)</span>' +
    UI.segHTML('etat', [{ value: 'ok', label: '✔ Correct' }, { value: 'bad', label: '✘ Défaut constaté', bad: true }], 'ok') + '</label>' +
    agentField() +
    '<div data-verdict></div>' +
    actionFieldHTML() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      UI.segWire(m);
      const verdict = m.querySelector('[data-verdict]');
      const actionField = m.querySelector('[data-action-field]');

      // Verdict PMS : ok / tolere (frais 6–10 °C : contrôle à cœur requis) / refus
      const evalConf = () => {
        const fam = UI.segValue(m, 'famille');
        const etat = UI.segValue(m, 'etat');
        const t = parseFloat(m.querySelector('[data-f="temp"]').value);
        let ok = etat !== 'bad';
        let tolere = false;
        if ((fam === 'frais' || fam === 'hache') && !isNaN(t)) {
          const seuil = fam === 'hache' ? RULES.hacheMax : RULES.fraisMax;
          if (t > RULES.fraisRefus) ok = false;
          else if (t > seuil) tolere = true;
        }
        if (fam === 'surgele' && !isNaN(t) && t > RULES.surgeleMax) ok = false;
        if (ok && tolere) {
          verdict.innerHTML = '<p class="pill warn" style="margin-bottom:12px">⚠ Seuil dépassé : contrôle de la T° à cœur obligatoire avant acceptation (refus si &gt; 10 °C à cœur)</p>';
        } else if (ok) {
          verdict.innerHTML = '<p class="pill ok" style="margin-bottom:12px">✔ Conforme</p>';
        } else {
          verdict.innerHTML = '<p class="pill bad" style="margin-bottom:12px">✘ NON CONFORME — refus de la marchandise</p>';
        }
        actionField.style.display = (ok && !tolere) ? 'none' : 'block';
        return { ok, tolere };
      };
      m.addEventListener('click', () => setTimeout(evalConf, 30));
      m.querySelector('[data-f="temp"]').addEventListener('input', evalConf);

      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const fournisseur = m.querySelector('[data-f="fournisseur"]').value.trim();
        const produit = m.querySelector('[data-f="produit"]').value.trim();
        if (!fournisseur || !produit) { UI.toast('Fournisseur et produit sont obligatoires', 'bad'); return; }
        const fam = UI.segValue(m, 'famille');
        const t = parseFloat(m.querySelector('[data-f="temp"]').value);
        if (fam !== 'epicerie' && isNaN(t)) { UI.toast('La température est obligatoire pour les produits frais et surgelés', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        const { ok, tolere } = evalConf();
        const action = m.querySelector('[data-f="action"]').value.trim();
        if (!ok && !action) { UI.toast('Indique l’action corrective (refus, réserve…)', 'bad'); return; }
        if (ok && tolere && !action) { UI.toast('Indique le résultat du contrôle de la T° à cœur', 'bad'); return; }
        await DB.addRecord({
          type: 'reception', date: UI.todayISO(), time: UI.nowHM(),
          fournisseur, produit, famille: fam,
          lot: m.querySelector('[data-f="lot"]').value.trim(),
          temp: isNaN(t) ? null : t, etat: UI.segValue(m, 'etat'),
          tolere, conforme: ok, action, agent,
        });
        close();
        UI.toast('Réception enregistrée ✔', 'ok');
        render();
      };
    }
  );
}

/* ================================================================
   REFROIDISSEMENT / REMISE EN TEMPÉRATURE
================================================================ */
VIEWS.refroidissement = async function (el) {
  const today = UI.todayISO();
  // Les suivis « en cours » restent visibles quel que soit leur âge (sinon inclôturables) ; historique borné à 7 jours.
  const all = alive(await DB.getByType('refroid')).sort((a, b) => (b.date + b.timeStart).localeCompare(a.date + a.timeStart));
  const enCours = all.filter(r => r.status === 'encours');
  const finis = all.filter(r => r.status !== 'encours' && r.date >= UI.addDays(today, -6));

  el.innerHTML = headerHTML('Refroidissement & remise en T°', 'Refroidissement : +63→+10 °C en 2 h max · Remise : +10→+63 °C en 1 h max',
      '<button class="btn" id="new-refroid">➕ Démarrer un suivi</button>') +

    (enCours.length ? '<div class="card"><h2>⏱️ En cours</h2><div class="rec-list">' + enCours.map(r => {
      const mins = Math.round((Date.now() - new Date(r.startISO).getTime()) / 60000);
      const limit = r.mode === 'remise' ? RULES.remiseMinutes : RULES.refroidMinutes;
      return '<div class="rec-item ' + (mins > limit ? 'bad' : '') + '" data-refroid-item="' + r.id + '">' +
        '<div class="big" data-refroid-mins="' + r.id + '">' + mins + ' min</div>' +
        '<div class="body"><div class="title">' + UI.esc(r.produit) + '</div>' +
        '<div class="meta">' + (r.mode === 'remise' ? '🔥 Remise en température' : '📉 Refroidissement') + ' — départ ' + UI.esc(r.timeStart) + ' à ' + UI.fmtTemp(r.tempStart) +
        (mins > limit ? ' — ⚠️ délai dépassé' : ' (limite ' + limit + ' min)') + '</div></div>' +
        '<button class="btn small" data-finish="' + r.id + '">Terminer</button></div>';
    }).join('') + '</div></div>' : '') +

    '<div class="card"><h2>Terminés (7 jours)</h2>' +
    (finis.length ? '<div class="rec-list">' + finis.map(r =>
      '<div class="rec-item ' + (r.conforme === false ? 'bad' : 'ok') + '">' +
      '<div class="big">' + r.durationMin + ' min</div>' +
      '<div class="body"><div class="title">' + UI.esc(r.produit) + '</div>' +
      '<div class="meta">' + UI.frDate(r.date) + ' — ' + (r.mode === 'remise' ? 'Remise' : 'Refroidissement') + ' : ' +
      UI.fmtTemp(r.tempStart) + ' (' + UI.esc(r.timeStart) + ') → ' + UI.fmtTemp(r.tempEnd) + ' (' + UI.esc(r.timeEnd) + ') — ' + UI.esc(r.agent) +
      (r.conforme === false ? ' — ⚠️ ' + UI.esc(r.action || '') : '') + '</div></div>' +
      '<span class="pill ' + (r.conforme === false ? 'bad' : 'ok') + '">' + (r.conforme === false ? 'Non conforme' : 'Conforme') + '</span></div>'
    ).join('') + '</div>' : '<div class="empty">Aucun suivi terminé cette semaine.</div>') + '</div>';

  el.querySelector('#new-refroid').addEventListener('click', openRefroidStartModal);
  el.querySelectorAll('[data-finish]').forEach(b => b.addEventListener('click', () => openRefroidFinishModal(Number(b.dataset.finish))));
};

/** ISO datetime local à partir d'une date AAAA-MM-JJ et d'une heure HH:MM. */
function isoFromDayTime(day, hm) {
  return new Date(day + 'T' + hm + ':00').toISOString();
}

async function openRefroidStartModal() {
  const menuNames = await getTodayMenuNames();
  UI.modal(
    '<h2>Démarrer un suivi</h2>' +
    '<label class="field"><span class="lbl">Type</span>' +
    UI.segHTML('mode', [
      { value: 'refroidissement', label: '📉 Refroidissement (2 h max)' },
      { value: 'remise', label: '🔥 Remise en T° (1 h max)' },
    ], 'refroidissement') + '</label>' +
    dishInputHTML('produit', 'Préparation / plat', 'Ex. : blanquette de veau', menuNames) +
    '<label class="field"><span class="lbl">Température de départ (°C)</span>' +
    UI.tempInputHTML('temp', { placeholder: '63.0' }) + '</label>' +
    '<label class="field"><span class="lbl">Heure de début (modifiable si saisie après coup)</span>' +
    '<input type="time" data-f="heure" value="' + UI.nowHM() + '"></label>' +
    agentField() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">▶ Démarrer</button></div>',
    (m, close) => {
      UI.segWire(m);
      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const produit = m.querySelector('[data-f="produit"]').value.trim();
        const t = parseFloat(m.querySelector('[data-f="temp"]').value);
        if (!produit || isNaN(t)) { UI.toast('Renseigne le plat et la température', 'bad'); return; }
        const heure = m.querySelector('[data-f="heure"]').value || UI.nowHM();
        const agent = requireAgent(m); if (!agent) return;
        // Une heure « dans le futur » correspond en réalité à hier soir
        // (ex. : départ 23:30 saisi à 00:15) : on date au jour précédent.
        const day = heure > UI.nowHM() ? UI.addDays(UI.todayISO(), -1) : UI.todayISO();
        await DB.addRecord({
          type: 'refroid', date: day, mode: UI.segValue(m, 'mode'),
          produit, tempStart: t, timeStart: heure, startISO: isoFromDayTime(day, heure),
          status: 'encours', agent,
        });
        close();
        UI.toast('Suivi démarré — pense à le terminer !', 'ok');
        render();
      };
    }
  );
}

async function openRefroidFinishModal(id) {
  const rec = await DB.getRecord(id);
  if (!rec) return;
  const limit = rec.mode === 'remise' ? RULES.remiseMinutes : RULES.refroidMinutes;
  const target = rec.mode === 'remise' ? RULES.remiseTarget : RULES.refroidTarget;

  UI.modal(
    '<h2>Terminer : ' + UI.esc(rec.produit) + '</h2>' +
    '<p class="muted" style="margin-bottom:14px">' +
    (rec.mode === 'remise' ? 'Objectif : ≥ ' + target + ' °C en moins de ' + limit + ' min' : 'Objectif : ≤ ' + target + ' °C en moins de ' + limit + ' min') +
    ' — départ ' + UI.esc(rec.timeStart) + ' à ' + UI.fmtTemp(rec.tempStart) + '</p>' +
    '<label class="field"><span class="lbl">Température finale (°C)</span>' +
    UI.tempInputHTML('temp') + '</label>' +
    '<label class="field"><span class="lbl">Heure de fin (modifiable si saisie après coup)</span>' +
    '<input type="time" data-f="heureFin" value="' + UI.nowHM() + '"></label>' +
    agentField(rec.agent) +
    '<div data-verdict></div>' +
    actionFieldHTML() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      const tempInput = m.querySelector('[data-f="temp"]');
      const heureInput = m.querySelector('[data-f="heureFin"]');
      const verdict = m.querySelector('[data-verdict]');
      const actionField = m.querySelector('[data-action-field]');
      // Durée = heure de fin choisie − début. L'heure de fin est ancrée sur
      // l'occurrence la plus récente NON FUTURE de HH:MM (si l'heure est
      // « dans le futur », c'était hier soir). Une fin antérieure au début est
      // refusée : mins vaut alors null.
      const endInfo = () => {
        const hm = heureInput.value || UI.nowHM();
        const now = new Date();
        let end = new Date(UI.todayISO() + 'T' + hm + ':00');
        if (end > now) end = new Date(end.getTime() - 24 * 3600 * 1000);
        const start = new Date(rec.startISO);
        if (end < start) return { hm, mins: null };
        return { hm, mins: Math.round((end - start) / 60000) };
      };

      const check = () => {
        const v = parseFloat(tempInput.value);
        if (isNaN(v)) { verdict.innerHTML = ''; actionField.style.display = 'none'; return null; }
        const { mins } = endInfo();
        if (mins == null) {
          verdict.innerHTML = '<p class="pill bad" style="margin-bottom:12px">⚠ Heure de fin antérieure au début (' + UI.esc(rec.timeStart) + ') — vérifie l’heure saisie</p>';
          actionField.style.display = 'none';
          return null;
        }
        const tempOK = rec.mode === 'remise' ? v >= target : v <= target;
        const ok = tempOK && mins <= limit;
        verdict.innerHTML = '<p class="pill ' + (ok ? 'ok' : 'bad') + '" style="margin-bottom:12px">' +
          (ok ? '✔ Conforme' : '✘ NON CONFORME') + ' — durée : ' + mins + ' min / ' + limit + ' min</p>';
        actionField.style.display = ok ? 'none' : 'block';
        return ok;
      };
      tempInput.addEventListener('input', check);
      heureInput.addEventListener('input', check);

      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const v = parseFloat(tempInput.value);
        if (isNaN(v)) { UI.toast('Saisis la température finale', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        const { hm, mins } = endInfo();
        if (mins == null) { UI.toast('Heure de fin antérieure au début du suivi — corrige l’heure', 'bad'); return; }
        const tempOK = rec.mode === 'remise' ? v >= target : v <= target;
        const ok = tempOK && mins <= limit;
        const action = m.querySelector('[data-f="action"]').value.trim();
        if (!ok && !action) { UI.toast('Indique l’action corrective (prolongation, jet du produit…)', 'bad'); return; }
        Object.assign(rec, {
          tempEnd: v, timeEnd: hm, durationMin: mins,
          status: 'fini', conforme: ok, action: ok ? '' : action, agent,
        });
        await DB.updateRecord(rec);
        close();
        UI.toast(ok ? 'Suivi terminé ✔' : 'Non-conformité enregistrée', ok ? 'ok' : 'bad');
        render();
      };
    }
  );
}

/* ================================================================
   TEMPÉRATURES DE SERVICE
================================================================ */
VIEWS.service = async function (el) {
  const today = UI.todayISO();
  const state = VIEWS.service._state || (VIEWS.service._state = {});
  // Le choix explicite midi/soir n'est mémorisé que pour la journée en cours ;
  // sans choix, le service suit l'heure (midi avant 14 h). Ne PAS écrire
  // state.svc ici : sinon le service resterait figé d'un jour sur l'autre.
  if (state.date !== today) { state.date = today; state.svc = null; }
  const svc = state.svc || (new Date().getHours() < 14 ? 'midi' : 'soir');

  const [recsAll, menus] = await Promise.all([
    DB.getByTypeAndRange('service', today, today),
    DB.getByTypeAndRange('menu', today, today),
  ]);
  const recs = alive(recsAll);
  recs.sort((a, b) => b.time.localeCompare(a.time));

  // Plats prévus au menu de ce service, rapprochés des contrôles déjà faits
  // POUR CE SERVICE (un plat contrôlé au midi doit être re-contrôlé au soir).
  const menu = menus.find(mn => mn.service === svc);
  const items = menu ? (menu.items || []) : [];
  const doneByPlat = {};
  recs.filter(r => svcOf(r) === svc).forEach(r => { const k = (r.plat || '').trim().toLowerCase(); if (!doneByPlat[k]) doneByPlat[k] = r; });

  const temoinsToday = recs.filter(r => r.platTemoin).length;

  el.innerHTML = headerHTML('Températures de service', 'Avant chaque service : chaude ≥ 63 °C · froide cible 3 °C, limite 6 °C (10 °C si conso < 2 h) — ' + UI.frDate(today),
      '<button class="btn" id="new-serv">➕ Contrôle libre</button>') +

    '<div class="card"><div class="row" style="margin-bottom:10px">' +
    '<h2 style="margin:0">🍲 Plats du jour à contrôler</h2>' +
    '<div class="grow"></div>' +
    UI.segHTML('svc', [{ value: 'midi', label: '🌞 Midi' }, { value: 'soir', label: '🌙 Soir' }], svc) +
    '</div>' +
    (items.length
      ? '<div class="rec-list">' + items.map(name => {
          const r = doneByPlat[name.trim().toLowerCase()];
          if (r) {
            return '<div class="rec-item ' + (r.conforme === false ? 'bad' : 'ok') + '"><div class="big">' + UI.fmtTemp(r.temp) + '</div>' +
              '<div class="body"><div class="title">' + UI.esc(name) + '</div>' +
              '<div class="meta">' + (r.liaison === 'chaude' ? '🔥 chaude' : '❄️ froide') + ' — ' + UI.esc(r.time) + ' — ' + UI.esc(r.agent) + '</div></div>' +
              '<span class="pill ' + (r.conforme === false ? 'bad' : (r.tolere ? 'warn' : 'ok')) + '">' + (r.conforme === false ? 'Non conforme' : (r.tolere ? 'Toléré < 2 h' : '✔')) + '</span></div>';
          }
          return '<div class="rec-item"><div class="big">—</div>' +
            '<div class="body"><div class="title">' + UI.esc(name) + '</div><div class="meta">pas encore contrôlé</div></div>' +
            '<button class="btn small" data-ctrl="' + UI.esc(name) + '">🌡️ Prendre la T°</button></div>';
        }).join('') + '</div>'
      : '<div class="empty" style="padding:14px">Aucun menu enregistré pour le ' + (svc === 'midi' ? 'midi' : 'soir') + ' — complète le module 🍲 Menu (ou importe ton fichier de l’année).</div>') +
    '</div>' +

    '<div class="card" style="padding:12px 18px"><div class="row">' +
    '<span class="pill ' + (temoinsToday ? 'ok' : 'warn') + '">🥡 Plats témoins du jour : ' + temoinsToday + '</span>' +
    '<span class="muted" style="font-size:13px">PMS : une portion ≥ 100 g de chaque plat avant chaque service, conservée 5 jours à 3 °C au frigo plats témoins.</span>' +
    '</div></div>' +

    (recs.length ? '<div class="card"><h2>Contrôles du jour</h2><div class="rec-list">' + recs.map(r =>
      '<div class="rec-item ' + (r.conforme === false ? 'bad' : 'ok') + '">' +
      '<div class="big">' + UI.fmtTemp(r.temp) + '</div>' +
      '<div class="body"><div class="title">' + UI.esc(r.plat) + (r.platTemoin ? ' <span class="pill info">Plat témoin ✔</span>' : '') + '</div>' +
      '<div class="meta">' + (r.liaison === 'chaude' ? '🔥 Liaison chaude' : '❄️ Liaison froide') + ' — ' + UI.esc(r.time) + ' — ' + UI.esc(r.agent) +
      (r.conforme === false ? ' — ⚠️ ' + UI.esc(r.action || '') : '') + '</div></div>' +
      '<span class="pill ' + (r.conforme === false ? 'bad' : (r.tolere ? 'warn' : 'ok')) + '">' + (r.conforme === false ? 'Non conforme' : (r.tolere ? 'Toléré < 2 h' : 'Conforme')) + '</span></div>'
    ).join('') + '</div></div>' : '');

  UI.segWire(el);
  el.querySelector('.seg[data-seg="svc"]').addEventListener('click', () => setTimeout(() => {
    const v = UI.segValue(el, 'svc');
    if (v && v !== svc) { state.svc = v; render(); }
  }, 30));
  el.querySelector('#new-serv').addEventListener('click', () => openServiceModal('', svc));
  el.querySelectorAll('[data-ctrl]').forEach(b => b.addEventListener('click', () => openServiceModal(b.dataset.ctrl, svc)));
};

async function openServiceModal(prefillPlat, svc) {
  const service = svc || (new Date().getHours() < 14 ? 'midi' : 'soir');
  const menuNames = await getTodayMenuNames();
  UI.modal(
    '<h2>🍽️ Contrôle au service</h2>' +
    dishInputHTML('plat', 'Plat', 'Ex. : purée, salade de betteraves…', menuNames, prefillPlat || '') +
    '<label class="field"><span class="lbl">Liaison</span>' +
    UI.segHTML('liaison', [
      { value: 'chaude', label: '🔥 Chaude (≥ 63 °C)' },
      { value: 'froide', label: '❄️ Froide (≤ 10 °C)' },
    ], 'chaude') + '</label>' +
    '<label class="field"><span class="lbl">Température (°C)</span>' +
    UI.tempInputHTML('temp') + '</label>' +
    '<label class="field"><span class="lbl">Plat témoin prélevé ?</span>' +
    UI.segHTML('temoin', [{ value: 'oui', label: '✔ Oui' }, { value: 'non', label: 'Non' }], 'non') + '</label>' +
    agentField() +
    '<div data-verdict></div>' +
    actionFieldHTML() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      UI.segWire(m);
      const tempInput = m.querySelector('[data-f="temp"]');
      const verdict = m.querySelector('[data-verdict]');
      const actionField = m.querySelector('[data-action-field]');

      const check = () => {
        const v = parseFloat(tempInput.value);
        if (isNaN(v)) { verdict.innerHTML = ''; actionField.style.display = 'none'; return null; }
        const liaison = UI.segValue(m, 'liaison');
        const ok = liaison === 'chaude' ? v >= RULES.chaudMin : v <= RULES.froidMax;
        let html;
        if (liaison === 'froide' && ok && v > RULES.froidLimite) {
          html = '<p class="pill warn" style="margin-bottom:12px">⚠ Toléré (6–10 °C) : à consommer dans les 2 heures</p>';
        } else if (ok) {
          html = '<p class="pill ok" style="margin-bottom:12px">✔ Conforme</p>';
        } else {
          html = '<p class="pill bad" style="margin-bottom:12px">✘ NON CONFORME</p>' +
            '<p class="muted" style="font-size:13px;margin-bottom:10px">' +
            (liaison === 'chaude' ? 'PMS : recuire/réchauffer jusqu\'à ≥ 63 °C ou détruire.' : 'PMS : &gt; 10 °C → destruction des produits.') + '</p>';
        }
        verdict.innerHTML = html;
        actionField.style.display = ok ? 'none' : 'block';
        return ok;
      };
      tempInput.addEventListener('input', check);
      m.addEventListener('click', () => setTimeout(check, 30));

      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const plat = m.querySelector('[data-f="plat"]').value.trim();
        const v = parseFloat(tempInput.value);
        if (!plat || isNaN(v)) { UI.toast('Renseigne le plat et la température', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        const liaison = UI.segValue(m, 'liaison');
        const ok = liaison === 'chaude' ? v >= RULES.chaudMin : v <= RULES.froidMax;
        const action = m.querySelector('[data-f="action"]').value.trim();
        if (!ok && !action) { UI.toast('Indique l’action corrective', 'bad'); return; }
        await DB.addRecord({
          type: 'service', date: UI.todayISO(), time: UI.nowHM(),
          plat, liaison, service, temp: v, platTemoin: UI.segValue(m, 'temoin') === 'oui',
          tolere: liaison === 'froide' && ok && v > RULES.froidLimite,
          conforme: ok, action: ok ? '' : action, agent,
        });
        close();
        UI.toast('Contrôle enregistré ✔', 'ok');
        render();
      };
    }
  );
}

/* ================================================================
   DÉCONGÉLATION (fiche de décongélation du PMS)
================================================================ */
/** Vrai si le délai d'utilisation d'un produit en décongélation est dépassé. */
function isDecongelDepasse(r) {
  if (r.statut === 'termine' || !r.limite) return false;
  const today = UI.todayISO();
  return r.limite < today || (r.limite === today && r.limiteTime && r.limiteTime < UI.nowHM());
}

VIEWS.decongel = async function (el) {
  const today = UI.todayISO();
  // Les « en cours » restent visibles quel que soit leur âge (à traiter) ; historique limité à 14 jours.
  const recs = alive(await DB.getByType('decongel')).sort((a, b) => (b.date + b.time).localeCompare(a.date + a.time));
  const enCours = recs.filter(r => r.statut !== 'termine');
  const finis = recs.filter(r => r.statut === 'termine' && r.date >= UI.addDays(today, -14));

  const rowHTML = r => {
    const depasse = isDecongelDepasse(r);
    return '<div class="rec-item ' + (depasse ? 'bad' : (r.statut === 'termine' ? '' : 'ok')) + '">' +
      '<div class="big">🧊</div>' +
      '<div class="body"><div class="title">' + UI.esc(r.produit) + (r.fournisseur ? ' <span class="muted">· ' + UI.esc(r.fournisseur) + '</span>' : '') + '</div>' +
      '<div class="meta">Mis à décongeler le ' + UI.frDate(r.date) + ' à ' + UI.esc(r.time) + ' — à utiliser avant le <b>' + UI.frDate(r.limite) + ' ' + UI.esc(r.limiteTime || '') + '</b>' +
      (r.lot ? ' — lot ' + UI.esc(r.lot) : '') + ' — ' + UI.esc(r.agent) +
      (depasse ? ' — ⚠️ DÉLAI DÉPASSÉ : à détruire' : '') + '</div></div>' +
      (r.statut !== 'termine' ? '<button class="btn small secondary" data-fin="' + r.id + '">Utilisé / sorti</button>' : '<span class="pill">Terminé</span>') +
      '</div>';
  };

  el.innerHTML = headerHTML('Décongélation', 'PMS : décongélation en enceinte à 3 °C uniquement (jamais à T° ambiante), utiliser sous 48 h, ne jamais recongeler',
      '<button class="btn" id="new-dec">➕ Mise en décongélation</button>') +
    '<div class="card"><h2>⏳ En cours (' + enCours.length + ')</h2>' +
    (enCours.length ? '<div class="rec-list">' + enCours.map(rowHTML).join('') + '</div>' : '<div class="empty" style="padding:16px">Aucun produit en décongélation.</div>') + '</div>' +
    (finis.length ? '<div class="card"><h2>Terminés (14 jours)</h2><div class="rec-list">' + finis.map(rowHTML).join('') + '</div></div>' : '');

  el.querySelector('#new-dec').addEventListener('click', openDecongelModal);
  el.querySelectorAll('[data-fin]').forEach(b => b.addEventListener('click', async () => {
    const rec = await DB.getRecord(Number(b.dataset.fin));
    if (!rec) return;
    openIssueModal(rec, isDecongelDepasse(rec), 'délai de décongélation (48 h) dépassé');
  }));
};

/** Clôture qualifiée d'un produit suivi (décongélation / entamé) : Utilisé ou
 *  Jeté. Si le délai était dépassé, seul « Jeté » est possible et une
 *  non-conformité clôturée est tracée automatiquement (preuve de destruction). */
function openIssueModal(rec, depasse, motifDepasse) {
  UI.modal(
    '<h2>' + UI.esc(rec.produit) + '</h2>' +
    (depasse ? '<p class="pill bad" style="margin-bottom:12px">⚠ ' + UI.esc(motifDepasse) + ' — le produit doit être jeté</p>' : '') +
    agentField() +
    '<div class="actions">' +
    '<button class="btn ghost" data-x="cancel">Annuler</button>' +
    (depasse ? '' : '<button class="btn" data-x="utilise">✔ Utilisé</button>') +
    '<button class="btn danger" data-x="jete">🗑️ Jeté</button></div>',
    (m, close) => {
      m.querySelector('[data-x="cancel"]').onclick = close;
      const finish = async issue => {
        const agent = requireAgent(m); if (!agent) return;
        rec.statut = 'termine';
        rec.issue = issue;
        rec.sortieDate = UI.todayISO();
        rec.sortieTime = UI.nowHM();
        if (rec.type === 'entame') rec.finDate = UI.todayISO();
        await DB.updateRecord(rec);
        if (issue === 'jete' && depasse) {
          await DB.addRecord({
            type: 'nonconf', date: UI.todayISO(), time: UI.nowHM(),
            objet: 'Produit détruit — ' + rec.produit,
            lieu: '', lot: rec.lot || '', peremption: rec.dlc || rec.limite || '',
            description: motifDepasse,
            action: 'Produit jeté (destruction tracée)', statut: 'cloturee', agent,
          });
        }
        close();
        UI.toast(issue === 'jete' ? 'Produit jeté — tracé ✔' : 'Produit utilisé ✔', 'ok');
        render();
      };
      const btnU = m.querySelector('[data-x="utilise"]');
      if (btnU) btnU.onclick = () => finish('utilise');
      m.querySelector('[data-x="jete"]').onclick = () => finish('jete');
    }
  );
}

function openDecongelModal() {
  const connus = (SETTINGS.fournisseurs || []).map(f => f.name);
  const now = new Date();
  const limiteDate = new Date(now.getTime() + RULES.decongelHeures * 3600 * 1000);
  const limISO = limiteDate.getFullYear() + '-' + String(limiteDate.getMonth() + 1).padStart(2, '0') + '-' + String(limiteDate.getDate()).padStart(2, '0');

  UI.modal(
    '<h2>🧊 Mise en décongélation</h2>' +
    '<label class="field"><span class="lbl">Produit</span><input type="text" data-f="produit" placeholder="Ex. : filets de poisson"></label>' +
    '<div class="row"><div class="grow"><label class="field"><span class="lbl">Fournisseur (optionnel)</span>' +
    '<input type="text" data-f="fournisseur" list="dl-dec-f" autocomplete="off"><datalist id="dl-dec-f">' + connus.map(f => '<option value="' + UI.esc(f) + '">').join('') + '</datalist></label></div>' +
    '<div class="grow"><label class="field"><span class="lbl">N° de lot (optionnel)</span><input type="text" data-f="lot"></label></div></div>' +
    '<label class="field"><span class="lbl">À utiliser avant (48 h par défaut)</span><input type="date" data-f="limite" value="' + limISO + '"></label>' +
    agentField() +
    '<p class="muted" style="font-size:13px">Rappels PMS : décongélation en chambre froide à 3 °C, à l’abri de toute contamination, évacuer l’eau de décongélation, recongélation interdite.</p>' +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const produit = m.querySelector('[data-f="produit"]').value.trim();
        if (!produit) { UI.toast('Indique le produit', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        await DB.addRecord({
          type: 'decongel', date: UI.todayISO(), time: UI.nowHM(),
          produit,
          fournisseur: m.querySelector('[data-f="fournisseur"]').value.trim(),
          lot: m.querySelector('[data-f="lot"]').value.trim(),
          limite: m.querySelector('[data-f="limite"]').value || limISO,
          limiteTime: UI.nowHM(),
          statut: 'encours', agent,
        });
        close();
        UI.toast('Décongélation enregistrée ✔', 'ok');
        render();
      };
    }
  );
}

/* ================================================================
   PRODUITS ENTAMÉS (DLC internes du PMS)
================================================================ */
const ENTAME_TYPES = [
  { label: 'Produits UHT', jours: 3 },
  { label: 'Lait, crème fraîche', jours: 2 },
  { label: 'Mayonnaise industrielle', jours: 21 },
  { label: 'Fromage râpé / cubes', jours: 3 },
  { label: 'Conserves à faible risque', jours: 30 },
  { label: 'Produits IV gamme', jours: 1 },
  { label: 'Produits décongelés', jours: 2 },
  { label: 'Viandes, volailles', jours: 2 },
  { label: 'Charcuterie tranchée', jours: 2 },
  { label: 'Charcuterie non tranchée', jours: 5 },
  { label: 'Produits sous vide', jours: 2 },
  { label: 'Plats cuisinés', jours: 3 },
  { label: 'Excédents', jours: 1 },
];

VIEWS.entames = async function (el) {
  const today = UI.todayISO();
  // Les produits en cours restent visibles quel que soit leur âge ; historique limité à 45 jours.
  const recs = alive(await DB.getByType('entame')).sort((a, b) => (a.dlc || '').localeCompare(b.dlc || ''));
  const actifs = recs.filter(r => r.statut !== 'termine');
  const inactifs = recs.filter(r => r.statut === 'termine' && r.date >= UI.addDays(today, -45));

  const rowHTML = r => {
    const perime = r.statut !== 'termine' && r.dlc && r.dlc < today;
    const bientot = r.statut !== 'termine' && r.dlc === today;
    return '<div class="rec-item ' + (perime ? 'bad' : (r.statut === 'termine' ? '' : 'ok')) + '">' +
      '<div class="big">' + (perime ? '⚠️' : '📦') + '</div>' +
      '<div class="body"><div class="title">' + UI.esc(r.produit) + (r.categorie ? ' <span class="muted">· ' + UI.esc(r.categorie) + '</span>' : '') + '</div>' +
      '<div class="meta">Ouvert le ' + UI.frDate(r.date) + ' — DLC interne : <b>' + UI.frDate(r.dlc) + '</b>' +
      (perime ? ' — ⚠️ DÉPASSÉE : à jeter' : (bientot ? ' — à consommer aujourd\'hui' : '')) + ' — ' + UI.esc(r.agent) + '</div></div>' +
      (r.statut !== 'termine' ? '<button class="btn small secondary" data-fin="' + r.id + '">Consommé / jeté</button>' : '<span class="pill">Terminé</span>') +
      '</div>';
  };

  el.innerHTML = headerHTML('Produits entamés', 'PMS : noter la date d’ouverture, conserver l’étiquette d’origine, stocker au frigo de jour à 3 °C, ne jamais dépasser la DLC d’origine',
      '<button class="btn" id="new-ent">➕ Produit entamé</button>') +
    '<div class="card"><h2>📦 En cours (' + actifs.length + ')</h2>' +
    (actifs.length ? '<div class="rec-list">' + actifs.map(rowHTML).join('') + '</div>' : '<div class="empty" style="padding:16px">Aucun produit entamé suivi.</div>') + '</div>' +
    (inactifs.length ? '<div class="card"><h2>Historique récent</h2><div class="rec-list">' + inactifs.slice(0, 15).map(rowHTML).join('') + '</div></div>' : '');

  el.querySelector('#new-ent').addEventListener('click', openEntameModal);
  el.querySelectorAll('[data-fin]').forEach(b => b.addEventListener('click', async () => {
    const rec = await DB.getRecord(Number(b.dataset.fin));
    if (!rec) return;
    const depasse = rec.dlc && rec.dlc < UI.todayISO();
    openIssueModal(rec, depasse, 'DLC interne dépassée (' + UI.frDate(rec.dlc) + ')');
  }));
};

function openEntameModal() {
  UI.modal(
    '<h2>📦 Nouveau produit entamé</h2>' +
    '<label class="field"><span class="lbl">Produit</span><input type="text" data-f="produit" placeholder="Ex. : crème fraîche 5 L"></label>' +
    '<label class="field"><span class="lbl">Type (fixe la durée de conservation PMS)</span>' +
    '<select data-f="categorie">' + ENTAME_TYPES.map((t, i) => '<option value="' + i + '">' + t.label + ' — ' + t.jours + ' j</option>').join('') + '</select></label>' +
    '<label class="field"><span class="lbl">DLC interne (calculée, modifiable)</span>' +
    '<input type="date" data-f="dlc" value="' + UI.addDays(UI.todayISO(), ENTAME_TYPES[0].jours) + '"></label>' +
    '<p class="muted" style="font-size:13px;margin-bottom:12px">⚠️ La DLC interne ne doit jamais dépasser la DLC/DDM d’origine du produit.</p>' +
    agentField() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      m.querySelector('[data-f="categorie"]').addEventListener('change', e => {
        m.querySelector('[data-f="dlc"]').value = UI.addDays(UI.todayISO(), ENTAME_TYPES[Number(e.target.value)].jours);
      });
      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const produit = m.querySelector('[data-f="produit"]').value.trim();
        if (!produit) { UI.toast('Indique le produit', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        await DB.addRecord({
          type: 'entame', date: UI.todayISO(), time: UI.nowHM(),
          produit,
          categorie: ENTAME_TYPES[Number(m.querySelector('[data-f="categorie"]').value)].label,
          dlc: m.querySelector('[data-f="dlc"]').value,
          statut: 'encours', agent,
        });
        close();
        UI.toast('Produit entamé enregistré ✔', 'ok');
        render();
      };
    }
  );
}

/* ---------- Import de menus (Excel / CSV) ---------- */

// Charge la bibliothèque de lecture Excel à la demande (mise en cache hors ligne).
let _xlsxPromise = null;
function ensureXLSX() {
  if (window.XLSX) return Promise.resolve(window.XLSX);
  if (_xlsxPromise) return _xlsxPromise;
  _xlsxPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'js/vendor/xlsx.full.min.js';
    s.onload = () => resolve(window.XLSX);
    s.onerror = () => {
      // Échec passager (hors ligne au premier usage…) : permettre une nouvelle tentative
      _xlsxPromise = null;
      s.remove();
      reject(new Error('Impossible de charger le lecteur Excel — réessaie'));
    };
    document.head.appendChild(s);
  });
  return _xlsxPromise;
}

// Convertit une cellule (Date, série Excel, ou texte) en date ISO AAAA-MM-JJ, sinon null.
function parseDateCell(v) {
  if (v == null || v === '') return null;
  if (v instanceof Date && !isNaN(v)) {
    return v.getFullYear() + '-' + String(v.getMonth() + 1).padStart(2, '0') + '-' + String(v.getDate()).padStart(2, '0');
  }
  if (typeof v === 'number' && v > 20000 && v < 90000) {
    const ms = Math.round((v - 25569) * 86400 * 1000); // série Excel -> ms UTC
    const d = new Date(ms);
    return d.getUTCFullYear() + '-' + String(d.getUTCMonth() + 1).padStart(2, '0') + '-' + String(d.getUTCDate()).padStart(2, '0');
  }
  const s = String(v).trim();
  let m = s.match(/^(\d{4})[-\/.](\d{1,2})[-\/.](\d{1,2})/); // AAAA-MM-JJ
  if (m) return m[1] + '-' + m[2].padStart(2, '0') + '-' + m[3].padStart(2, '0');
  m = s.match(/^(\d{1,2})[-\/.](\d{1,2})[-\/.](\d{2,4})/);   // JJ/MM/AAAA
  if (m) {
    let y = m[3]; if (y.length === 2) y = '20' + y;
    return y + '-' + m[2].padStart(2, '0') + '-' + m[1].padStart(2, '0');
  }
  return null;
}

// Lit un fichier Excel/CSV : toutes les feuilles, en tableaux de lignes.
async function readWorkbook(file) {
  const XLSX = await ensureXLSX();
  const buf = await file.arrayBuffer();
  const wb = XLSX.read(buf, { type: 'array', cellDates: true });
  return wb.SheetNames.map(name => ({
    name,
    rows: XLSX.utils.sheet_to_json(wb.Sheets[name], { header: 1, raw: true, defval: '' }),
  }));
}

/* ---- Format « grille hebdomadaire » (fichier de menus de l'établissement) ----
 * Une feuille par semaine. Chaque jour est un bloc de 5 lignes ancré sur le nom
 * du jour en colonne A : entrée (n−1), plat (n), garniture (n+1), fromage (n+2),
 * dessert (n+3). Colonne B = midi, colonne D = soir ; la date est en colonne A
 * dans le bloc. */
const JOURS_SEMAINE = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche'];

function isGridMenuWorkbook(sheets) {
  const first = sheets[0];
  if (!first) return false;
  const headerOK = first.rows.slice(0, 3).some(r => r.some(c => /menu du/i.test(String(c))));
  const daysFound = first.rows.filter(r => JOURS_SEMAINE.includes(String(r[0]).trim().toLowerCase())).length;
  return headerOK && daysFound >= 3;
}

const GRID_OFFSET_CAT = { '-1': 'entree', 0: 'plat', 1: 'garniture', 2: 'dessert', 3: 'dessert' };

/** Parcourt toutes les feuilles « grille » et retourne { days, catalog } comme compute(). */
function parseGridMenus(sheets) {
  const days = {};   // date ISO -> { midi:Set, soir:Set }
  const catalog = {}; // nom -> catégorie
  const canon = {};   // nom en minuscules -> première graphie rencontrée (déduplication de casse)
  let skipped = 0;

  sheets.forEach(sheet => {
    const rows = sheet.rows;
    rows.forEach((row, n) => {
      const dayName = String(row[0]).trim().toLowerCase();
      const dayIdx = JOURS_SEMAINE.indexOf(dayName);
      if (dayIdx === -1) return;

      // Date du jour : cellule date en colonne A dans le bloc n−1 .. n+3,
      // sinon reconstruite depuis une date de l'en-tête, recalée sur son lundi
      // réel (l'en-tête peut porter une date de milieu ou de fin de semaine).
      let iso = null;
      for (let r = n - 1; r <= n + 3 && r < rows.length; r++) {
        if (r >= 0 && rows[r]) { const d = parseDateCell(rows[r][0]); if (d) { iso = d; break; } }
      }
      if (!iso) {
        for (const hr of rows.slice(0, 3)) {
          for (const c of hr) {
            const d = parseDateCell(c);
            if (d) {
              const wd = (new Date(d + 'T12:00:00').getDay() + 6) % 7; // 0 = lundi
              iso = UI.addDays(d, dayIdx - wd);
              break;
            }
          }
          if (iso) break;
        }
      }
      if (!iso) { skipped++; return; }

      for (let off = -1; off <= 3; off++) {
        const r = n + off;
        if (r < 0 || r >= rows.length || !rows[r]) continue;
        const cat = GRID_OFFSET_CAT[off];
        [['midi', 1], ['soir', 3]].forEach(([service, col]) => {
          const raw = rows[r][col];
          if (raw == null || raw instanceof Date || parseDateCell(raw)) return; // vraie date égarée en colonne plat
          const brut = String(raw).trim().replace(/\s+/g, ' ');
          if (!brut) return;
          const key = brut.toLowerCase();
          const name = canon[key] || (canon[key] = brut); // graphie canonique unique
          (days[iso] = days[iso] || { midi: new Set(), soir: new Set() })[service].add(name);
          if (!catalog[name]) catalog[name] = cat;
        });
      }
    });
  });

  return { days, catalog, skipped };
}

/** Assistant d'import : mapping des colonnes -> (date, colonnes de plats), aperçu, application. */
function openImportMenu(onDone) {
  UI.modal(
    '<h2>📥 Importer un menu (Excel / CSV)</h2>' +
    '<p class="muted" style="margin-bottom:12px">Sélectionne ton fichier .xlsx ou .csv. Chaque ligne = un jour ; chaque colonne de plat sera rattachée au midi ou au soir.</p>' +
    '<label class="field"><span class="lbl">Fichier</span>' +
    '<input type="file" accept=".xlsx,.xls,.csv" data-f="file" style="min-height:52px;padding:12px;border:1.5px dashed var(--border);border-radius:12px;width:100%"></label>' +
    '<div data-step></div>' +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Fermer</button></div>',
    (m, close) => {
      const step = m.querySelector('[data-step]');
      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-f="file"]').addEventListener('change', async e => {
        const f = e.target.files[0];
        if (!f) return;
        step.innerHTML = '<p class="muted">Lecture du fichier…</p>';
        let sheets;
        try { sheets = await readWorkbook(f); }
        catch (err) { step.innerHTML = '<p class="pill bad">' + UI.esc(err.message) + '</p>'; return; }

        // Format « grille hebdomadaire » de l'établissement : import direct, toutes les semaines
        if (isGridMenuWorkbook(sheets)) {
          const result = parseGridMenus(sheets);
          showGridPreview(step, result, sheets.length, close, onDone);
          return;
        }

        const rows = (sheets[0] ? sheets[0].rows : []).filter(r => r.some(c => String(c).trim() !== ''));
        if (rows.length < 2) { step.innerHTML = '<p class="pill bad">Fichier vide ou illisible.</p>'; return; }
        buildMapping(step, rows, close, onDone);
      });
    }
  );
}

/** Aperçu + application pour le format grille hebdomadaire (détecté automatiquement). */
function showGridPreview(step, result, nSheets, close, onDone) {
  const { days, catalog, skipped } = result;
  const dates = Object.keys(days).sort();
  if (!dates.length) {
    step.innerHTML = '<p class="pill bad">Format « menu hebdomadaire » reconnu, mais aucune date lisible.</p>';
    return;
  }
  const sample = dates.slice(0, 3).map(d =>
    '<div class="rec-item"><div class="body"><div class="title">' + UI.frDate(d) + '</div>' +
    '<div class="meta">Midi : ' + ([...days[d].midi].map(UI.esc).join(', ') || '—') + '<br>Soir : ' + ([...days[d].soir].map(UI.esc).join(', ') || '—') + '</div></div></div>'
  ).join('');

  step.innerHTML = '<hr class="sep">' +
    '<p class="pill ok" style="margin-bottom:10px">✔ Format « menu hebdomadaire » reconnu (' + nSheets + ' semaines)</p>' +
    '<p style="margin-bottom:10px"><b>' + dates.length + ' jours</b> de menus · <b>' + Object.keys(catalog).length + ' plats</b> différents · du ' +
    UI.frDate(dates[0]) + ' au ' + UI.frDate(dates[dates.length - 1]) +
    (skipped ? ' <span class="muted">(' + skipped + ' jour(s) sans date ignoré(s))</span>' : '') + '</p>' +
    '<div class="rec-list" style="margin-bottom:10px">' + sample + '</div>' +
    (dates.length > 3 ? '<p class="muted">… et ' + (dates.length - 3) + ' autres jours.</p>' : '') +
    '<div class="spacer"></div><button class="btn block" data-act="apply">✅ Importer les ' + dates.length + ' jours</button>';

  step.querySelector('[data-act="apply"]').addEventListener('click', async () => {
    const btn = step.querySelector('[data-act="apply"]');
    btn.disabled = true; btn.textContent = 'Import en cours…';
    await applyImport({ days, catalog });
    close();
    UI.toast('Menu de l’année importé : ' + dates.length + ' jours ✔', 'ok');
    if (onDone) onDone();
  });
}

function buildMapping(step, rows, close, onDone) {
  const header = rows[0].map((c, i) => String(c).trim() || ('Colonne ' + (i + 1)));
  const nCols = header.length;
  // Détection automatique de la colonne date (celle dont le plus de cellules sont des dates)
  let dateCol = 0, best = -1;
  for (let c = 0; c < nCols; c++) {
    let hits = 0;
    for (let r = 1; r < Math.min(rows.length, 40); r++) if (parseDateCell(rows[r][c])) hits++;
    if (hits > best) { best = hits; dateCol = c; }
  }
  const colOptions = sel => header.map((h, i) => '<option value="' + i + '"' + (i === sel ? ' selected' : '') + '>' + UI.esc(h) + '</option>').join('');
  const catOptions = PLAT_CATS.map(c => '<option value="' + c.value + '">' + c.label + '</option>').join('');

  // Une ligne de configuration par colonne de plat (toutes sauf la colonne date par défaut)
  const dishRows = header.map((h, i) => ({ col: i, use: i !== dateCol, service: 'midi', cat: guessCat(h) }));

  step.innerHTML =
    '<hr class="sep">' +
    '<label class="field"><span class="lbl">Colonne de la date</span><select data-map="date">' + colOptions(dateCol) + '</select></label>' +
    '<div class="lbl">Colonnes de plats à importer</div>' +
    '<div class="table-wrap"><table><thead><tr><th>Importer</th><th>Colonne</th><th>Service</th><th>Catégorie</th></tr></thead><tbody>' +
    dishRows.map((d, idx) =>
      '<tr data-dish="' + idx + '"><td style="text-align:center"><input type="checkbox" data-c="use" ' + (d.use ? 'checked' : '') + ' style="width:24px;height:24px"></td>' +
      '<td>' + UI.esc(header[d.col]) + '</td>' +
      '<td><select data-c="service"><option value="midi">🌞 Midi</option><option value="soir">🌙 Soir</option></select></td>' +
      '<td><select data-c="cat">' + PLAT_CATS.map(c => '<option value="' + c.value + '"' + (c.value === d.cat ? ' selected' : '') + '>' + c.label + '</option>').join('') + '</select></td></tr>'
    ).join('') + '</tbody></table></div>' +
    '<div class="spacer"></div>' +
    '<button class="btn secondary" data-act="preview">👁️ Aperçu</button>' +
    '<div data-preview style="margin-top:12px"></div>';

  // La colonne date ne doit pas être importée comme plat
  const syncDateExclusion = () => {
    const dc = Number(step.querySelector('[data-map="date"]').value);
    step.querySelectorAll('tr[data-dish]').forEach((tr, idx) => {
      if (dishRows[idx].col === dc) tr.querySelector('[data-c="use"]').checked = false;
    });
  };
  step.querySelector('[data-map="date"]').addEventListener('change', syncDateExclusion);

  const collectConfig = () => {
    const dc = Number(step.querySelector('[data-map="date"]').value);
    const dishes = [];
    step.querySelectorAll('tr[data-dish]').forEach((tr, idx) => {
      if (tr.querySelector('[data-c="use"]').checked && dishRows[idx].col !== dc) {
        dishes.push({ col: dishRows[idx].col, service: tr.querySelector('[data-c="service"]').value, cat: tr.querySelector('[data-c="cat"]').value });
      }
    });
    return { dc, dishes };
  };

  const compute = () => {
    const { dc, dishes } = collectConfig();
    const days = {}; // date -> { midi:Set, soir:Set }
    const catalog = {}; // name -> cat
    for (let r = 1; r < rows.length; r++) {
      const iso = parseDateCell(rows[r][dc]);
      if (!iso) continue;
      dishes.forEach(d => {
        const name = String(rows[r][d.col]).trim();
        if (!name) return;
        (days[iso] = days[iso] || { midi: new Set(), soir: new Set() })[d.service].add(name);
        if (!catalog[name]) catalog[name] = d.cat;
      });
    }
    return { days, catalog };
  };

  step.querySelector('[data-act="preview"]').addEventListener('click', () => {
    const { days, catalog } = compute();
    const dates = Object.keys(days).sort();
    const box = step.querySelector('[data-preview]');
    if (!dates.length) { box.innerHTML = '<p class="pill bad">Aucune date reconnue — vérifie la colonne de la date.</p>'; return; }
    const sample = dates.slice(0, 4).map(d =>
      '<div class="rec-item"><div class="body"><div class="title">' + UI.frDate(d) + '</div>' +
      '<div class="meta">Midi : ' + ([...days[d].midi].map(UI.esc).join(', ') || '—') + '<br>Soir : ' + ([...days[d].soir].map(UI.esc).join(', ') || '—') + '</div></div></div>'
    ).join('');
    box.innerHTML = '<p class="pill ok">' + dates.length + ' jour(s) · ' + Object.keys(catalog).length + ' plat(s) différents</p>' +
      '<div class="rec-list" style="margin-top:10px">' + sample + '</div>' +
      (dates.length > 4 ? '<p class="muted">… et ' + (dates.length - 4) + ' autres jours.</p>' : '') +
      '<div class="spacer"></div><button class="btn block" data-act="apply">✅ Importer ' + dates.length + ' jour(s)</button>';
    box.querySelector('[data-act="apply"]').addEventListener('click', async () => {
      await applyImport(compute());
      close();
      UI.toast('Menu importé : ' + dates.length + ' jour(s) ✔', 'ok');
      if (onDone) onDone();
    });
  });
}

// Devine la catégorie d'une colonne d'après son intitulé.
function guessCat(label) {
  const s = (label || '').toLowerCase();
  if (/entr[ée]e|hors.?d|potage|soupe/.test(s)) return 'entree';
  if (/dessert|fromage|laitage|fruit|compote/.test(s)) return 'dessert';
  if (/garniture|l[ée]gume|f[ée]culent|accompagn/.test(s)) return 'garniture';
  if (/plat|viande|poisson|principal/.test(s)) return 'plat';
  return 'plat';
}

// Applique l'import : ajoute les plats au catalogue et enregistre les menus du jour.
async function applyImport({ days, catalog }) {
  // Catalogue : ajoute les plats manquants
  const known = new Set(SETTINGS.plats.map(p => p.name.toLowerCase()));
  Object.keys(catalog).forEach(name => {
    if (!known.has(name.toLowerCase())) {
      SETTINGS.plats.push({ id: uid(), name, cat: catalog[name] });
      known.add(name.toLowerCase());
    }
  });
  SETTINGS.plats.sort((a, b) => a.name.localeCompare(b.name, 'fr'));
  await saveSettings();

  // Menus du jour : remplace les enregistrements existants pour chaque date+service importé
  for (const iso of Object.keys(days)) {
    const existing = await DB.getByTypeAndRange('menu', iso, iso);
    for (const service of ['midi', 'soir']) {
      const items = [...days[iso][service]];
      if (!items.length) continue;
      const prev = existing.find(m => m.service === service);
      if (prev) { prev.items = items; await DB.updateRecord(prev); }
      else await DB.addRecord({ type: 'menu', date: iso, service, items });
    }
  }
}

/* ================================================================
   MENU (catalogue de plats + menu du jour)
================================================================ */
VIEWS.menu = async function (el) {
  const state = VIEWS.menu._state || (VIEWS.menu._state = { date: UI.todayISO(), service: 'midi', items: null });

  async function loadDayMenu() {
    const menus = await DB.getByTypeAndRange('menu', state.date, state.date);
    const rec = menus.find(m => m.service === state.service);
    state.items = rec ? [...(rec.items || [])] : [];
    state._recId = rec ? rec.id : null;
  }
  if (state.items === null) await loadDayMenu();

  const catByType = {};
  SETTINGS.plats.forEach(p => { (catByType[p.cat] = catByType[p.cat] || []).push(p); });

  el.innerHTML = headerHTML('Menu', 'Saisis tes plats une fois : ils seront proposés automatiquement dans Refroidissement, Remise en T° et Service') +

    '<div class="card"><h2>🍲 Menu du jour</h2>' +
    '<div class="row" style="margin-bottom:12px">' +
    '<div><label class="field" style="margin:0"><span class="lbl">Date</span><input type="date" id="m-date" value="' + state.date + '"></label></div>' +
    '<div class="grow"><label class="field" style="margin:0"><span class="lbl">Service</span>' +
    UI.segHTML('service', [{ value: 'midi', label: '🌞 Midi' }, { value: 'soir', label: '🌙 Soir' }], state.service) + '</label></div>' +
    '</div>' +
    (SETTINGS.plats.length && SETTINGS.plats.length <= 24
      ? '<p class="muted" style="margin-bottom:8px">Coche les plats servis :</p>' +
        PLAT_CATS.filter(c => catByType[c.value]).map(c =>
          '<div style="margin-bottom:8px"><div class="zone" style="margin-bottom:6px">' + c.label + '</div><div class="chips">' +
          catByType[c.value].map(p =>
            '<button type="button" class="menu-pick ' + (state.items.includes(p.name) ? 'on' : '') + '" data-pick="' + UI.esc(p.name) + '">' + UI.esc(p.name) + '</button>'
          ).join('') + '</div></div>'
        ).join('') + '<hr class="sep">'
      : (SETTINGS.plats.length ? '' : '<div class="empty" style="padding:16px">Ajoute des plats au catalogue ci-dessous, ou importe ton fichier Excel.</div>')) +
    '<div class="row"><div class="grow"><input type="text" id="m-free" list="dl-menu-add" placeholder="Ajouter un plat (recherche dans le catalogue ou saisie libre)" autocomplete="off">' +
    '<datalist id="dl-menu-add">' + SETTINGS.plats.map(p => '<option value="' + UI.esc(p.name) + '">').join('') + '</datalist></div>' +
    '<button class="btn small secondary" id="m-free-add">Ajouter au menu</button></div>' +
    '<div id="m-selected" class="row" style="margin-top:12px"></div>' +
    '<div class="spacer"></div><button class="btn" id="m-save">💾 Enregistrer le menu du jour</button></div>' +

    '<div class="card"><h2>📖 Catalogue des plats (' + SETTINGS.plats.length + ')</h2>' +
    '<p class="muted" style="margin-bottom:12px">Les plats réutilisables d’un jour à l’autre. Ils alimentent les listes déroulantes.</p>' +
    '<div class="row" style="margin-bottom:12px"><button class="btn small" id="m-import">📥 Importer un menu (Excel / CSV)</button>' +
    (SETTINGS.plats.length ? '<button class="btn small ghost" id="m-clear">🗑️ Vider le catalogue</button>' : '') + '</div>' +
    (SETTINGS.plats.length
      ? PLAT_CATS.filter(c => catByType[c.value]).map(c =>
          '<div style="margin-bottom:10px"><div class="zone" style="margin-bottom:6px">' + c.label + '</div><div class="rec-list">' +
          catByType[c.value].map(p =>
            '<div class="rec-item"><div class="body"><div class="title">' + UI.esc(p.name) + '</div></div>' +
            '<button class="btn small ghost" data-del-plat="' + p.id + '">🗑️</button></div>'
          ).join('') + '</div></div>'
        ).join('')
      : '<div class="empty" style="padding:16px">Aucun plat dans le catalogue.</div>') +
    '<hr class="sep"><div class="row"><div class="grow"><input type="text" id="p-name" placeholder="Nom du plat"></div>' +
    '<div><select id="p-cat">' + PLAT_CATS.map(c => '<option value="' + c.value + '">' + c.label + '</option>').join('') + '</select></div>' +
    '<button class="btn small" id="p-add">➕ Ajouter</button></div></div>';

  UI.segWire(el);

  function renderSelected() {
    const box = el.querySelector('#m-selected');
    box.innerHTML = state.items.length
      ? state.items.map(n => '<span class="pill info">' + UI.esc(n) + ' <button data-unpick="' + UI.esc(n) + '" style="border:none;background:none;cursor:pointer;font-size:15px">✕</button></span>').join('')
      : '<span class="muted">Aucun plat sélectionné pour ce service.</span>';
    box.querySelectorAll('[data-unpick]').forEach(b => b.addEventListener('click', () => {
      state.items = state.items.filter(x => x !== b.dataset.unpick);
      el.querySelectorAll('[data-pick]').forEach(p => { if (p.dataset.pick === b.dataset.unpick) p.classList.remove('on'); });
      renderSelected();
    }));
  }
  renderSelected();

  el.querySelectorAll('[data-pick]').forEach(btn => btn.addEventListener('click', () => {
    const n = btn.dataset.pick;
    if (state.items.includes(n)) { state.items = state.items.filter(x => x !== n); btn.classList.remove('on'); }
    else { state.items.push(n); btn.classList.add('on'); }
    renderSelected();
  }));

  el.querySelector('#m-date').addEventListener('change', async e => { state.date = e.target.value; await loadDayMenu(); render(); });
  el.querySelector('.seg[data-seg="service"]').addEventListener('click', () => setTimeout(async () => {
    const v = UI.segValue(el, 'service');
    if (v && v !== state.service) { state.service = v; await loadDayMenu(); render(); }
  }, 30));

  el.querySelector('#m-free-add').addEventListener('click', () => {
    const v = el.querySelector('#m-free').value.trim();
    if (!v) return;
    if (!state.items.includes(v)) state.items.push(v);
    el.querySelector('#m-free').value = '';
    renderSelected();
  });

  el.querySelector('#m-save').addEventListener('click', async () => {
    const rec = { type: 'menu', date: state.date, service: state.service, items: state.items };
    if (state._recId) { rec.id = state._recId; await DB.updateRecord(rec); }
    else { state._recId = await DB.addRecord(rec); }
    UI.toast('Menu du ' + UI.frDate(state.date) + ' (' + (state.service === 'midi' ? 'midi' : 'soir') + ') enregistré ✔', 'ok');
  });

  el.querySelector('#p-add').addEventListener('click', async () => {
    const name = el.querySelector('#p-name').value.trim();
    if (!name) { UI.toast('Indique le nom du plat', 'bad'); return; }
    if (SETTINGS.plats.some(p => p.name.toLowerCase() === name.toLowerCase())) { UI.toast('Ce plat existe déjà', 'bad'); return; }
    SETTINGS.plats.push({ id: uid(), name, cat: el.querySelector('#p-cat').value });
    SETTINGS.plats.sort((a, b) => a.name.localeCompare(b.name, 'fr'));
    await saveSettings(); render();
  });
  el.querySelectorAll('[data-del-plat]').forEach(b => b.addEventListener('click', async () => {
    SETTINGS.plats = SETTINGS.plats.filter(p => p.id !== b.dataset.delPlat);
    await saveSettings(); render();
  }));

  el.querySelector('#m-import').addEventListener('click', () => openImportMenu(() => { state.items = null; render(); }));
  const clearBtn = el.querySelector('#m-clear');
  if (clearBtn) clearBtn.addEventListener('click', () => UI.confirm('Vider tout le catalogue de plats ? Les menus déjà enregistrés par jour sont conservés.', async () => {
    SETTINGS.plats = []; await saveSettings(); render();
  }));
};

/* ================================================================
   TRAÇABILITÉ ÉTIQUETTES (photos)
================================================================ */
/** Lundi de la semaine d'une date ISO (AAAA-MM-JJ). */
function mondayOf(iso) {
  const d = new Date(iso + 'T12:00:00');
  return UI.addDays(iso, -((d.getDay() + 6) % 7));
}

VIEWS.tracabilite = async function (el) {
  const today = UI.todayISO();
  const from = UI.addDays(today, -35);
  const [recs, menus] = await Promise.all([
    DB.getByTypeAndRange('etiquette', from, today),
    DB.getByTypeAndRange('menu', from, today),
  ]);
  recs.sort((a, b) => (b.date + (b.time || '')).localeCompare(a.date + (a.time || '')));

  // Menus par jour (midi + soir) pour rapprocher étiquettes et menu, comme le classeur hebdomadaire du PMS
  const menuByDay = {};
  menus.forEach(mn => {
    menuByDay[mn.date] = menuByDay[mn.date] || {};
    menuByDay[mn.date][mn.service] = mn.items || [];
  });

  // Regroupement par semaine (lundi → dimanche), puis par jour
  const weeks = [];
  const byWeek = {};
  recs.forEach(r => {
    const wk = mondayOf(r.date);
    if (!byWeek[wk]) { byWeek[wk] = {}; weeks.push(wk); }
    (byWeek[wk][r.date] = byWeek[wk][r.date] || []).push(r);
  });

  const JOURS = ['dimanche', 'lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi'];
  const dayName = iso => JOURS[new Date(iso + 'T12:00:00').getDay()];

  const cardHTML = r =>
    '<div class="photo-card" data-id="' + r.id + '">' +
    (r.photo ? '<img src="' + UI.esc(r.photo) + '" alt="étiquette">' : '<div style="height:120px;display:flex;align-items:center;justify-content:center;font-size:40px;background:#eef">🏷️</div>') +
    '<div class="cap"><b>' + UI.esc(r.produit || 'Produit') + '</b>🕐 ' + UI.frDate(r.date) + (r.time ? ' à ' + UI.esc(r.time) : '') + (r.dlc ? ' · DLC ' + UI.frDate(r.dlc) : '') + '</div></div>';

  const weekHTML = wk => {
    const days = Object.keys(byWeek[wk]).sort().reverse();
    const count = days.reduce((n, d) => n + byWeek[wk][d].length, 0);
    return '<div class="card"><h2>📅 Semaine du ' + UI.frDate(wk) + ' au ' + UI.frDate(UI.addDays(wk, 6)) + ' <span class="pill info">' + count + ' étiquette' + (count > 1 ? 's' : '') + '</span></h2>' +
      days.map(d => {
        const mn = menuByDay[d];
        const parts = mn ? ['midi', 'soir'].filter(s => mn[s] && mn[s].length)
          .map(s => (s === 'midi' ? '🌞 ' : '🌙 ') + mn[s].map(UI.esc).join(', ')) : [];
        const menuLine = parts.length
          ? '<div class="muted" style="font-size:13px;margin:2px 0 8px">🍲 Menu : ' + parts.join(' · ') + '</div>'
          : '';
        return '<div style="margin-bottom:14px"><div style="font-weight:700;text-transform:capitalize">' + dayName(d) + ' ' + UI.frDate(d) + '</div>' +
          menuLine + '<div class="photo-grid">' + byWeek[wk][d].map(cardHTML).join('') + '</div></div>';
      }).join('') + '</div>';
  };

  el.innerHTML = headerHTML('Traçabilité des étiquettes', 'Classées par semaine avec le menu correspondant (PMS : classeur hebdomadaire) — 5 dernières semaines',
      '<button class="btn" id="new-eti">📷 Nouvelle étiquette</button>') +
    (recs.length ? weeks.map(weekHTML).join('') : '<div class="empty"><span class="e-ico">🏷️</span>Aucune étiquette enregistrée.</div>');

  el.querySelector('#new-eti').addEventListener('click', openEtiquetteModal);
  el.querySelectorAll('.photo-card').forEach(c => c.addEventListener('click', () => openEtiquetteDetail(Number(c.dataset.id))));
};

/* ---------- OCR des étiquettes (Tesseract.js, 100 % hors ligne) ---------- */
let _ocrWorkerPromise = null;
function ensureOCR() {
  if (_ocrWorkerPromise) return _ocrWorkerPromise;
  _ocrWorkerPromise = (async () => {
    if (!window.Tesseract) {
      await new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = 'js/vendor/ocr/tesseract.min.js';
        s.onload = resolve;
        s.onerror = () => { s.remove(); reject(new Error('Lecteur OCR indisponible')); };
        document.head.appendChild(s);
      });
    }
    return Tesseract.createWorker('fra', 1, {
      workerPath: 'js/vendor/ocr/worker.min.js',
      corePath: 'js/vendor/ocr',
      langPath: 'js/vendor/ocr',
      gzip: true,
    });
  })().catch(e => { _ocrWorkerPromise = null; throw e; });
  return _ocrWorkerPromise;
}

/** Extrait produit / n° de lot / DLC du texte OCR d'une étiquette alimentaire. */
function parseEtiquetteOCR(texte) {
  const brut = String(texte || '');
  const lignes = brut.split(/\n+/).map(l => l.trim()).filter(l => l.length > 1);
  const res = { produit: '', lot: '', dlc: '' };

  // --- Dates candidates : JJ/MM/AAAA, JJ.MM.AA, JJ-MM-AAAA, AAAA-MM-JJ ---
  const today = UI.todayISO();
  const dates = [];
  const reDate = /(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2,4})|(\d{4})-(\d{2})-(\d{2})/g;
  const KW_DLC = /(dlc|ddm|[aà] consommer|consommer (jusqu|avant)|exp|use by|best before|bbd|p[ée]remption)/i;
  lignes.forEach(l => {
    let m;
    reDate.lastIndex = 0;
    while ((m = reDate.exec(l))) {
      let iso = null;
      if (m[4]) {
        iso = m[4] + '-' + m[5] + '-' + m[6];
      } else {
        let [, j, mo, a] = m;
        if (Number(mo) > 12 && Number(j) <= 12) { const t = j; j = mo; mo = t; } // format inversé
        if (a.length === 2) a = (Number(a) > 70 ? '19' : '20') + a;
        if (Number(mo) >= 1 && Number(mo) <= 12 && Number(j) >= 1 && Number(j) <= 31) {
          iso = a + '-' + String(mo).padStart(2, '0') + '-' + String(j).padStart(2, '0');
        }
      }
      // plausible : entre il y a 1 an et dans 5 ans
      if (iso && iso > UI.addDays(today, -365) && iso < UI.addDays(today, 5 * 365)) {
        dates.push({ iso, kw: KW_DLC.test(l), future: iso >= today });
      }
    }
  });
  // priorité : date future près d'un mot-clé DLC > future la plus proche > n'importe laquelle
  dates.sort((a, b) => (b.kw - a.kw) || (b.future - a.future) || a.iso.localeCompare(b.iso));
  if (dates.length) res.dlc = dates[0].iso;

  // --- N° de lot : après un mot-clé LOT/BATCH, ou motif L+chiffres ---
  const reLotKw = /(?:lot|batch|n[°o]\s*lot)\s*[:n°o.]*\s*([A-Z0-9][A-Z0-9\-\/.]{2,15})/i;
  const reLotL = /\bL[ :.]?([0-9][0-9A-Z\-\/]{3,12})\b/;
  for (const l of lignes) {
    const m1 = reLotKw.exec(l);
    if (m1) { res.lot = m1[1].replace(/[.,:]+$/, ''); break; }
  }
  if (!res.lot) {
    for (const l of lignes) {
      const m2 = reLotL.exec(l);
      if (m2) { res.lot = 'L' + m2[1]; break; }
    }
  }

  // --- Produit : parmi les premières lignes, la plus « riche en lettres » qui
  // ne ressemble ni à une date, ni à un lot, ni à un poids/prix ---
  const REJET = /(dlc|ddm|lot|batch|exp|kg\b|\bg\b|€|\bpoids|net|conserver|consommer|ingr[ée]dients|\d{1,2}[\/.\-]\d{1,2})/i;
  let best = '', bestScore = 0;
  lignes.slice(0, 8).forEach((l, i) => {
    const lettres = (l.match(/[A-Za-zÀ-ÿ]/g) || []).length;
    if (lettres < 4 || REJET.test(l)) return;
    const score = lettres * (i < 3 ? 2 : 1); // les premières lignes sont souvent le nom
    if (score > bestScore) { bestScore = score; best = l; }
  });
  res.produit = best.replace(/\s+/g, ' ').trim().slice(0, 60);

  return res;
}

function openEtiquetteModal() {
  UI.modal(
    '<h2>🏷️ Nouvelle étiquette <span class="pill info" data-count style="display:none"></span></h2>' +
    '<label class="field"><span class="lbl">Photo de l’étiquette</span>' +
    '<input type="file" accept="image/*" capture="environment" data-f="photo" style="min-height:52px;padding:12px;border:1.5px dashed var(--border);border-radius:12px;width:100%"></label>' +
    '<div data-preview style="margin-bottom:8px"></div>' +
    '<div data-ocr style="margin-bottom:12px"></div>' +
    '<label class="field"><span class="lbl">Produit</span><input type="text" data-f="produit" placeholder="Ex. : escalope de dinde"></label>' +
    '<div class="row"><div class="grow"><label class="field"><span class="lbl">N° de lot (optionnel)</span><input type="text" data-f="lot"></label></div>' +
    '<div class="grow"><label class="field"><span class="lbl">DLC / DDM (optionnel)</span><input type="date" data-f="dlc"></label></div></div>' +
    agentField() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Fermer</button>' +
    '<button class="btn secondary" data-x="next">💾 + 📷 Suivante</button>' +
    '<button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      let photoData = null;
      let saved = 0;
      let ocrSeq = 0; // ignore le résultat d'une photo remplacée entre-temps
      const photoInput = m.querySelector('[data-f="photo"]');
      const ocrStatus = m.querySelector('[data-ocr]');

      // Lecture automatique de l'étiquette (OCR hors ligne) : pré-remplit les
      // champs vides — l'agent vérifie et corrige.
      const runOCR = async () => {
        if (!photoData) return;
        const seq = ++ocrSeq;
        ocrStatus.innerHTML = '<span class="pill info">🔍 Lecture de l’étiquette en cours…</span>';
        try {
          const worker = await ensureOCR();
          const { data } = await worker.recognize(photoData);
          if (seq !== ocrSeq || !m.isConnected) return;
          const found = parseEtiquetteOCR(data.text);
          const filled = [];
          const fProduit = m.querySelector('[data-f="produit"]');
          const fLot = m.querySelector('[data-f="lot"]');
          const fDlc = m.querySelector('[data-f="dlc"]');
          if (found.produit && !fProduit.value.trim()) { fProduit.value = found.produit; filled.push('produit'); }
          if (found.lot && !fLot.value.trim()) { fLot.value = found.lot; filled.push('lot'); }
          if (found.dlc && !fDlc.value) { fDlc.value = found.dlc; filled.push('DLC'); }
          ocrStatus.innerHTML = filled.length
            ? '<span class="pill ok">✔ OCR : ' + filled.join(', ') + ' pré-rempli(s) — vérifie avant d’enregistrer</span>'
            : '<span class="pill warn">OCR : rien de lisible détecté — saisis les champs à la main</span>';
        } catch (e) {
          if (seq === ocrSeq && m.isConnected) ocrStatus.innerHTML = '<span class="pill warn">OCR indisponible (' + UI.esc(e.message) + ')</span>';
        }
      };

      photoInput.addEventListener('change', async e => {
        const f = e.target.files[0];
        if (!f) return;
        try {
          photoData = await UI.shrinkImage(f, 1000);
          m.querySelector('[data-preview]').innerHTML = '<img src="' + photoData + '" class="photo-full" style="max-height:220px">';
          runOCR();
        } catch { UI.toast('Impossible de lire la photo', 'bad'); }
      });

      const save = async () => {
        const produit = m.querySelector('[data-f="produit"]').value.trim();
        if (!photoData && !produit) { UI.toast('Ajoute une photo ou le nom du produit', 'bad'); return false; }
        const agent = requireAgent(m); if (!agent) return false;
        await DB.addRecord({
          type: 'etiquette', date: UI.todayISO(), time: UI.nowHM(),
          produit, photo: photoData,
          lot: m.querySelector('[data-f="lot"]').value.trim(),
          dlc: m.querySelector('[data-f="dlc"]').value,
          agent,
        });
        saved++;
        return true;
      };

      m.querySelector('[data-x="cancel"]').onclick = () => { close(); if (saved) render(); };
      m.querySelector('[data-x="save"]').onclick = async () => {
        if (!(await save())) return;
        close();
        UI.toast('Étiquette enregistrée ✔', 'ok');
        render();
      };
      // Enregistrer puis enchaîner directement sur la photo suivante (réceptions en rafale)
      m.querySelector('[data-x="next"]').onclick = async () => {
        if (!(await save())) return;
        photoData = null;
        ocrSeq++; // un OCR encore en cours sur l'ancienne photo est abandonné
        m.querySelector('[data-preview]').innerHTML = '';
        ocrStatus.innerHTML = '';
        m.querySelector('[data-f="produit"]').value = '';
        m.querySelector('[data-f="lot"]').value = '';
        m.querySelector('[data-f="dlc"]').value = '';
        photoInput.value = '';
        const counter = m.querySelector('[data-count]');
        counter.style.display = '';
        counter.textContent = saved + ' enregistrée' + (saved > 1 ? 's' : '');
        UI.toast('Étiquette ' + saved + ' enregistrée ✔', 'ok');
        photoInput.click();
      };
    }
  );
}

async function openEtiquetteDetail(id) {
  const r = await DB.getRecord(id);
  if (!r) return;
  UI.modal(
    '<h2>' + UI.esc(r.produit || 'Étiquette') + '</h2>' +
    (r.photo ? '<img src="' + UI.esc(r.photo) + '" class="photo-full">' : '') +
    '<p style="margin-top:12px">' + UI.frDate(r.date) + ' ' + UI.esc(r.time || '') +
    (r.lot ? ' · Lot : <b>' + UI.esc(r.lot) + '</b>' : '') +
    (r.dlc ? ' · DLC : <b>' + UI.frDate(r.dlc) + '</b>' : '') +
    '<br><span class="muted">Enregistré par ' + UI.esc(r.agent) + '</span></p>' +
    '<div class="actions"><button class="btn danger" data-x="del">🗑️ Supprimer</button><button class="btn ghost" data-x="close">Fermer</button></div>',
    (m, close) => {
      m.querySelector('[data-x="close"]').onclick = close;
      m.querySelector('[data-x="del"]').onclick = () => UI.confirm('Supprimer cette étiquette ?', async () => {
        await DB.deleteRecord(id); close(); UI.toast('Étiquette supprimée'); render();
      });
    }
  );
}

/* ================================================================
   PLAN DE NETTOYAGE
================================================================ */
VIEWS.nettoyage = async function (el) {
  const today = UI.todayISO();
  const recent = alive(await DB.getByTypeAndRange('nettoyage', UI.addDays(today, -31), today));
  const FREQ_LABEL = { quotidien: 'Quotidien', hebdomadaire: 'Hebdo', mensuel: 'Mensuel' };
  const FREQ_DAYS = { quotidien: 0, hebdomadaire: 6, mensuel: 30 };

  const lastDone = {};
  recent.forEach(r => {
    if (!lastDone[r.taskId] || r.date > lastDone[r.taskId].date) lastDone[r.taskId] = r;
  });

  // Regroupement par zone, comme les fiches de suivi nettoyage/désinfection du PMS
  const FREQ_ORDER = { quotidien: 0, hebdomadaire: 1, mensuel: 2 };
  const zones = [];
  const byZone = {};
  SETTINGS.cleaningTasks.forEach(t => {
    if (!byZone[t.zone]) { byZone[t.zone] = []; zones.push(t.zone); }
    byZone[t.zone].push(t);
  });

  const taskHTML = t => {
    const last = lastDone[t.id];
    const isDone = last && last.date >= UI.addDays(today, -FREQ_DAYS[t.freq]);
    const doneToday = last && last.date === today;
    return '<div class="task-row ' + (isDone ? 'done' : '') + '" data-task="' + t.id + '">' +
      '<button class="check" data-check="' + t.id + '" data-donetoday="' + (doneToday ? '1' : '') + '">✔</button>' +
      '<div class="body" style="flex:1"><div class="tname">' + UI.esc(t.name) + '</div>' +
      '<div class="zone"><span class="tag-freq">' + FREQ_LABEL[t.freq] + '</span>' +
      (last ? ' · fait le ' + UI.frDate(last.date) + ' par ' + UI.esc(last.agent) : ' · jamais fait') + '</div></div></div>';
  };

  const isDue = t => {
    const last = lastDone[t.id];
    return !(last && last.date >= UI.addDays(today, -FREQ_DAYS[t.freq]));
  };

  const sections = zones.map(z => {
    const tasks = byZone[z].slice().sort((a, b) => FREQ_ORDER[a.freq] - FREQ_ORDER[b.freq] || a.name.localeCompare(b.name, 'fr'));
    const due = tasks.filter(isDue).length;
    const dueDaily = tasks.filter(t => t.freq === 'quotidien' && isDue(t)).length;
    return '<div class="card"><h2>🧽 ' + UI.esc(z) + ' ' + (due ? '<span class="pill warn">' + due + ' à faire</span>' : '<span class="pill ok">à jour</span>') +
      (dueDaily > 1 ? ' <button class="btn small secondary" data-checkzone="' + UI.esc(z) + '" style="float:right">✔ Tout le quotidien (' + dueDaily + ')</button>' : '') + '</h2>' +
      tasks.map(taskHTML).join('') + '</div>';
  }).join('');

  el.innerHTML = headerHTML('Plan de nettoyage & désinfection', 'Fiches de suivi par zone (PMS) — coche chaque tâche réalisée, traçabilité date + agent') +
    (SETTINGS.cleaningTasks.length ? sections : '<div class="empty"><span class="e-ico">🧽</span>Ajoute les tâches de nettoyage dans les Réglages.</div>');

  el.querySelectorAll('[data-check]').forEach(btn => btn.addEventListener('click', async () => {
    const taskId = btn.dataset.check;
    const task = SETTINGS.cleaningTasks.find(t => t.id === taskId);
    if (!task) return;
    // Date prise au moment du clic (la vue peut rester ouverte au passage de minuit)
    const clickDay = UI.todayISO();

    if (btn.dataset.donetoday === '1') {
      // décocher : supprime l'enregistrement du jour
      const todays = (await DB.getByTypeAndRange('nettoyage', clickDay, clickDay)).filter(r => r.taskId === taskId);
      for (const r of todays) await DB.deleteRecord(r.id);
      UI.toast('Tâche décochée');
      render();
      return;
    }

    const doSave = async agent => {
      await DB.addRecord({
        type: 'nettoyage', date: clickDay, time: UI.nowHM(),
        taskId, taskName: task.name, zone: task.zone, freq: task.freq, agent,
      });
      UI.toast(task.name + ' ✔', 'ok');
      render();
    };

    const agent = getCurrentAgent();
    if (agent) { doSave(agent); return; }
    UI.modal(
      '<h2>Qui a réalisé « ' + UI.esc(task.name) + ' » ?</h2>' + agentField('') +
      '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Valider</button></div>',
      (m, close) => {
        m.querySelector('[data-x="cancel"]').onclick = close;
        m.querySelector('[data-x="save"]').onclick = () => {
          const a = requireAgent(m); if (!a) return;
          close(); doSave(a);
        };
      }
    );
  }));

  // « Tout cocher » les tâches quotidiennes dues d'une zone en une fois
  el.querySelectorAll('[data-checkzone]').forEach(btn => btn.addEventListener('click', () => {
    const zone = btn.dataset.checkzone;
    const clickDay = UI.todayISO();
    const todo = SETTINGS.cleaningTasks.filter(t => t.zone === zone && t.freq === 'quotidien' && isDue(t));
    if (!todo.length) return;

    const doSaveAll = async agent => {
      for (const t of todo) {
        await DB.addRecord({
          type: 'nettoyage', date: clickDay, time: UI.nowHM(),
          taskId: t.id, taskName: t.name, zone: t.zone, freq: t.freq, agent,
        });
      }
      UI.toast(zone + ' : ' + todo.length + ' tâches cochées ✔', 'ok');
      render();
    };

    const agent = getCurrentAgent();
    if (agent) { doSaveAll(agent); return; }
    UI.modal(
      '<h2>Qui a réalisé le nettoyage « ' + UI.esc(zone) + ' » ?</h2>' + agentField('') +
      '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Valider</button></div>',
      (m, close) => {
        m.querySelector('[data-x="cancel"]').onclick = close;
        m.querySelector('[data-x="save"]').onclick = () => {
          const a = requireAgent(m); if (!a) return;
          close(); doSaveAll(a);
        };
      }
    );
  }));
};

/* ================================================================
   HUILES DE FRITURE
================================================================ */
VIEWS.huiles = async function (el) {
  const today = UI.todayISO();
  const recs = alive(await DB.getByTypeAndRange('huile', UI.addDays(today, -30), today)).sort((a, b) => (b.date + b.time).localeCompare(a.date + a.time));
  const ACTION_LABEL = { controle: 'Contrôle visuel', filtration: 'Filtration', changement: 'Changement d’huile' };
  const ETAT_LABEL = { bon: 'Bonne', moyen: 'À surveiller', 'a-changer': 'À changer' };

  el.innerHTML = headerHTML('Huiles de friture', 'Contrôle visuel quotidien · friture ≤ 175 °C · changer une huile foncée ou moussante',
      '<button class="btn" id="new-huile">➕ Nouveau contrôle</button>') +
    (recs.length ? '<div class="rec-list">' + recs.map(r =>
      '<div class="rec-item ' + (r.conforme === false || (r.etat === 'a-changer' && r.action !== 'changement') ? 'bad' : 'ok') + '">' +
      '<div class="big">🍟</div>' +
      '<div class="body"><div class="title">' + UI.esc(r.friteuse) + ' — ' + (ACTION_LABEL[r.action] || r.action) + '</div>' +
      '<div class="meta">' + UI.frDate(r.date) + ' ' + UI.esc(r.time) + ' — huile : ' + (ETAT_LABEL[r.etat] || r.etat) +
      (r.polaires === 'nok' ? ' — polaires > 25 % ⚠️' : (r.polaires === 'ok' ? ' — polaires ≤ 25 % ✔' : '')) +
      (r.polairesPct != null ? ' (' + r.polairesPct + ' %)' : '') +
      (r.volume != null || r.destination ? ' — usagée : ' + (r.volume != null ? r.volume + ' L ' : '') + UI.esc(r.destination || '') + (r.bon ? ' (bon ' + UI.esc(r.bon) + ')' : '') : '') +
      (r.temp != null ? ' — ' + UI.fmtTemp(r.temp) : '') + ' — ' + UI.esc(r.agent) +
      (r.remarque ? ' — ' + UI.esc(r.remarque) : '') + '</div></div>' +
      '<span class="pill ' + (r.conforme === false ? 'bad' : (r.etat === 'a-changer' ? 'warn' : 'ok')) + '">' + (r.conforme === false ? 'Non conforme' : (ETAT_LABEL[r.etat] || '')) + '</span></div>'
    ).join('') + '</div>' : '<div class="empty"><span class="e-ico">🍟</span>Aucun contrôle d’huile enregistré ce mois-ci.</div>');

  el.querySelector('#new-huile').addEventListener('click', openHuileModal);
};

function openHuileModal() {
  UI.modal(
    '<h2>🍟 Contrôle huile de friture</h2>' +
    '<label class="field"><span class="lbl">Friteuse</span><select data-f="friteuse">' +
    SETTINGS.friteuses.map(f => '<option>' + UI.esc(f) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span class="lbl">Opération</span>' +
    UI.segHTML('action', [
      { value: 'controle', label: '👁️ Contrôle' },
      { value: 'filtration', label: '🫗 Filtration' },
      { value: 'changement', label: '🔄 Changement' },
    ], 'controle') + '</label>' +
    '<label class="field"><span class="lbl">État de l’huile</span>' +
    UI.segHTML('etat', [
      { value: 'bon', label: '✔ Bonne' },
      { value: 'moyen', label: '≈ À surveiller' },
      { value: 'a-changer', label: '✘ À changer', bad: true },
    ], 'bon') + '</label>' +
    '<label class="field"><span class="lbl">Test composés polaires (critère réglementaire : ≤ 25 %)</span>' +
    UI.segHTML('polaires', [
      { value: '', label: 'Non testé' },
      { value: 'ok', label: '✔ ≤ 25 %' },
      { value: 'nok', label: '✘ > 25 %', bad: true },
    ], '') + '</label>' +
    '<label class="field"><span class="lbl">Valeur mesurée (%, optionnel)</span>' +
    '<input type="number" step="0.5" inputmode="decimal" data-f="polairesPct" placeholder="Ex. : 18"></label>' +
    '<div data-elimination style="display:none">' +
    '<hr class="sep"><div class="lbl" style="margin-bottom:8px">Traçabilité de l’huile usagée (changement)</div>' +
    '<div class="row"><div class="grow"><label class="field"><span class="lbl">Volume (L)</span><input type="number" step="0.5" inputmode="decimal" data-f="volume"></label></div>' +
    '<div class="grow"><label class="field"><span class="lbl">Destination / collecteur</span><input type="text" data-f="destination" placeholder="Ex. : bac de récupération, Oleovia…"></label></div></div>' +
    '<label class="field"><span class="lbl">N° de bon d’enlèvement (optionnel)</span><input type="text" data-f="bon"></label>' +
    '</div>' +
    '<label class="field"><span class="lbl">Température de friture (°C, optionnel)</span>' +
    '<input type="number" step="1" inputmode="numeric" data-f="temp" placeholder="≤ 175"></label>' +
    '<label class="field"><span class="lbl">Remarque (optionnel)</span><input type="text" data-f="remarque"></label>' +
    agentField() +
    '<div data-verdict></div>' +
    actionFieldHTML() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      UI.segWire(m);
      const verdict = m.querySelector('[data-verdict]');
      const actionField = m.querySelector('[data-action-field]');
      const elimination = m.querySelector('[data-elimination]');

      // Verdict : > 25 % de composés polaires (ou % saisi > 25) = huile impropre
      const evalHuile = () => {
        const pct = parseFloat(m.querySelector('[data-f="polairesPct"]').value);
        const seg = UI.segValue(m, 'polaires') || '';
        const nok = seg === 'nok' || (!isNaN(pct) && pct > 25);
        verdict.innerHTML = nok
          ? '<p class="pill bad" style="margin-bottom:12px">✘ NON CONFORME — huile impropre (&gt; 25 % de composés polaires) : changement obligatoire</p>'
          : (seg === 'ok' || !isNaN(pct) ? '<p class="pill ok" style="margin-bottom:12px">✔ Polarité conforme</p>' : '');
        actionField.style.display = nok ? 'block' : 'none';
        elimination.style.display = UI.segValue(m, 'action') === 'changement' ? 'block' : 'none';
        return nok;
      };
      m.addEventListener('click', () => setTimeout(evalHuile, 30));
      m.querySelector('[data-f="polairesPct"]').addEventListener('input', evalHuile);

      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const agent = requireAgent(m); if (!agent) return;
        const nok = evalHuile();
        const action = m.querySelector('[data-f="action"]').value.trim();
        if (nok && !action) { UI.toast('Indique l’action corrective (changement de l’huile…)', 'bad'); return; }
        const t = parseFloat(m.querySelector('[data-f="temp"]').value);
        const pct = parseFloat(m.querySelector('[data-f="polairesPct"]').value);
        const vol = parseFloat(m.querySelector('[data-f="volume"]').value);
        await DB.addRecord({
          type: 'huile', date: UI.todayISO(), time: UI.nowHM(),
          friteuse: m.querySelector('[data-f="friteuse"]').value,
          action: UI.segValue(m, 'action'), etat: UI.segValue(m, 'etat'),
          polaires: UI.segValue(m, 'polaires') || '',
          polairesPct: isNaN(pct) ? null : pct,
          volume: isNaN(vol) ? null : vol,
          destination: m.querySelector('[data-f="destination"]').value.trim(),
          bon: m.querySelector('[data-f="bon"]').value.trim(),
          temp: isNaN(t) ? null : t,
          remarque: m.querySelector('[data-f="remarque"]').value.trim(),
          conforme: !nok, actionCorrective: nok ? action : '',
          agent,
        });
        close();
        UI.toast(nok ? 'Non-conformité huile enregistrée' : 'Contrôle enregistré ✔', nok ? 'bad' : 'ok');
        render();
      };
    }
  );
}

/* ================================================================
   NON-CONFORMITÉS
================================================================ */
VIEWS.nonconformites = async function (el) {
  const today = UI.todayISO();
  const from = UI.addDays(today, -30);
  const [manual, temps, receptions, services, refroids, huiles] = await Promise.all([
    DB.getByType('nonconf'),
    DB.getByTypeAndRange('temp', from, today),
    DB.getByTypeAndRange('reception', from, today),
    DB.getByTypeAndRange('service', from, today),
    DB.getByTypeAndRange('refroid', from, today),
    DB.getByTypeAndRange('huile', from, today),
  ]);

  const autos = alive([...temps, ...receptions, ...services, ...refroids, ...huiles])
    .filter(r => r.conforme === false)
    .map(r => ({
      date: r.date, time: r.time || r.timeEnd || '',
      objet: TYPE_LABELS[r.type] + ' — ' + (r.equipName || r.friteuse || r.produit || r.plat || ''),
      description: r.type === 'temp' ? 'Relevé ' + UI.fmtTemp(r.temp)
        : r.type === 'refroid' ? UI.fmtTemp(r.tempStart) + ' → ' + UI.fmtTemp(r.tempEnd) + ' en ' + r.durationMin + ' min'
        : r.type === 'huile' ? 'Composés polaires > 25 %' + (r.polairesPct != null ? ' (' + r.polairesPct + ' %)' : '')
        : 'Relevé ' + UI.fmtTemp(r.temp),
      action: r.actionCorrective || r.action, agent: r.agent, auto: true,
    }));

  const rows = [
    ...alive(manual).map(r => Object.assign({ auto: false }, r)),
    ...autos,
  ].sort((a, b) => (b.date + (b.time || '')).localeCompare(a.date + (a.time || '')));

  el.innerHTML = headerHTML('Non-conformités', '30 derniers jours — celles issues des relevés apparaissent automatiquement',
      '<button class="btn" id="new-nc">➕ Signaler</button>') +
    (rows.length ? '<div class="rec-list">' + rows.map(r =>
      '<div class="rec-item bad">' +
      '<div class="big">⚠️</div>' +
      '<div class="body"><div class="title">' + UI.esc(r.objet) + (r.auto ? ' <span class="pill info">auto</span>' : (r.statut === 'cloturee' ? ' <span class="pill ok">clôturée</span>' : ' <span class="pill warn">ouverte</span>')) + '</div>' +
      '<div class="meta">' + UI.frDate(r.date) + ' ' + UI.esc(r.time || '') + ' — ' + UI.esc(r.description || '') +
      (r.action ? ' — Action : ' + UI.esc(r.action) : '') + ' — ' + UI.esc(r.agent || '') + '</div></div>' +
      (!r.auto && r.statut !== 'cloturee' ? '<button class="btn small secondary" data-close-nc="' + r.id + '">Clôturer</button>' : '') +
      '</div>'
    ).join('') + '</div>' : '<div class="empty"><span class="e-ico">✅</span>Aucune non-conformité sur les 30 derniers jours. Continue comme ça !</div>');

  el.querySelector('#new-nc').addEventListener('click', openNCModal);
  el.querySelectorAll('[data-close-nc]').forEach(b => b.addEventListener('click', async () => {
    const rec = await DB.getRecord(Number(b.dataset.closeNc));
    if (rec) { rec.statut = 'cloturee'; await DB.updateRecord(rec); UI.toast('Non-conformité clôturée ✔', 'ok'); render(); }
  }));
};

function openNCModal() {
  UI.modal(
    '<h2>⚠️ Signaler une non-conformité</h2>' +
    '<label class="field"><span class="lbl">Objet / élément concerné</span>' +
    '<input type="text" data-f="objet" placeholder="Ex. : panne frigo BOF, produit périmé en réserve…"></label>' +
    '<label class="field"><span class="lbl">Lieu de l’incident</span>' +
    '<input type="text" data-f="lieu" placeholder="Ex. : économat, zone cuisson…"></label>' +
    '<div class="row"><div class="grow"><label class="field"><span class="lbl">N° de lot (optionnel)</span><input type="text" data-f="lot"></label></div>' +
    '<div class="grow"><label class="field"><span class="lbl">Date de péremption (optionnel)</span><input type="date" data-f="peremption"></label></div></div>' +
    '<label class="field"><span class="lbl">Description de l’incident</span>' +
    '<textarea data-f="description" placeholder="Décris le problème constaté"></textarea></label>' +
    '<label class="field"><span class="lbl">Action corrective mise en place</span>' +
    '<textarea data-f="action" placeholder="Ex. : denrées isolées et étiquetées « NE PAS UTILISER », dépanneur appelé…"></textarea></label>' +
    agentField() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const objet = m.querySelector('[data-f="objet"]').value.trim();
        if (!objet) { UI.toast('Indique l’objet', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        await DB.addRecord({
          type: 'nonconf', date: UI.todayISO(), time: UI.nowHM(),
          objet,
          lieu: m.querySelector('[data-f="lieu"]').value.trim(),
          lot: m.querySelector('[data-f="lot"]').value.trim(),
          peremption: m.querySelector('[data-f="peremption"]').value,
          description: m.querySelector('[data-f="description"]').value.trim(),
          action: m.querySelector('[data-f="action"]').value.trim(),
          statut: 'ouverte', agent,
        });
        close();
        UI.toast('Non-conformité signalée', 'ok');
        render();
      };
    }
  );
}

/* ================================================================
   HISTORIQUE & EXPORT
================================================================ */
/** Écarte les enregistrements annulés (ils restent visibles dans l'Historique,
 *  barrés, mais ne comptent plus nulle part ailleurs). */
function alive(recs) { return recs.filter(r => !r.annule); }

/** Colonnes d'export d'un registre + colonne Annulé (traçabilité des corrections). */
function exportCols(type) {
  return [...EXPORT_COLUMNS[type],
    ['Annulé', r => r.annule ? 'ANNULÉ le ' + UI.frDate((r.annuleQuand || '').slice(0, 10)) + ' par ' + (r.annulePar || '') + ' — motif : ' + (r.annuleMotif || '') : '']];
}

/** Annulation tracée d'un enregistrement erroné : jamais de suppression, une
 *  ligne barrée avec motif, auteur et horodatage (valeur probante du registre). */
function openAnnulModal(rec, onDone) {
  UI.modal(
    '<h2>🚫 Annuler cet enregistrement</h2>' +
    '<p class="muted" style="margin-bottom:12px">' + UI.esc(TYPE_LABELS[rec.type] || rec.type) + ' du ' + UI.frDate(rec.date) + ' ' + UI.esc(rec.time || '') +
    ' — l’enregistrement restera visible barré dans le registre (comme une rature sur le papier). Ressaisis ensuite la bonne valeur dans le module concerné.</p>' +
    '<label class="field"><span class="lbl">Motif de l’annulation (obligatoire)</span>' +
    '<input type="text" data-f="motif" placeholder="Ex. : erreur de saisie — 63 au lieu de 6,3"></label>' +
    agentField() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Retour</button><button class="btn danger" data-x="ok">Annuler l’enregistrement</button></div>',
    (m, close) => {
      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="ok"]').onclick = async () => {
        const motif = m.querySelector('[data-f="motif"]').value.trim();
        if (!motif) { UI.toast('Le motif est obligatoire', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        rec.annule = true;
        rec.annuleMotif = motif;
        rec.annulePar = agent;
        rec.annuleQuand = new Date().toISOString();
        await DB.updateRecord(rec);
        close();
        UI.toast('Enregistrement annulé (tracé) ✔', 'ok');
        if (onDone) onDone(); else render();
      };
    }
  );
}

const EXPORT_COLUMNS = {
  temp: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Équipement', r => r.equipName], ['Température (°C)', r => r.statut === 'hs' ? 'À l’arrêt (' + (r.motif || '') + ')' : r.temp], ['Conforme', r => r.statut === 'hs' ? '—' : (r.conforme === false ? 'NON' : 'OUI')], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  reception: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Fournisseur', r => r.fournisseur], ['Produit', r => r.produit], ['Lot / BL', r => r.lot], ['Famille', r => r.famille], ['Température (°C)', r => r.temp], ['État', r => r.etat === 'bad' ? 'Défaut' : 'Correct'], ['Conforme', r => r.conforme === false ? 'NON' : (r.tolere ? 'Contrôle à cœur' : 'OUI')], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  refroid: [['Date', r => UI.frDate(r.date)], ['Type', r => r.mode === 'remise' ? 'Remise en T°' : 'Refroidissement'], ['Préparation', r => r.produit], ['T° départ', r => r.tempStart], ['Heure départ', r => r.timeStart], ['T° fin', r => r.tempEnd], ['Heure fin', r => r.timeEnd], ['Durée (min)', r => r.durationMin], ['Conforme', r => r.status === 'encours' ? 'En cours' : (r.conforme === false ? 'NON' : 'OUI')], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  service: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Service', r => r.service || ''], ['Plat', r => r.plat], ['Liaison', r => r.liaison], ['Température (°C)', r => r.temp], ['Plat témoin', r => r.platTemoin ? 'OUI' : 'NON'], ['Conforme', r => r.conforme === false ? 'NON' : (r.tolere ? 'Toléré <2h' : 'OUI')], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  decongel: [['Date mise en décongélation', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Produit', r => r.produit], ['Fournisseur', r => r.fournisseur], ['Lot', r => r.lot], ['À utiliser avant', r => UI.frDate(r.limite)], ['Sorti le', r => r.sortieDate ? UI.frDate(r.sortieDate) + ' ' + (r.sortieTime || '') : ''], ['Devenir', r => r.issue === 'jete' ? 'JETÉ' : (r.issue === 'utilise' ? 'Utilisé' : '')], ['Statut', r => r.statut === 'termine' ? 'Terminé' : 'En cours'], ['Agent', r => r.agent]],
  entame: [['Date ouverture', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Produit', r => r.produit], ['Type', r => r.categorie], ['DLC interne', r => UI.frDate(r.dlc)], ['Clôturé le', r => r.finDate ? UI.frDate(r.finDate) : ''], ['Devenir', r => r.issue === 'jete' ? 'JETÉ' : (r.issue === 'utilise' ? 'Consommé' : '')], ['Statut', r => r.statut === 'termine' ? 'Terminé' : 'En cours'], ['Agent', r => r.agent]],
  etiquette: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Produit', r => r.produit], ['Lot', r => r.lot], ['DLC', r => r.dlc ? UI.frDate(r.dlc) : ''], ['Photo', r => r.photo ? 'OUI' : 'NON'], ['Agent', r => r.agent]],
  nettoyage: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Tâche', r => r.taskName], ['Zone', r => r.zone], ['Fréquence', r => r.freq], ['Agent', r => r.agent]],
  huile: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Friteuse', r => r.friteuse], ['Opération', r => r.action], ['État huile', r => r.etat], ['Polarité', r => r.polaires === 'nok' ? '> 25 % NON CONFORME' : (r.polaires === 'ok' ? '≤ 25 %' : '') + (r.polairesPct != null ? ' (' + r.polairesPct + ' %)' : '')], ['Huile usagée', r => r.volume != null || r.destination ? (r.volume != null ? r.volume + ' L' : '') + (r.destination ? ' → ' + r.destination : '') + (r.bon ? ' (bon ' + r.bon + ')' : '') : ''], ['Température (°C)', r => r.temp], ['Action corrective', r => r.actionCorrective], ['Remarque', r => r.remarque], ['Agent', r => r.agent]],
  nonconf: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Objet', r => r.objet], ['Lieu', r => r.lieu], ['Lot', r => r.lot], ['Péremption', r => r.peremption ? UI.frDate(r.peremption) : ''], ['Description', r => r.description], ['Action corrective', r => r.action], ['Statut', r => r.statut], ['Agent', r => r.agent]],
  verif: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Instrument', r => r.instrument], ['Méthode', r => r.methode], ['Écart constaté (°C)', r => r.ecart], ['Conforme (|écart| ≤ 1 °C)', r => r.conforme === false ? 'NON' : 'OUI'], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  fermeture: [['Date', r => UI.frDate(r.date)], ['Motif', r => r.motif], ['Agent', r => r.agent]],
};

/* ---------- Export PDF des registres (jsPDF, chargé à la demande) ---------- */
let _pdfPromise = null;
function ensureJsPDF() {
  if (window.jspdf && window.jspdf.jsPDF && window.jspdf.jsPDF.API.autoTable) return Promise.resolve(window.jspdf.jsPDF);
  if (_pdfPromise) return _pdfPromise;
  const loadScript = src => new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.onload = resolve;
    s.onerror = () => { s.remove(); reject(new Error('Impossible de charger le générateur PDF — réessaie')); };
    document.head.appendChild(s);
  });
  _pdfPromise = loadScript('js/vendor/jspdf.umd.min.js')
    .then(() => loadScript('js/vendor/jspdf.plugin.autotable.min.js'))
    .then(() => window.jspdf.jsPDF)
    .catch(e => { _pdfPromise = null; throw e; });
  return _pdfPromise;
}

/** Les polices standard des PDF ne couvrent que l'alphabet latin (WinAnsi) :
 *  un emoji ou symbole exotique rendrait la ligne entière illisible — on le
 *  remplace par « ? » en conservant tous les caractères français. */
function pdfSafe(s) {
  return String(s == null ? '' : s).replace(/\u2212/g, '-').replace(/[^\x20-\x7E -ÿŒœ€–—‘’“”…•]/g, '?');
}

/** Génère le document PDF (A4 paysage) pour un ou plusieurs registres :
 *  en-tête officiel (établissement, registre, période, visa) + tableau,
 *  lignes non conformes en rouge. Retourne { blob, total } (total = 0 si vide). */
async function buildRegistresPDF(types, from, to) {
  const jsPDF = await ensureJsPDF();
  const doc = new jsPDF({ orientation: 'landscape', unit: 'mm', format: 'a4' });
  let first = true;
  let total = 0;

  for (const type of types) {
    const recs = (await DB.getByTypeAndRange(type, from, to)).sort((a, b) => (a.date + (a.time || '')).localeCompare(b.date + (b.time || '')));
    if (!recs.length) continue;
    if (!first) doc.addPage();
    first = false;
    total += recs.length;

    doc.setFontSize(14);
    doc.setFont(undefined, 'bold');
    doc.text(pdfSafe(SETTINGS.etablissement), 14, 13);
    doc.setFont(undefined, 'normal');
    doc.setFontSize(11);
    doc.text(pdfSafe('Registre : ' + TYPE_LABELS[type] + ' — période du ' + UI.frDate(from) + ' au ' + UI.frDate(to)), 14, 20);
    doc.setFontSize(9);
    doc.text(pdfSafe('Édité le ' + UI.frDate(UI.todayISO()) + ' à ' + UI.nowHM() + ' — ' + recs.length + ' enregistrement(s) — Visa du responsable : ____________________'), 14, 26);

    // Registre des enceintes : preuve de la régularité (jours sans relevé vs fermetures déclarées)
    let startY = 30;
    if (type === 'temp') {
      const fermetures = await DB.getByTypeAndRange('fermeture', from, to);
      const fermesSet = new Set(fermetures.map(f => f.date));
      const joursAvecReleve = new Set(recs.map(r => r.date));
      const today0 = UI.todayISO();
      let manques = 0, jours = 0;
      for (let d = from; d <= to && d <= today0; d = UI.addDays(d, 1)) {
        jours++;
        if (!joursAvecReleve.has(d) && !fermesSet.has(d)) manques++;
      }
      doc.text(pdfSafe('Régularité : ' + jours + ' jour(s) sur la période, ' + manques + ' sans relevé non justifié, ' + fermesSet.size + ' déclaré(s) fermé(s).'), 14, 30);
      startY = 34;
    }

    const cols = exportCols(type);
    const ncRows = new Set();
    const body = recs.map((r, i) => {
      if (r.conforme === false) ncRows.add(i);
      return cols.map(c => pdfSafe(c[1](r)));
    });
    doc.autoTable({
      startY,
      head: [cols.map(c => pdfSafe(c[0]))],
      body,
      styles: { fontSize: 8, cellPadding: 1.5 },
      headStyles: { fillColor: [26, 127, 90] },
      didParseCell: d => {
        if (d.section === 'body' && ncRows.has(d.row.index)) d.cell.styles.fillColor = [253, 220, 218];
      },
    });
  }

  return { blob: first ? null : doc.output('blob'), total };
}

/** Export PDF à la demande (boutons de l'Historique). */
async function exportPDF(types, from, to, filename) {
  let res;
  try { res = await buildRegistresPDF(types, from, to); }
  catch (e) { UI.toast(e.message, 'bad'); return; }
  if (!res.blob) { UI.toast('Rien à exporter sur cette période', 'bad'); return; }
  await UI.saveFile(filename, 'application/pdf', res.blob);
  UI.toast('PDF généré (' + res.total + ' enregistrements) ✔', 'ok');
}

/** Une fois par semaine, dépose aussi le PDF lisible des registres des 30
 *  derniers jours sur Google Drive (dossier « Registres PDF ») — en plus de la
 *  sauvegarde JSON technique qui, elle, sert à restaurer l'application. */
let _pdfDriveEnCours = false;
async function maybeWeeklyPdfToDrive() {
  const url = (SETTINGS.driveUrl || '').trim();
  if (!url || !navigator.onLine || _pdfDriveEnCours) return;
  const last = localStorage.getItem('haccp-pdf-drive-last') || '';
  const today = UI.todayISO();
  if (last && last > UI.addDays(today, -7)) return;
  _pdfDriveEnCours = true;
  try {
    await sendWeeklyPdf_(url, today);
  } finally {
    _pdfDriveEnCours = false;
  }
}

async function sendWeeklyPdf_(url, today) {
  const res = await buildRegistresPDF(Object.keys(EXPORT_COLUMNS), UI.addDays(today, -30), today);
  if (!res.blob) { localStorage.setItem('haccp-pdf-drive-last', today); return; }
  const b64 = await new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(',')[1]);
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(res.blob);
  });
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain;charset=utf-8' },
    body: JSON.stringify({ app: 'haccp-cuisine', type: 'pdf', filename: 'registres-haccp-30j-' + today + '.pdf', data: b64 }),
  });
  if (resp.ok) {
    localStorage.setItem('haccp-pdf-drive-last', today);
    UI.toast('Registres PDF déposés sur Drive ✔', 'ok');
  }
}

VIEWS.historique = async function (el) {
  const today = UI.todayISO();
  const state = VIEWS.historique._state || (VIEWS.historique._state = { type: 'temp', from: UI.addDays(today, -6), to: today });
  state.calMonth = state.calMonth || today.slice(0, 7); // AAAA-MM affiché dans le calendrier

  // --- Calendrier de complétude : chaque jour doit porter les relevés d'enceintes ---
  const moisDebut = state.calMonth + '-01';
  const joursDansMois = new Date(Number(state.calMonth.slice(0, 4)), Number(state.calMonth.slice(5, 7)), 0).getDate();
  const moisFin = state.calMonth + '-' + String(joursDansMois).padStart(2, '0');
  const [calTemps, calFermetures] = await Promise.all([
    DB.getByTypeAndRange('temp', moisDebut, moisFin),
    DB.getByTypeAndRange('fermeture', moisDebut, moisFin),
  ]);
  const relevesParJour = {};
  alive(calTemps).forEach(r => { (relevesParJour[r.date] = relevesParJour[r.date] || new Set()).add(r.equipId); });
  const fermes = {};
  calFermetures.forEach(f => { fermes[f.date] = f; });
  const nbEquips = SETTINGS.equipements.length || 1;

  const moisNom = new Date(moisDebut + 'T12:00:00').toLocaleDateString('fr-FR', { month: 'long', year: 'numeric' });
  const premierJourSemaine = (new Date(moisDebut + 'T12:00:00').getDay() + 6) % 7; // 0 = lundi
  let calCells = '';
  for (let i = 0; i < premierJourSemaine; i++) calCells += '<div class="cal-day empty"></div>';
  for (let d = 1; d <= joursDansMois; d++) {
    const iso = state.calMonth + '-' + String(d).padStart(2, '0');
    let cls = 'future', label = '';
    if (iso <= today) {
      if (fermes[iso]) { cls = 'closed'; label = '🚪'; }
      else {
        const n = relevesParJour[iso] ? relevesParJour[iso].size : 0;
        cls = n >= nbEquips ? 'full' : (n > 0 ? 'part' : 'miss');
      }
    }
    calCells += '<div class="cal-day ' + cls + '" data-cal-day="' + iso + '">' + d + (label ? '<span>' + label + '</span>' : '') + '</div>';
  }
  const joursVides = Object.keys(fermes).length;
  const joursManques = (() => {
    let n = 0;
    for (let d = 1; d <= joursDansMois; d++) {
      const iso = state.calMonth + '-' + String(d).padStart(2, '0');
      if (iso > today) break;
      if (!fermes[iso] && !(relevesParJour[iso] && relevesParJour[iso].size)) n++;
    }
    return n;
  })();

  el.innerHTML = headerHTML('Historique & export', 'Consultation des enregistrements et export CSV/PDF pour les contrôles sanitaires') +

    '<div class="card no-print"><div class="row" style="margin-bottom:10px">' +
    '<h2 style="margin:0">📅 Complétude des relevés — ' + UI.esc(moisNom) + '</h2><div class="grow"></div>' +
    '<button class="btn small ghost" id="cal-prev">‹</button><button class="btn small ghost" id="cal-next">›</button></div>' +
    '<div class="cal-grid">' + ['L', 'M', 'M', 'J', 'V', 'S', 'D'].map(j => '<div class="cal-head">' + j + '</div>').join('') + calCells + '</div>' +
    '<div class="row" style="margin-top:10px;font-size:12.5px" class="muted">' +
    '<span class="pill ok">Complet</span><span class="pill warn">Partiel</span><span class="pill bad">Aucun relevé</span><span class="pill">🚪 Fermé</span>' +
    (joursManques ? '<span class="muted">— ' + joursManques + ' jour(s) sans aucun relevé ce mois-ci : touche le jour pour le justifier (fermeture)</span>' : '<span class="muted">— touche un jour pour le détail</span>') +
    '</div></div>' +

    '<div class="card no-print"><div class="row">' +
    '<div class="grow"><label class="field" style="margin:0"><span class="lbl">Registre</span><select id="h-type">' +
    Object.keys(TYPE_LABELS).map(t => '<option value="' + t + '"' + (t === state.type ? ' selected' : '') + '>' + TYPE_LABELS[t] + '</option>').join('') +
    '</select></label></div>' +
    '<div><label class="field" style="margin:0"><span class="lbl">Du</span><input type="date" id="h-from" value="' + state.from + '"></label></div>' +
    '<div><label class="field" style="margin:0"><span class="lbl">Au</span><input type="date" id="h-to" value="' + state.to + '"></label></div>' +
    '</div><div class="spacer"></div><div class="row">' +
    '<span class="lbl" style="margin:0">Période :</span>' +
    '<button class="btn small ghost" data-range="7">7 jours</button>' +
    '<button class="btn small ghost" data-range="30">30 jours</button>' +
    '<button class="btn small ghost" data-range="mois">Mois dernier</button>' +
    '<button class="btn small ghost" data-range="90">Trimestre</button>' +
    '</div><div class="spacer"></div><div class="row">' +
    '<button class="btn small" id="h-export">⬇️ CSV de ce registre</button>' +
    '<button class="btn small" id="h-pdf">🧾 PDF de ce registre</button>' +
    '<button class="btn small" id="h-export-all">📁 CSV inspection (tous)</button>' +
    '<button class="btn small" id="h-pdf-all">🧾 PDF inspection (tous)</button>' +
    '<button class="btn small secondary" id="h-print">🖨️ Imprimer</button>' +
    '</div>' +
    '<hr class="sep"><div class="row">' +
    '<div class="grow"><input type="text" id="h-search" placeholder="🔎 Rappel de lot : produit, n° de lot, fournisseur, plat…"></div>' +
    '<button class="btn small" id="h-search-btn">Rechercher partout</button></div>' +
    '<div id="h-search-results" style="margin-top:10px"></div>' +
    '</div>' +
    '<div class="card"><div class="table-wrap" id="h-table"></div></div>';

  async function refreshTable() {
    // Jeton de séquence : si une requête plus récente est partie entre-temps
    // (changement de registre pendant un chargement lent), celle-ci s'abandonne.
    state._seq = (state._seq || 0) + 1;
    const seq = state._seq;
    const recs = (await DB.getByTypeAndRange(state.type, state.from, state.to)).sort((a, b) => (b.date + (b.time || '')).localeCompare(a.date + (a.time || '')));
    if (seq !== state._seq) return;
    const cols = exportCols(state.type);
    const box = document.getElementById('h-table');
    if (!box) return; // l'utilisateur a quitté la vue pendant la requête
    const annulable = !['menu', 'fermeture'].includes(state.type);
    // En-tête officiel, visible uniquement à l'impression (classeur PMS)
    const printHeader = '<div class="print-header"><h2>' + UI.esc(SETTINGS.etablissement) + '</h2>' +
      'Registre : <b>' + UI.esc(TYPE_LABELS[state.type]) + '</b> — Période : du ' + UI.frDate(state.from) + ' au ' + UI.frDate(state.to) +
      ' — Édité le ' + UI.frDate(UI.todayISO()) + ' à ' + UI.nowHM() +
      '<div class="visa">Visa du responsable : ______________________</div></div>';
    box.innerHTML = printHeader + (recs.length
      ? '<table><thead><tr>' + cols.map(c => '<th>' + UI.esc(c[0]) + '</th>').join('') +
        (annulable ? '<th class="no-print"></th>' : '') + '</tr></thead><tbody>' +
        recs.map(r => '<tr' + (r.annule ? ' class="annule"' : (r.conforme === false ? ' style="background:var(--red-light)"' : '')) + '>' +
          cols.map(c => '<td>' + UI.esc(c[1](r) == null ? '' : c[1](r)) + '</td>').join('') +
          (annulable ? '<td class="no-print">' + (r.annule ? '' : '<button class="btn small ghost" data-annul="' + r.id + '" title="Annuler cet enregistrement (tracé)">🚫</button>') + '</td>' : '') +
          '</tr>').join('') +
        '</tbody></table>'
      : '<div class="empty">Aucun enregistrement sur cette période.</div>');
    box.querySelectorAll('[data-annul]').forEach(b => b.addEventListener('click', async () => {
      const rec = await DB.getRecord(Number(b.dataset.annul));
      if (rec) openAnnulModal(rec, refreshTable);
    }));
  }

  // Calendrier : navigation et détail/justification d'un jour
  const shiftMonth = delta => {
    const d = new Date(state.calMonth + '-15T12:00:00');
    d.setMonth(d.getMonth() + delta);
    state.calMonth = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
    render();
  };
  el.querySelector('#cal-prev').addEventListener('click', () => shiftMonth(-1));
  el.querySelector('#cal-next').addEventListener('click', () => shiftMonth(1));
  el.querySelectorAll('[data-cal-day]').forEach(c => c.addEventListener('click', async () => {
    const iso = c.dataset.calDay;
    if (iso > today) return;
    const ferme = fermes[iso];
    const n = relevesParJour[iso] ? relevesParJour[iso].size : 0;
    const [nett, recep] = await Promise.all([
      DB.getByTypeAndRange('nettoyage', iso, iso),
      DB.getByTypeAndRange('reception', iso, iso),
    ]);
    UI.modal(
      '<h2>📅 ' + UI.frDate(iso) + '</h2>' +
      (ferme
        ? '<p class="pill" style="margin-bottom:12px">🚪 Jour déclaré fermé : ' + UI.esc(ferme.motif || '') + ' (' + UI.esc(ferme.agent || '') + ')</p>'
        : '<div class="rec-list" style="margin-bottom:12px">' +
          '<div class="rec-item ' + (n >= nbEquips ? 'ok' : 'bad') + '"><div class="body"><div class="title">Relevés d’enceintes : ' + n + '/' + nbEquips + '</div></div></div>' +
          '<div class="rec-item"><div class="body"><div class="title">Nettoyage : ' + nett.length + ' tâche(s) · Réceptions : ' + recep.length + '</div></div></div>' +
          '</div>') +
      (!ferme && n === 0
        ? '<label class="field"><span class="lbl">Justifier ce jour vide : motif de fermeture</span>' +
          '<input type="text" data-f="motif" placeholder="Ex. : cuisine fermée, week-end sous-traité, férié…"></label>' + agentField()
        : '') +
      '<div class="actions"><button class="btn ghost" data-x="close">Fermer</button>' +
      (ferme ? '<button class="btn danger" data-x="unclose">Annuler la fermeture</button>' : '') +
      (!ferme && n === 0 ? '<button class="btn" data-x="declare">🚪 Déclarer fermé</button>' : '') +
      '</div>',
      (m, close) => {
        m.querySelector('[data-x="close"]').onclick = close;
        const btnD = m.querySelector('[data-x="declare"]');
        if (btnD) btnD.onclick = async () => {
          const agent = requireAgent(m); if (!agent) return;
          await DB.addRecord({
            type: 'fermeture', date: iso, time: UI.nowHM(),
            motif: m.querySelector('[data-f="motif"]').value.trim() || 'fermeture',
            agent,
          });
          close(); UI.toast('Jour déclaré fermé ✔', 'ok'); render();
        };
        const btnU = m.querySelector('[data-x="unclose"]');
        if (btnU) btnU.onclick = () => UI.confirm('Annuler la déclaration de fermeture du ' + UI.frDate(iso) + ' ?', async () => {
          await DB.deleteRecord(ferme.id); close(); render();
        });
      }
    );
  }));

  el.querySelector('#h-type').addEventListener('change', e => { state.type = e.target.value; refreshTable(); });
  el.querySelector('#h-from').addEventListener('change', e => { state.from = e.target.value; refreshTable(); });
  el.querySelector('#h-to').addEventListener('change', e => { state.to = e.target.value; refreshTable(); });
  el.querySelector('#h-print').addEventListener('click', () => window.print());
  el.querySelector('#h-pdf').addEventListener('click', () =>
    exportPDF([state.type], state.from, state.to, 'haccp-' + state.type + '-' + state.from + '-' + state.to + '.pdf'));
  el.querySelector('#h-pdf-all').addEventListener('click', () =>
    exportPDF(Object.keys(EXPORT_COLUMNS), state.from, state.to, 'haccp-inspection-' + state.from + '-' + state.to + '.pdf'));

  // Recherche multi-registres (traçabilité ascendante : « où est passé le lot X ? »).
  // Index léger construit par curseur (sans matérialiser les photos), une fois par visite.
  state._searchIndex = null;
  const getSearchIndex = async () => {
    if (state._searchIndex) return state._searchIndex;
    const idx = [];
    await DB.eachRecord(r => idx.push({
      id: r.id, type: r.type, date: r.date, time: r.time, conforme: r.conforme,
      produit: r.produit, plat: r.plat, lot: r.lot, fournisseur: r.fournisseur,
      objet: r.objet, equipName: r.equipName, taskName: r.taskName,
      description: r.description, categorie: r.categorie, agent: r.agent,
      hasPhoto: !!r.photo,
    }));
    state._searchIndex = idx;
    return idx;
  };

  const doSearch = async () => {
    const q = el.querySelector('#h-search').value.trim().toLowerCase();
    const box = el.querySelector('#h-search-results');
    if (q.length < 2) { box.innerHTML = '<p class="muted">Saisis au moins 2 caractères.</p>'; return; }
    box.innerHTML = '<p class="muted">Recherche…</p>';
    const all = await getSearchIndex();
    const FIELDS = ['produit', 'plat', 'lot', 'fournisseur', 'objet', 'equipName', 'taskName', 'description', 'categorie'];
    const hits = all.filter(r => FIELDS.some(f => r[f] && String(r[f]).toLowerCase().includes(q)))
      .sort((a, b) => (b.date + (b.time || '')).localeCompare(a.date + (a.time || '')));
    const shown = hits.slice(0, 80);
    if (!box.isConnected) return;
    box.innerHTML = hits.length
      ? '<p class="pill info" style="margin-bottom:10px">' + hits.length + ' résultat(s)' + (hits.length > 80 ? ' — 80 premiers affichés' : '') + '</p>' +
        '<div class="rec-list">' + shown.map(r => {
          const titre = r.produit || r.plat || r.objet || r.equipName || r.taskName || '—';
          return '<div class="rec-item ' + (r.conforme === false ? 'bad' : '') + '"' + (r.type === 'etiquette' ? ' data-open-eti="' + r.id + '" style="cursor:pointer"' : '') + '>' +
            '<div class="big" style="font-size:13px">' + UI.esc(TYPE_LABELS[r.type] || r.type).split(' ')[0] + '</div>' +
            '<div class="body"><div class="title">' + UI.esc(titre) + (r.type === 'etiquette' && r.hasPhoto ? ' 📷' : '') + '</div>' +
            '<div class="meta">' + UI.esc(TYPE_LABELS[r.type] || r.type) + ' — ' + UI.frDate(r.date) + ' ' + UI.esc(r.time || '') +
            (r.lot ? ' — lot <b>' + UI.esc(r.lot) + '</b>' : '') +
            (r.fournisseur ? ' — ' + UI.esc(r.fournisseur) : '') +
            (r.agent ? ' — ' + UI.esc(r.agent) : '') + '</div></div></div>';
        }).join('') + '</div>'
      : '<p class="muted">Aucun résultat pour « ' + UI.esc(q) + ' ».</p>';
    box.querySelectorAll('[data-open-eti]').forEach(c => c.addEventListener('click', () => openEtiquetteDetail(Number(c.dataset.openEti))));
  };
  el.querySelector('#h-search-btn').addEventListener('click', doSearch);
  el.querySelector('#h-search').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });

  // Raccourcis de période
  el.querySelectorAll('[data-range]').forEach(b => b.addEventListener('click', () => {
    const t = UI.todayISO();
    if (b.dataset.range === 'mois') {
      const debutMoisCourant = t.slice(0, 8) + '01';
      state.to = UI.addDays(debutMoisCourant, -1);            // dernier jour du mois précédent
      state.from = state.to.slice(0, 8) + '01';               // 1er jour du mois précédent
    } else {
      state.from = UI.addDays(t, -(Number(b.dataset.range) - 1));
      state.to = t;
    }
    el.querySelector('#h-from').value = state.from;
    el.querySelector('#h-to').value = state.to;
    refreshTable();
  }));

  // Export complet pour inspection : un seul CSV avec une section par registre
  el.querySelector('#h-export-all').addEventListener('click', async () => {
    const sections = [];
    let total = 0;
    for (const type of Object.keys(EXPORT_COLUMNS)) {
      const recs = (await DB.getByTypeAndRange(type, state.from, state.to)).sort((a, b) => (a.date + (a.time || '')).localeCompare(b.date + (b.time || '')));
      if (!recs.length) continue;
      total += recs.length;
      const cols = exportCols(type);
      sections.push([['=== ' + TYPE_LABELS[type] + ' (' + recs.length + ') ===']]);
      sections.push([cols.map(c => c[0]), ...recs.map(r => cols.map(c => c[1](r)))]);
      sections.push([['']]);
    }
    if (!total) { UI.toast('Rien à exporter sur cette période', 'bad'); return; }
    const rows = sections.flat();
    UI.downloadCSV(
      'haccp-inspection-' + state.from + '-' + state.to + '.csv',
      [SETTINGS.etablissement + ' — Registres HACCP du ' + UI.frDate(state.from) + ' au ' + UI.frDate(state.to)],
      rows
    );
    UI.toast('Export inspection généré (' + total + ' enregistrements) ✔', 'ok');
  });
  el.querySelector('#h-export').addEventListener('click', async () => {
    const recs = (await DB.getByTypeAndRange(state.type, state.from, state.to)).sort((a, b) => (a.date + (a.time || '')).localeCompare(b.date + (b.time || '')));
    if (!recs.length) { UI.toast('Rien à exporter sur cette période', 'bad'); return; }
    const cols = exportCols(state.type);
    UI.downloadCSV(
      'haccp-' + state.type + '-' + state.from + '-' + state.to + '.csv',
      cols.map(c => c[0]),
      recs.map(r => cols.map(c => c[1](r)))
    );
    UI.toast('Export CSV généré ✔', 'ok');
  });

  refreshTable();
};

/* ================================================================
   RÉGLAGES
================================================================ */
/** Modale de vérification d'un thermomètre (méthode 0 °C / 100 °C / étalon). */
function openVerifModal(instrument) {
  UI.modal(
    '<h2>🌡️ Vérification : ' + UI.esc(instrument) + '</h2>' +
    '<p class="muted" style="margin-bottom:12px">GBPH : vérification périodique. Conforme si l’écart constaté est ≤ 1 °C en valeur absolue.</p>' +
    '<label class="field"><span class="lbl">Méthode</span>' +
    UI.segHTML('methode', [
      { value: 'eau glacée (0 °C)', label: '🧊 Eau glacée (0 °C)' },
      { value: 'eau bouillante (100 °C)', label: '♨️ Eau bouillante (100 °C)' },
      { value: 'comparaison sonde étalon', label: '⚖️ Sonde étalon' },
    ], 'eau glacée (0 °C)') + '</label>' +
    '<label class="field"><span class="lbl">Écart constaté (°C)</span>' +
    UI.tempInputHTML('ecart', { placeholder: '0.0', hint: 'Valeur lue − valeur attendue (peut être négatif)' }) + '</label>' +
    agentField() +
    '<div data-verdict></div>' +
    actionFieldHTML() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      UI.segWire(m);
      const input = m.querySelector('[data-f="ecart"]');
      const verdict = m.querySelector('[data-verdict]');
      const actionField = m.querySelector('[data-action-field]');
      const check = () => {
        const v = parseFloat(input.value);
        if (isNaN(v)) { verdict.innerHTML = ''; actionField.style.display = 'none'; return null; }
        const ok = Math.abs(v) <= 1;
        verdict.innerHTML = '<p class="pill ' + (ok ? 'ok' : 'bad') + '" style="margin-bottom:12px">' +
          (ok ? '✔ Conforme (écart ≤ 1 °C)' : '✘ NON CONFORME — instrument à ajuster ou remplacer') + '</p>';
        actionField.style.display = ok ? 'none' : 'block';
        return ok;
      };
      input.addEventListener('input', check);
      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const v = parseFloat(input.value);
        if (isNaN(v)) { UI.toast('Saisis l’écart constaté', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        const ok = Math.abs(v) <= 1;
        const action = m.querySelector('[data-f="action"]').value.trim();
        if (!ok && !action) { UI.toast('Indique l’action corrective (remplacement, ajustage…)', 'bad'); return; }
        await DB.addRecord({
          type: 'verif', date: UI.todayISO(), time: UI.nowHM(),
          instrument, methode: UI.segValue(m, 'methode'),
          ecart: v, conforme: ok, action: ok ? '' : action, agent,
        });
        close();
        UI.toast('Vérification enregistrée ✔', 'ok');
        render();
      };
    }
  );
}

VIEWS.parametres = async function (el) {
  const FREQ_LABEL = { quotidien: 'Quotidien', hebdomadaire: 'Hebdomadaire', mensuel: 'Mensuel' };

  // dernières vérifications par instrument
  const verifs = await DB.getByType('verif');
  const lastVerif = {};
  verifs.forEach(v => { if (!lastVerif[v.instrument] || v.date > lastVerif[v.instrument].date) lastVerif[v.instrument] = v; });

  el.innerHTML = headerHTML('Réglages', 'Configuration de l’établissement') +

    '<div class="card"><h2>🏥 Établissement</h2>' +
    '<label class="field"><span class="lbl">Nom affiché</span><input type="text" id="s-etab" value="' + UI.esc(SETTINGS.etablissement) + '"></label>' +
    '<button class="btn small" id="s-etab-save">Enregistrer</button></div>' +

    '<div class="card"><h2>👥 Agents (' + SETTINGS.agents.length + ')</h2>' +
    '<div class="row" style="margin-bottom:12px">' + SETTINGS.agents.map((a, i) =>
      '<span class="pill info">' + UI.esc(a) + ' <button data-del-agent="' + i + '" style="border:none;background:none;cursor:pointer;font-size:15px">✕</button></span>').join('') + '</div>' +
    '<div class="row"><div class="grow"><input type="text" id="s-agent-new" placeholder="Prénom NOM"></div>' +
    '<button class="btn small" id="s-agent-add">Ajouter</button></div></div>' +

    '<div class="card"><h2>❄️ Enceintes froides (' + SETTINGS.equipements.length + ')</h2>' +
    '<div class="rec-list" style="margin-bottom:12px">' + SETTINGS.equipements.map((e, i) =>
      '<div class="rec-item"><div class="body"><div class="title">' + UI.esc(e.name) + '</div>' +
      '<div class="meta">' + (e.type === 'negatif' ? 'Froid négatif' : 'Froid positif') + ' · consigne ' + e.min + ' à ' + e.max + ' °C</div></div>' +
      '<button class="btn small ghost" data-del-equip="' + i + '">🗑️</button></div>').join('') + '</div>' +
    '<button class="btn small" id="s-equip-add">➕ Ajouter une enceinte</button></div>' +

    '<div class="card"><h2>🚚 Fournisseurs (' + (SETTINGS.fournisseurs || []).length + ')</h2>' +
    '<p class="muted" style="margin-bottom:12px">Liste du PMS — proposée automatiquement à la réception. Bons de livraison à conserver 1 mois en cuisine.</p>' +
    '<div class="rec-list" style="margin-bottom:12px">' + (SETTINGS.fournisseurs || []).map((f, i) =>
      '<div class="rec-item"><div class="body"><div class="title">' + UI.esc(f.name) + '</div>' +
      '<div class="meta">' + UI.esc(f.produits || '') + (f.jours ? ' · Livraison : ' + UI.esc(f.jours) : '') + '</div></div>' +
      '<button class="btn small ghost" data-del-fourn="' + i + '">🗑️</button></div>').join('') + '</div>' +
    '<div class="row"><div class="grow"><input type="text" id="s-fourn-name" placeholder="Nom du fournisseur"></div>' +
    '<div class="grow"><input type="text" id="s-fourn-prod" placeholder="Produits livrés"></div>' +
    '<div class="grow"><input type="text" id="s-fourn-jours" placeholder="Jours de livraison"></div>' +
    '<button class="btn small" id="s-fourn-add">Ajouter</button></div></div>' +

    '<div class="card"><h2>🌡️ Instruments de mesure (' + (SETTINGS.thermometres || []).length + ')</h2>' +
    '<p class="muted" style="margin-bottom:12px">Vérification périodique des thermomètres (eau glacée 0 °C / eau bouillante 100 °C) — à présenter en inspection. Conforme si écart ≤ 1 °C.</p>' +
    '<div class="rec-list" style="margin-bottom:12px">' + (SETTINGS.thermometres || []).map((t, i) => {
      const lv = lastVerif[t];
      const age = lv ? Math.round((new Date() - new Date(lv.date + 'T12:00:00')) / 86400000) : null;
      return '<div class="rec-item ' + (lv && lv.conforme === false ? 'bad' : '') + '"><div class="body"><div class="title">' + UI.esc(t) + '</div>' +
        '<div class="meta">' + (lv ? 'Dernière vérification : ' + UI.frDate(lv.date) + ' (' + (lv.conforme === false ? '⚠️ non conforme' : 'écart ' + lv.ecart + ' °C ✔') + ')' +
        (age > 365 ? ' — <b>plus d’un an !</b>' : '') : 'Jamais vérifié') + '</div></div>' +
        '<button class="btn small" data-verif-th="' + i + '">✅ Vérifier</button>' +
        '<button class="btn small ghost" data-del-th="' + i + '">🗑️</button></div>';
    }).join('') + '</div>' +
    '<div class="row"><div class="grow"><input type="text" id="s-th-new" placeholder="Nom de l’instrument"></div>' +
    '<button class="btn small" id="s-th-add">Ajouter</button></div></div>' +

    '<div class="card"><h2>🍟 Friteuses (' + SETTINGS.friteuses.length + ')</h2>' +
    '<div class="row" style="margin-bottom:12px">' + SETTINGS.friteuses.map((f, i) =>
      '<span class="pill info">' + UI.esc(f) + ' <button data-del-frit="' + i + '" style="border:none;background:none;cursor:pointer;font-size:15px">✕</button></span>').join('') + '</div>' +
    '<div class="row"><div class="grow"><input type="text" id="s-frit-new" placeholder="Nom de la friteuse"></div>' +
    '<button class="btn small" id="s-frit-add">Ajouter</button></div></div>' +

    '<div class="card"><h2>🧽 Plan de nettoyage (' + SETTINGS.cleaningTasks.length + ' tâches)</h2>' +
    '<div class="rec-list" style="margin-bottom:12px">' + SETTINGS.cleaningTasks.map((t, i) =>
      '<div class="rec-item"><div class="body"><div class="title">' + UI.esc(t.name) + '</div>' +
      '<div class="meta">' + UI.esc(t.zone) + ' · ' + FREQ_LABEL[t.freq] + '</div></div>' +
      '<button class="btn small ghost" data-del-task="' + i + '">🗑️</button></div>').join('') + '</div>' +
    '<button class="btn small" id="s-task-add">➕ Ajouter une tâche</button></div>' +

    '<div class="card"><h2>☁️ Sauvegardes cloud & serveur</h2>' +
    '<p class="muted" style="margin-bottom:12px">Chaque jour, l’application peut déposer une sauvegarde complète (registres + photos) vers une ou plusieurs destinations. Notices pas-à-pas : <b>GOOGLE-DRIVE.md</b> et <b>SAUVEGARDES-CLOUD.md</b> (Dropbox, Nextcloud, serveur).</p>' +

    '<label class="field" style="display:flex;align-items:center;gap:12px"><input type="checkbox" id="s-drive-auto" ' + (SETTINGS.driveAuto ? 'checked' : '') + ' style="width:26px;height:26px;min-height:0">' +
    '<span class="lbl" style="margin:0;text-transform:none;letter-spacing:0;font-size:15px">Sauvegarde automatique quotidienne (vers toutes les destinations actives)</span></label>' +

    '<hr class="sep"><div class="lbl" style="margin-bottom:8px">🟢 Google Drive <span class="muted">(photos déposées aussi en fichiers images)</span></div>' +
    '<label class="field"><span class="lbl">Adresse du script (…/exec)</span>' +
    '<input type="text" id="s-drive-url" value="' + UI.esc(SETTINGS.driveUrl || '') + '" placeholder="https://script.google.com/macros/s/.../exec"></label>' +

    '<hr class="sep"><div class="row" style="margin-bottom:8px"><input type="checkbox" id="s-dbx-on" ' + (SETTINGS.dropbox && SETTINGS.dropbox.on ? 'checked' : '') + ' style="width:26px;height:26px;min-height:0">' +
    '<div class="lbl" style="margin:0">🔵 Dropbox</div></div>' +
    '<label class="field"><span class="lbl">Jeton d’accès (voir notice)</span>' +
    '<input type="text" id="s-dbx-token" value="' + UI.esc((SETTINGS.dropbox && SETTINGS.dropbox.token) || '') + '" placeholder="sl.xxxxxxxx…"></label>' +

    '<hr class="sep"><div class="row" style="margin-bottom:8px"><input type="checkbox" id="s-wd-on" ' + (SETTINGS.webdav && SETTINGS.webdav.on ? 'checked' : '') + ' style="width:26px;height:26px;min-height:0">' +
    '<div class="lbl" style="margin:0">🟠 Nextcloud / ownCloud / WebDAV</div></div>' +
    '<label class="field"><span class="lbl">URL du dossier WebDAV</span>' +
    '<input type="text" id="s-wd-url" value="' + UI.esc((SETTINGS.webdav && SETTINGS.webdav.url) || '') + '" placeholder="https://cloud.exemple.fr/remote.php/dav/files/UTILISATEUR/HACCP/"></label>' +
    '<div class="row"><div class="grow"><label class="field"><span class="lbl">Utilisateur</span><input type="text" id="s-wd-user" value="' + UI.esc((SETTINGS.webdav && SETTINGS.webdav.user) || '') + '"></label></div>' +
    '<div class="grow"><label class="field"><span class="lbl">Mot de passe d’application</span><input type="password" id="s-wd-pass" value="' + UI.esc((SETTINGS.webdav && SETTINGS.webdav.pass) || '') + '"></label></div></div>' +

    '<hr class="sep"><div class="row" style="margin-bottom:8px"><input type="checkbox" id="s-http-on" ' + (SETTINGS.httpPost && SETTINGS.httpPost.on ? 'checked' : '') + ' style="width:26px;height:26px;min-height:0">' +
    '<div class="lbl" style="margin:0">⚫ Serveur maison (HTTP POST)</div></div>' +
    '<label class="field"><span class="lbl">Adresse du point de dépôt</span>' +
    '<input type="text" id="s-http-url" value="' + UI.esc((SETTINGS.httpPost && SETTINGS.httpPost.url) || '') + '" placeholder="https://mon-serveur.fr/haccp-backup.php"></label>' +

    '<div class="row" style="margin-top:6px"><button class="btn small" id="s-drive-save">Enregistrer</button>' +
    '<button class="btn small secondary" id="s-drive-now">☁️ Sauvegarder maintenant (test)</button></div>' +
    (lastAutoBackupDate() ? '<p class="muted" style="margin-top:10px">Dernière sauvegarde auto : ' + UI.frDate(lastAutoBackupDate()) + '</p>' : '') +
    '</div>' +

    '<div class="card"><h2>🔒 Code PIN des Réglages</h2>' +
    '<p class="muted" style="margin-bottom:12px">' + (SETTINGS.pin ? 'Les Réglages sont protégés par un PIN.' : 'Optionnel : protège cet écran contre les fausses manipulations sur la tablette partagée.') + '</p>' +
    '<div class="row"><div class="grow"><input type="password" inputmode="numeric" maxlength="4" id="s-pin" placeholder="' + (SETTINGS.pin ? 'Nouveau PIN (4 chiffres)' : 'PIN à 4 chiffres') + '" autocomplete="off"></div>' +
    '<button class="btn small" id="s-pin-set">' + (SETTINGS.pin ? 'Changer' : 'Activer') + '</button>' +
    (SETTINGS.pin ? '<button class="btn small ghost" id="s-pin-off">Désactiver</button>' : '') + '</div></div>' +

    '<div class="card"><h2>💾 Sauvegarde locale (fichier)</h2>' +
    '<p class="muted" style="margin-bottom:12px">Les données restent sur la tablette. Exporte aussi régulièrement une sauvegarde complète (JSON) et garde-la ailleurs (clé USB, ordinateur, mail).</p>' +
    '<div class="row"><button class="btn small" id="s-backup">⬇️ Exporter la sauvegarde</button>' +
    '<button class="btn small secondary" id="s-restore">⬆️ Restaurer une sauvegarde</button>' +
    '<input type="file" id="s-restore-file" accept="application/json" style="display:none"></div></div>';

  // Établissement
  el.querySelector('#s-etab-save').addEventListener('click', async () => {
    SETTINGS.etablissement = el.querySelector('#s-etab').value.trim() || DEFAULT_SETTINGS.etablissement;
    await saveSettings(); UI.toast('Enregistré ✔', 'ok');
  });

  // Sauvegardes cloud
  const readCloudForm = () => {
    SETTINGS.driveUrl = el.querySelector('#s-drive-url').value.trim();
    SETTINGS.driveAuto = el.querySelector('#s-drive-auto').checked;
    SETTINGS.dropbox = { on: el.querySelector('#s-dbx-on').checked, token: el.querySelector('#s-dbx-token').value.trim() };
    SETTINGS.webdav = {
      on: el.querySelector('#s-wd-on').checked,
      url: el.querySelector('#s-wd-url').value.trim(),
      user: el.querySelector('#s-wd-user').value.trim(),
      pass: el.querySelector('#s-wd-pass').value,
    };
    SETTINGS.httpPost = { on: el.querySelector('#s-http-on').checked, url: el.querySelector('#s-http-url').value.trim() };
  };
  el.querySelector('#s-drive-save').addEventListener('click', async () => {
    readCloudForm();
    await saveSettings();
    UI.toast('Réglages de sauvegarde enregistrés ✔', 'ok');
  });
  el.querySelector('#s-drive-now').addEventListener('click', async () => {
    readCloudForm();
    await saveSettings();
    if (!backupTargets().length) { UI.toast('Configure au moins une destination (Drive, Dropbox, WebDAV ou serveur)', 'bad'); return; }
    UI.toast('Envoi en cours…');
    const res = await sendBackupAll();
    if (res.results.length) {
      res.results.forEach(r => UI.toast((r.ok ? '✔ ' : '✘ ') + r.name + (r.ok ? ' : sauvegarde envoyée' : ' : ' + r.message), r.ok ? 'ok' : 'bad'));
    } else if (res.message) {
      UI.toast(res.message, 'bad');
    }
    if (res.ok) { setLastAutoBackupDate(UI.todayISO()); render(); }
  });

  // Agents
  el.querySelector('#s-agent-add').addEventListener('click', async () => {
    const v = el.querySelector('#s-agent-new').value.trim();
    if (!v) return;
    if (!SETTINGS.agents.includes(v)) SETTINGS.agents.push(v);
    await saveSettings(); render();
  });
  el.querySelectorAll('[data-del-agent]').forEach(b => b.addEventListener('click', async () => {
    SETTINGS.agents.splice(Number(b.dataset.delAgent), 1);
    await saveSettings(); render();
  }));

  // Enceintes
  el.querySelector('#s-equip-add').addEventListener('click', () => {
    UI.modal(
      '<h2>➕ Nouvelle enceinte froide</h2>' +
      '<label class="field"><span class="lbl">Nom</span><input type="text" data-f="name" placeholder="Ex. : Frigo pâtisserie"></label>' +
      '<label class="field"><span class="lbl">Type</span>' +
      UI.segHTML('type', [{ value: 'positif', label: '❄️ Froid positif (cible 3 °C)' }, { value: 'negatif', label: '🧊 Froid négatif (cible −18 °C)' }], 'positif') + '</label>' +
      '<div class="row"><div class="grow"><label class="field"><span class="lbl">Min (°C)</span><input type="number" step="0.5" data-f="min" value="0"></label></div>' +
      '<div class="grow"><label class="field"><span class="lbl">Max (°C)</span><input type="number" step="0.5" data-f="max" value="6"></label></div></div>' +
      '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Ajouter</button></div>',
      (m, close) => {
        UI.segWire(m);
        m.querySelector('.seg[data-seg="type"]').addEventListener('click', () => setTimeout(() => {
          const neg = UI.segValue(m, 'type') === 'negatif';
          m.querySelector('[data-f="min"]').value = neg ? -30 : 0;
          m.querySelector('[data-f="max"]').value = neg ? -15 : 6;
        }, 30));
        m.querySelector('[data-x="cancel"]').onclick = close;
        m.querySelector('[data-x="save"]').onclick = async () => {
          const name = m.querySelector('[data-f="name"]').value.trim();
          const min = parseFloat(m.querySelector('[data-f="min"]').value);
          const max = parseFloat(m.querySelector('[data-f="max"]').value);
          if (!name || isNaN(min) || isNaN(max) || min >= max) { UI.toast('Vérifie le nom et les consignes', 'bad'); return; }
          const neg = UI.segValue(m, 'type') === 'negatif';
          SETTINGS.equipements.push({ id: uid(), name, type: neg ? 'negatif' : 'positif', min, max, cible: neg ? -18 : 3 });
          await saveSettings(); close(); render();
        };
      }
    );
  });
  el.querySelectorAll('[data-del-equip]').forEach(b => b.addEventListener('click', () => {
    const i = Number(b.dataset.delEquip);
    UI.confirm('Supprimer « ' + SETTINGS.equipements[i].name + ' » ? L’historique de ses relevés est conservé.', async () => {
      SETTINGS.equipements.splice(i, 1);
      await saveSettings(); render();
    });
  }));

  // Fournisseurs
  el.querySelector('#s-fourn-add').addEventListener('click', async () => {
    const name = el.querySelector('#s-fourn-name').value.trim();
    if (!name) return;
    SETTINGS.fournisseurs = SETTINGS.fournisseurs || [];
    SETTINGS.fournisseurs.push({
      name,
      produits: el.querySelector('#s-fourn-prod').value.trim(),
      jours: el.querySelector('#s-fourn-jours').value.trim(),
    });
    await saveSettings(); render();
  });
  el.querySelectorAll('[data-del-fourn]').forEach(b => b.addEventListener('click', () => {
    const i = Number(b.dataset.delFourn);
    UI.confirm('Supprimer le fournisseur « ' + SETTINGS.fournisseurs[i].name + ' » ?', async () => {
      SETTINGS.fournisseurs.splice(i, 1);
      await saveSettings(); render();
    });
  }));

  // Instruments de mesure
  el.querySelector('#s-th-add').addEventListener('click', async () => {
    const v = el.querySelector('#s-th-new').value.trim();
    if (!v) return;
    SETTINGS.thermometres = SETTINGS.thermometres || [];
    if (!SETTINGS.thermometres.includes(v)) SETTINGS.thermometres.push(v);
    await saveSettings(); render();
  });
  el.querySelectorAll('[data-verif-th]').forEach(b => b.addEventListener('click', () =>
    openVerifModal(SETTINGS.thermometres[Number(b.dataset.verifTh)])));
  el.querySelectorAll('[data-del-th]').forEach(b => b.addEventListener('click', () => {
    const i = Number(b.dataset.delTh);
    UI.confirm('Supprimer « ' + SETTINGS.thermometres[i] + ' » ? L’historique de ses vérifications est conservé.', async () => {
      SETTINGS.thermometres.splice(i, 1);
      await saveSettings(); render();
    });
  }));

  // Friteuses
  el.querySelector('#s-frit-add').addEventListener('click', async () => {
    const v = el.querySelector('#s-frit-new').value.trim();
    if (!v) return;
    if (!SETTINGS.friteuses.includes(v)) SETTINGS.friteuses.push(v);
    await saveSettings(); render();
  });
  el.querySelectorAll('[data-del-frit]').forEach(b => b.addEventListener('click', async () => {
    SETTINGS.friteuses.splice(Number(b.dataset.delFrit), 1);
    await saveSettings(); render();
  }));

  // Tâches de nettoyage
  el.querySelector('#s-task-add').addEventListener('click', () => {
    UI.modal(
      '<h2>➕ Nouvelle tâche de nettoyage</h2>' +
      '<label class="field"><span class="lbl">Tâche</span><input type="text" data-f="name" placeholder="Ex. : nettoyage du four"></label>' +
      '<label class="field"><span class="lbl">Zone</span><input type="text" data-f="zone" placeholder="Ex. : Cuisine"></label>' +
      '<label class="field"><span class="lbl">Fréquence</span>' +
      UI.segHTML('freq', [
        { value: 'quotidien', label: 'Quotidien' },
        { value: 'hebdomadaire', label: 'Hebdo' },
        { value: 'mensuel', label: 'Mensuel' },
      ], 'quotidien') + '</label>' +
      '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Ajouter</button></div>',
      (m, close) => {
        UI.segWire(m);
        m.querySelector('[data-x="cancel"]').onclick = close;
        m.querySelector('[data-x="save"]').onclick = async () => {
          const name = m.querySelector('[data-f="name"]').value.trim();
          if (!name) { UI.toast('Indique la tâche', 'bad'); return; }
          SETTINGS.cleaningTasks.push({
            id: uid(), name,
            zone: m.querySelector('[data-f="zone"]').value.trim() || 'Cuisine',
            freq: UI.segValue(m, 'freq'),
          });
          await saveSettings(); close(); render();
        };
      }
    );
  });
  el.querySelectorAll('[data-del-task]').forEach(b => b.addEventListener('click', () => {
    const i = Number(b.dataset.delTask);
    UI.confirm('Supprimer la tâche « ' + SETTINGS.cleaningTasks[i].name + ' » ?', async () => {
      SETTINGS.cleaningTasks.splice(i, 1);
      await saveSettings(); render();
    });
  }));

  // Sauvegarde / restauration
  // Code PIN
  el.querySelector('#s-pin-set').addEventListener('click', async () => {
    const v = el.querySelector('#s-pin').value.trim();
    if (!/^\d{4}$/.test(v)) { UI.toast('Le PIN doit faire exactement 4 chiffres', 'bad'); return; }
    SETTINGS.pin = v;
    await saveSettings();
    _pinOkUntil = Date.now() + 5 * 60 * 1000;
    UI.toast('PIN activé ✔ (à retenir : il est aussi dans la sauvegarde JSON)', 'ok');
    render();
  });
  const pinOff = el.querySelector('#s-pin-off');
  if (pinOff) pinOff.addEventListener('click', () => UI.confirm('Désactiver le code PIN des Réglages ?', async () => {
    SETTINGS.pin = '';
    await saveSettings(); render();
  }));

  el.querySelector('#s-backup').addEventListener('click', async () => {
    const blob = new Blob([JSON.stringify(await buildBackup())], { type: 'application/json' });
    // UI.saveFile gère l'APK (écriture + partage natif) ET le navigateur —
    // un simple lien blob ne fonctionne pas dans une WebView Capacitor.
    const ok = await UI.saveFile('sauvegarde-haccp-' + UI.todayISO() + '.json', 'application/json', blob);
    if (ok) UI.toast('Sauvegarde exportée ✔', 'ok');
  });
  el.querySelector('#s-restore').addEventListener('click', () => el.querySelector('#s-restore-file').click());
  el.querySelector('#s-restore-file').addEventListener('change', async e => {
    const f = e.target.files[0];
    if (!f) return;
    try {
      const data = JSON.parse(await f.text());
      if (data.app !== 'haccp-cuisine' || !Array.isArray(data.records)) throw new Error('fichier invalide');
      UI.confirm('Restaurer ' + data.records.length + ' enregistrements ? Ils s’ajoutent aux données actuelles.', async () => {
        // Clone profond des défauts : une sauvegarde ancienne (sans plats,
        // thermometres…) ne doit pas faire pointer SETTINGS sur les tableaux
        // de DEFAULT_SETTINGS eux-mêmes (qui seraient ensuite mutés).
        SETTINGS = Object.assign({}, JSON.parse(JSON.stringify(DEFAULT_SETTINGS)), data.settings);
        await saveSettings();
        for (const r of data.records) {
          delete r.id;
          // Assainissement : une photo doit être une image en dataURL (sinon rejetée)
          if (r.photo && !/^data:image\//.test(String(r.photo))) delete r.photo;
          // Les menus sont uniques par (date, service) : fusionner au lieu de dupliquer
          if (r.type === 'menu' && r.date && r.service) {
            const existing = (await DB.getByTypeAndRange('menu', r.date, r.date)).find(m2 => m2.service === r.service);
            if (existing) {
              existing.items = [...new Set([...(existing.items || []), ...(r.items || [])])];
              await DB.updateRecord(existing);
              continue;
            }
          }
          await DB.addRecord(r);
        }
        // Invalider les états de vues qui cachent des données désormais périmées
        if (VIEWS.menu._state) VIEWS.menu._state.items = null;
        if (VIEWS.historique._state) VIEWS.historique._state._searchIndex = null;
        UI.toast('Sauvegarde restaurée ✔', 'ok');
        render();
      });
    } catch {
      UI.toast('Fichier de sauvegarde invalide', 'bad');
    }
  });
};

/* ---------- Chronomètre des refroidissements en cours ---------- */
const _refroidAlerted = new Set();

/** Toutes les 30 s : rafraîchit les compteurs affichés, alerte (bip + vibration
 *  + toast) au dépassement de la limite, badge rouge sur l'onglet. */
async function refroidTick() {
  let encours = [];
  try { encours = (await DB.getByType('refroid')).filter(r => r.status === 'encours'); }
  catch { return; }
  let depasse = false;
  encours.forEach(r => {
    const limit = r.mode === 'remise' ? RULES.remiseMinutes : RULES.refroidMinutes;
    const mins = Math.round((Date.now() - new Date(r.startISO).getTime()) / 60000);
    if (mins > limit) depasse = true;
    // compteur visible à l'écran, sans re-render complet
    const el = document.querySelector('[data-refroid-mins="' + r.id + '"]');
    if (el) {
      el.textContent = mins + ' min';
      const item = document.querySelector('[data-refroid-item="' + r.id + '"]');
      if (item && mins > limit) item.classList.add('bad');
    }
    if (mins > limit && !_refroidAlerted.has(r.id)) {
      _refroidAlerted.add(r.id);
      UI.beep();
      UI.toast('⏰ ' + (r.mode === 'remise' ? 'Remise en T°' : 'Refroidissement') + ' « ' + r.produit + ' » : délai de ' + limit + ' min DÉPASSÉ !', 'bad');
    }
  });
  const navBtn = document.querySelector('.nav-btn[data-view="refroidissement"]');
  if (navBtn) navBtn.classList.toggle('has-alert', depasse);
}

/* ---------- Démarrage ---------- */
(async function init() {
  await loadSettings();
  document.querySelectorAll('.nav-btn').forEach(b => b.addEventListener('click', () => navigate(b.dataset.view)));
  render();
  // Sauvegarde automatique quotidienne. La tablette reste souvent allumée en
  // continu : on retente au retour au premier plan et toutes les heures
  // (idempotent : une seule sauvegarde par jour grâce à haccp-drive-last).
  const tryBackup = () => maybeAutoBackup().catch(e => console.warn('backup auto', e));
  setTimeout(tryBackup, 2500);
  setInterval(tryBackup, 60 * 60 * 1000);
  // chronomètre des refroidissements (compteurs vivants + alerte de dépassement)
  setInterval(() => { refroidTick().catch(() => {}); }, 30 * 1000);
  // La tablette reste allumée en continu : au passage de minuit (ou au retour
  // au premier plan un autre jour), re-rendre pour afficher la nouvelle journée.
  let renderedDay = UI.todayISO();
  const checkNewDay = () => {
    if (UI.todayISO() !== renderedDay) { renderedDay = UI.todayISO(); render(); }
  };
  setInterval(checkNewDay, 60 * 1000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { checkNewDay(); tryBackup(); }
  });
})();
