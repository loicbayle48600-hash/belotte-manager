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

function navigate(view) {
  currentView = view;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  render();
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

/** Envoie la sauvegarde au script Google Drive. Retourne {ok, message}.
 *  Sous Capacitor (APK) la requête passe en natif ; en navigateur, un POST
 *  text/plain vers Apps Script est une « simple request » CORS dont la réponse
 *  est lisible — on vérifie donc réellement la réussite (pas de no-cors qui
 *  afficherait un faux succès). */
async function sendToDrive() {
  const url = (SETTINGS.driveUrl || '').trim();
  if (!url) return { ok: false, message: 'Aucune URL Google Drive configurée' };
  if (!navigator.onLine) return { ok: false, message: 'Pas de connexion Internet' };
  const payload = JSON.stringify(await buildBackup());
  try {
    const resp = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'text/plain;charset=utf-8' }, body: payload });
    if (!resp.ok) return { ok: false, message: 'Erreur serveur (' + resp.status + ') — vérifie l’URL du script' };
    const txt = await resp.text().catch(() => '');
    try {
      const json = JSON.parse(txt);
      if (json && json.ok === false) return { ok: false, message: 'Script Drive : ' + (json.erreur || 'erreur inconnue') };
    } catch { /* réponse non JSON : on garde le statut HTTP comme critère */ }
    return { ok: true, message: 'Sauvegarde envoyée' };
  } catch (e) {
    return { ok: false, message: 'Échec de l’envoi : ' + e.message };
  }
}

/** Sauvegarde automatique quotidienne (au démarrage, si activée et connectée). */
async function maybeAutoBackup() {
  if (!SETTINGS.driveAuto || !SETTINGS.driveUrl) return;
  if (!navigator.onLine) return;
  const today = UI.todayISO();
  if (lastAutoBackupDate() === today) return;
  const res = await sendToDrive();
  if (res.ok) {
    setLastAutoBackupDate(today);
    UI.toast('Sauvegarde Google Drive effectuée ✔', 'ok');
  }
}

/* ================================================================
   TABLEAU DE BORD
================================================================ */
VIEWS.dashboard = async function (el) {
  const today = UI.todayISO();
  const j5 = UI.addDays(today, -5);
  const [temps, receptions, services, nettoyages, refroids, decongels, entames, nonconfs, servicesJ5] = await Promise.all([
    DB.getByTypeAndRange('temp', today, today),
    DB.getByTypeAndRange('reception', today, today),
    DB.getByTypeAndRange('service', today, today),
    DB.getByTypeAndRange('nettoyage', today, today),
    DB.getByType('refroid'),
    DB.getByType('decongel'),
    DB.getByType('entame'),
    DB.getByType('nonconf'),
    DB.getByTypeAndRange('service', j5, j5),
  ]);
  const decDepasse = decongels.filter(isDecongelDepasse);
  const entPerimes = entames.filter(r => r.statut !== 'termine' && r.dlc && r.dlc < today);
  const entAujourdhui = entames.filter(r => r.statut !== 'termine' && r.dlc === today);
  const ncOuvertes = nonconfs.filter(r => r.statut === 'ouverte');
  // Plats témoins prélevés il y a exactement 5 jours : fin de conservation PMS, à retirer du frigo
  const temoinsARetirer = servicesJ5.filter(r => r.platTemoin);

  const dailyTasks = SETTINGS.cleaningTasks.filter(t => t.freq === 'quotidien');
  const doneTasks = new Set(nettoyages.map(n => n.taskId));
  const dailyDone = dailyTasks.filter(t => doneTasks.has(t.id)).length;
  const ncToday = [...temps, ...receptions, ...services, ...refroids.filter(r => r.date === today)].filter(r => r.conforme === false).length;
  const enCours = refroids.filter(r => r.status === 'encours');

  const equipsDone = new Set(temps.map(t => t.equipId));
  const equipsMissing = SETTINGS.equipements.filter(e => !equipsDone.has(e.id));

  // Sauvegarde Drive automatique en échec silencieux ? (aucune sauvegarde depuis > 3 jours)
  let driveWarn = '';
  if (SETTINGS.driveAuto && SETTINGS.driveUrl) {
    const last = lastAutoBackupDate();
    const jours = last ? Math.round((new Date(today + 'T12:00:00') - new Date(last + 'T12:00:00')) / 86400000) : null;
    if (!last || jours > 3) {
      driveWarn = '<div class="card" style="border-color:var(--orange)"><div class="row">' +
        '<span class="pill warn">☁️ Sauvegarde Google Drive : ' + (last ? 'dernière il y a ' + jours + ' jours' : 'jamais effectuée') + '</span>' +
        '<span class="muted" style="font-size:13px">Vérifie l’URL du script dans les Réglages puis « Sauvegarder maintenant ».</span>' +
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

    (enCours.length ? '<div class="card"><h2>⏱️ En cours</h2><div class="rec-list">' + enCours.map(r => {
      const mins = Math.round((Date.now() - new Date(r.startISO).getTime()) / 60000);
      const limit = r.mode === 'remise' ? RULES.remiseMinutes : RULES.refroidMinutes;
      return '<div class="rec-item ' + (mins > limit ? 'bad' : '') + '"><div class="big">' + mins + ' min</div>' +
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
      (temoinsARetirer.length ? '<div class="rec-item"><div class="big">🥡</div><div class="body"><div class="title">Plats témoins du ' + UI.frDate(j5) + ' à retirer</div><div class="meta">Fin des 5 jours de conservation : ' + temoinsARetirer.map(r => UI.esc(r.plat)).join(', ') + '</div></div></div>' : '') +
      '</div></div>' : '') +

    (equipsMissing.length ? '<div class="card"><h2>À faire</h2><p class="muted" style="margin-bottom:10px">Enceintes sans relevé aujourd’hui :</p><div class="row">' +
      equipsMissing.map(e => '<span class="pill warn">🌡️ ' + UI.esc(e.name) + '</span>').join('') +
      '</div><div class="spacer"></div><button class="btn" data-go="temperatures">Faire les relevés</button></div>' : '') +

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
  const recs = await DB.getByTypeAndRange('temp', today, today);

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
          ? rEq.map(r => '<span class="pill ' + (r.conforme === false ? 'bad' : 'ok') + '">' + UI.esc(r.moment) + ' ' + UI.fmtTemp(r.temp) + '</span>').join(' ')
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
  const hour = new Date().getHours();
  const defMoment = hour < 14 ? 'matin' : 'soir';
  const hasNext = queue && queue.length > 0;

  UI.modal(
    '<h2>' + UI.esc(eq.name) + (queue ? ' <span class="pill info">' + (queue.length + 1) + ' restante' + (queue.length ? 's' : '') + '</span>' : '') + '</h2>' +
    '<p class="muted" style="margin-bottom:14px">' + (eq.cible != null ? 'Valeur cible : ' + eq.cible + ' °C · ' : '') + 'Limites critiques : ' + eq.min + ' à ' + eq.max + ' °C</p>' +
    '<label class="field"><span class="lbl">Température relevée (°C)</span>' +
    UI.tempInputHTML('temp', { placeholder: eq.type === 'negatif' ? '-18.0' : '3.0', hint: eq.type === 'negatif' ? 'Enceinte négative : pense au signe − (bouton ±)' : '' }) + '</label>' +
    '<label class="field"><span class="lbl">Moment</span>' +
    UI.segHTML('moment', [{ value: 'matin', label: '🌅 Matin' }, { value: 'soir', label: '🌇 Soir' }], defMoment) + '</label>' +
    agentField() +
    '<div data-verdict></div>' +
    actionFieldHTML() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">' + (queue ? 'Arrêter' : 'Annuler') + '</button>' +
    '<button class="btn" data-x="save">' + (hasNext ? 'Enregistrer → suivante' : 'Enregistrer') + '</button></div>',
    (m, close) => {
      UI.segWire(m);
      const tempInput = m.querySelector('[data-f="temp"]');
      const verdict = m.querySelector('[data-verdict]');
      const actionField = m.querySelector('[data-action-field]');

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
          moment: UI.segValue(m, 'moment') || defMoment,
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
  const recs = (await DB.getByTypeAndRange('reception', UI.addDays(today, -6), today)).sort((a, b) => (b.date + b.time).localeCompare(a.date + a.time));

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
  const all = (await DB.getByType('refroid')).sort((a, b) => (b.date + b.timeStart).localeCompare(a.date + a.timeStart));
  const enCours = all.filter(r => r.status === 'encours');
  const finis = all.filter(r => r.status !== 'encours' && r.date >= UI.addDays(today, -6));

  el.innerHTML = headerHTML('Refroidissement & remise en T°', 'Refroidissement : +63→+10 °C en 2 h max · Remise : +10→+63 °C en 1 h max',
      '<button class="btn" id="new-refroid">➕ Démarrer un suivi</button>') +

    (enCours.length ? '<div class="card"><h2>⏱️ En cours</h2><div class="rec-list">' + enCours.map(r => {
      const mins = Math.round((Date.now() - new Date(r.startISO).getTime()) / 60000);
      const limit = r.mode === 'remise' ? RULES.remiseMinutes : RULES.refroidMinutes;
      return '<div class="rec-item ' + (mins > limit ? 'bad' : '') + '"><div class="big">' + mins + ' min</div>' +
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
  const recs = (await DB.getByTypeAndRange('service', today, today)).sort((a, b) => b.time.localeCompare(a.time));

  const temoinsToday = recs.filter(r => r.platTemoin).length;
  el.innerHTML = headerHTML('Températures de service', 'Avant chaque service (midi et soir) : chaude ≥ 63 °C · froide cible 3 °C, limite 6 °C (10 °C si conso < 2 h) — ' + UI.frDate(today),
      '<button class="btn" id="new-serv">➕ Nouveau contrôle</button>') +
    '<div class="card" style="padding:12px 18px"><div class="row">' +
    '<span class="pill ' + (temoinsToday ? 'ok' : 'warn') + '">🥡 Plats témoins du jour : ' + temoinsToday + '</span>' +
    '<span class="muted" style="font-size:13px">PMS : une portion ≥ 100 g de chaque plat (entrée, viande, légumes, dessert) avant chaque service, conservée 5 jours à 3 °C au frigo plats témoins.</span>' +
    '</div></div>' +
    (recs.length ? '<div class="rec-list">' + recs.map(r =>
      '<div class="rec-item ' + (r.conforme === false ? 'bad' : 'ok') + '">' +
      '<div class="big">' + UI.fmtTemp(r.temp) + '</div>' +
      '<div class="body"><div class="title">' + UI.esc(r.plat) + (r.platTemoin ? ' <span class="pill info">Plat témoin ✔</span>' : '') + '</div>' +
      '<div class="meta">' + (r.liaison === 'chaude' ? '🔥 Liaison chaude' : '❄️ Liaison froide') + ' — ' + UI.esc(r.time) + ' — ' + UI.esc(r.agent) +
      (r.conforme === false ? ' — ⚠️ ' + UI.esc(r.action || '') : '') + '</div></div>' +
      '<span class="pill ' + (r.conforme === false ? 'bad' : (r.tolere ? 'warn' : 'ok')) + '">' + (r.conforme === false ? 'Non conforme' : (r.tolere ? 'Toléré < 2 h' : 'Conforme')) + '</span></div>'
    ).join('') + '</div>' : '<div class="empty"><span class="e-ico">🍽️</span>Aucun contrôle aujourd’hui.</div>');

  el.querySelector('#new-serv').addEventListener('click', openServiceModal);
};

async function openServiceModal() {
  const menuNames = await getTodayMenuNames();
  UI.modal(
    '<h2>🍽️ Contrôle au service</h2>' +
    dishInputHTML('plat', 'Plat', 'Ex. : purée, salade de betteraves…', menuNames) +
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
          plat, liaison, temp: v, platTemoin: UI.segValue(m, 'temoin') === 'oui',
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
  const recs = (await DB.getByType('decongel')).sort((a, b) => (b.date + b.time).localeCompare(a.date + a.time));
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
    if (rec) { rec.statut = 'termine'; rec.sortieDate = UI.todayISO(); rec.sortieTime = UI.nowHM(); await DB.updateRecord(rec); UI.toast('Produit sorti de décongélation ✔', 'ok'); render(); }
  }));
};

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
  const recs = (await DB.getByType('entame')).sort((a, b) => (a.dlc || '').localeCompare(b.dlc || ''));
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
    if (rec) { rec.statut = 'termine'; rec.finDate = UI.todayISO(); await DB.updateRecord(rec); UI.toast('Produit clôturé ✔', 'ok'); render(); }
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

// Lit un fichier Excel/CSV en tableau de lignes (tableaux de cellules).
async function readSheetRows(file) {
  const XLSX = await ensureXLSX();
  const buf = await file.arrayBuffer();
  const wb = XLSX.read(buf, { type: 'array', cellDates: true });
  const sheet = wb.Sheets[wb.SheetNames[0]];
  return XLSX.utils.sheet_to_json(sheet, { header: 1, raw: true, defval: '' });
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
        let rows;
        try { rows = await readSheetRows(f); }
        catch (err) { step.innerHTML = '<p class="pill bad">' + UI.esc(err.message) + '</p>'; return; }
        rows = rows.filter(r => r.some(c => String(c).trim() !== ''));
        if (rows.length < 2) { step.innerHTML = '<p class="pill bad">Fichier vide ou illisible.</p>'; return; }
        buildMapping(step, rows, close, onDone);
      });
    }
  );
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

function openEtiquetteModal() {
  UI.modal(
    '<h2>🏷️ Nouvelle étiquette <span class="pill info" data-count style="display:none"></span></h2>' +
    '<label class="field"><span class="lbl">Photo de l’étiquette</span>' +
    '<input type="file" accept="image/*" capture="environment" data-f="photo" style="min-height:52px;padding:12px;border:1.5px dashed var(--border);border-radius:12px;width:100%"></label>' +
    '<div data-preview style="margin-bottom:12px"></div>' +
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
      const photoInput = m.querySelector('[data-f="photo"]');
      photoInput.addEventListener('change', async e => {
        const f = e.target.files[0];
        if (!f) return;
        try {
          photoData = await UI.shrinkImage(f, 1000);
          m.querySelector('[data-preview]').innerHTML = '<img src="' + photoData + '" class="photo-full" style="max-height:220px">';
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
        m.querySelector('[data-preview]').innerHTML = '';
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
  const recent = await DB.getByTypeAndRange('nettoyage', UI.addDays(today, -31), today);
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
  const recs = (await DB.getByTypeAndRange('huile', UI.addDays(today, -30), today)).sort((a, b) => (b.date + b.time).localeCompare(a.date + a.time));
  const ACTION_LABEL = { controle: 'Contrôle visuel', filtration: 'Filtration', changement: 'Changement d’huile' };
  const ETAT_LABEL = { bon: 'Bonne', moyen: 'À surveiller', 'a-changer': 'À changer' };

  el.innerHTML = headerHTML('Huiles de friture', 'Contrôle visuel quotidien · friture ≤ 175 °C · changer une huile foncée ou moussante',
      '<button class="btn" id="new-huile">➕ Nouveau contrôle</button>') +
    (recs.length ? '<div class="rec-list">' + recs.map(r =>
      '<div class="rec-item ' + (r.etat === 'a-changer' && r.action !== 'changement' ? 'bad' : 'ok') + '">' +
      '<div class="big">🍟</div>' +
      '<div class="body"><div class="title">' + UI.esc(r.friteuse) + ' — ' + (ACTION_LABEL[r.action] || r.action) + '</div>' +
      '<div class="meta">' + UI.frDate(r.date) + ' ' + UI.esc(r.time) + ' — huile : ' + (ETAT_LABEL[r.etat] || r.etat) +
      (r.temp != null ? ' — ' + UI.fmtTemp(r.temp) : '') + ' — ' + UI.esc(r.agent) +
      (r.remarque ? ' — ' + UI.esc(r.remarque) : '') + '</div></div>' +
      '<span class="pill ' + (r.etat === 'a-changer' ? 'warn' : 'ok') + '">' + (ETAT_LABEL[r.etat] || '') + '</span></div>'
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
    '<label class="field"><span class="lbl">Température de friture (°C, optionnel)</span>' +
    '<input type="number" step="1" inputmode="numeric" data-f="temp" placeholder="≤ 175"></label>' +
    '<label class="field"><span class="lbl">Remarque (optionnel)</span><input type="text" data-f="remarque"></label>' +
    agentField() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      UI.segWire(m);
      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const agent = requireAgent(m); if (!agent) return;
        const t = parseFloat(m.querySelector('[data-f="temp"]').value);
        await DB.addRecord({
          type: 'huile', date: UI.todayISO(), time: UI.nowHM(),
          friteuse: m.querySelector('[data-f="friteuse"]').value,
          action: UI.segValue(m, 'action'), etat: UI.segValue(m, 'etat'),
          temp: isNaN(t) ? null : t,
          remarque: m.querySelector('[data-f="remarque"]').value.trim(),
          agent,
        });
        close();
        UI.toast('Contrôle enregistré ✔', 'ok');
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
  const [manual, temps, receptions, services, refroids] = await Promise.all([
    DB.getByType('nonconf'),
    DB.getByTypeAndRange('temp', from, today),
    DB.getByTypeAndRange('reception', from, today),
    DB.getByTypeAndRange('service', from, today),
    DB.getByTypeAndRange('refroid', from, today),
  ]);

  const autos = [...temps, ...receptions, ...services, ...refroids]
    .filter(r => r.conforme === false)
    .map(r => ({
      date: r.date, time: r.time || r.timeEnd || '',
      objet: TYPE_LABELS[r.type] + ' — ' + (r.equipName || r.produit || r.plat || ''),
      description: r.type === 'temp' ? 'Relevé ' + UI.fmtTemp(r.temp)
        : r.type === 'refroid' ? UI.fmtTemp(r.tempStart) + ' → ' + UI.fmtTemp(r.tempEnd) + ' en ' + r.durationMin + ' min'
        : 'Relevé ' + UI.fmtTemp(r.temp),
      action: r.action, agent: r.agent, auto: true,
    }));

  const rows = [
    ...manual.map(r => Object.assign({ auto: false }, r)),
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
const EXPORT_COLUMNS = {
  temp: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Équipement', r => r.equipName], ['Moment', r => r.moment], ['Température (°C)', r => r.temp], ['Conforme', r => r.conforme === false ? 'NON' : 'OUI'], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  reception: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Fournisseur', r => r.fournisseur], ['Produit', r => r.produit], ['Lot / BL', r => r.lot], ['Famille', r => r.famille], ['Température (°C)', r => r.temp], ['État', r => r.etat === 'bad' ? 'Défaut' : 'Correct'], ['Conforme', r => r.conforme === false ? 'NON' : (r.tolere ? 'Contrôle à cœur' : 'OUI')], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  refroid: [['Date', r => UI.frDate(r.date)], ['Type', r => r.mode === 'remise' ? 'Remise en T°' : 'Refroidissement'], ['Préparation', r => r.produit], ['T° départ', r => r.tempStart], ['Heure départ', r => r.timeStart], ['T° fin', r => r.tempEnd], ['Heure fin', r => r.timeEnd], ['Durée (min)', r => r.durationMin], ['Conforme', r => r.status === 'encours' ? 'En cours' : (r.conforme === false ? 'NON' : 'OUI')], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  service: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Plat', r => r.plat], ['Liaison', r => r.liaison], ['Température (°C)', r => r.temp], ['Plat témoin', r => r.platTemoin ? 'OUI' : 'NON'], ['Conforme', r => r.conforme === false ? 'NON' : (r.tolere ? 'Toléré <2h' : 'OUI')], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  decongel: [['Date mise en décongélation', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Produit', r => r.produit], ['Fournisseur', r => r.fournisseur], ['Lot', r => r.lot], ['À utiliser avant', r => UI.frDate(r.limite)], ['Sorti le', r => r.sortieDate ? UI.frDate(r.sortieDate) + ' ' + (r.sortieTime || '') : ''], ['Statut', r => r.statut === 'termine' ? 'Terminé' : 'En cours'], ['Agent', r => r.agent]],
  entame: [['Date ouverture', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Produit', r => r.produit], ['Type', r => r.categorie], ['DLC interne', r => UI.frDate(r.dlc)], ['Clôturé le', r => r.finDate ? UI.frDate(r.finDate) : ''], ['Statut', r => r.statut === 'termine' ? 'Terminé' : 'En cours'], ['Agent', r => r.agent]],
  etiquette: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Produit', r => r.produit], ['Lot', r => r.lot], ['DLC', r => r.dlc ? UI.frDate(r.dlc) : ''], ['Photo', r => r.photo ? 'OUI' : 'NON'], ['Agent', r => r.agent]],
  nettoyage: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Tâche', r => r.taskName], ['Zone', r => r.zone], ['Fréquence', r => r.freq], ['Agent', r => r.agent]],
  huile: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Friteuse', r => r.friteuse], ['Opération', r => r.action], ['État huile', r => r.etat], ['Température (°C)', r => r.temp], ['Remarque', r => r.remarque], ['Agent', r => r.agent]],
  nonconf: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Objet', r => r.objet], ['Lieu', r => r.lieu], ['Lot', r => r.lot], ['Péremption', r => r.peremption ? UI.frDate(r.peremption) : ''], ['Description', r => r.description], ['Action corrective', r => r.action], ['Statut', r => r.statut], ['Agent', r => r.agent]],
};

VIEWS.historique = async function (el) {
  const today = UI.todayISO();
  const state = VIEWS.historique._state || (VIEWS.historique._state = { type: 'temp', from: UI.addDays(today, -6), to: today });

  el.innerHTML = headerHTML('Historique & export', 'Consultation des enregistrements et export CSV pour les contrôles sanitaires') +
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
    '<button class="btn small" id="h-export">⬇️ Exporter ce registre (CSV)</button>' +
    '<button class="btn small" id="h-export-all">📁 Tout exporter (inspection)</button>' +
    '<button class="btn small secondary" id="h-print">🖨️ Imprimer</button>' +
    '</div></div>' +
    '<div class="card"><div class="table-wrap" id="h-table"></div></div>';

  async function refreshTable() {
    const recs = (await DB.getByTypeAndRange(state.type, state.from, state.to)).sort((a, b) => (b.date + (b.time || '')).localeCompare(a.date + (a.time || '')));
    const cols = EXPORT_COLUMNS[state.type];
    const box = document.getElementById('h-table');
    if (!box) return; // l'utilisateur a quitté la vue pendant la requête
    // En-tête officiel, visible uniquement à l'impression (classeur PMS)
    const printHeader = '<div class="print-header"><h2>' + UI.esc(SETTINGS.etablissement) + '</h2>' +
      'Registre : <b>' + UI.esc(TYPE_LABELS[state.type]) + '</b> — Période : du ' + UI.frDate(state.from) + ' au ' + UI.frDate(state.to) +
      ' — Édité le ' + UI.frDate(UI.todayISO()) + ' à ' + UI.nowHM() +
      '<div class="visa">Visa du responsable : ______________________</div></div>';
    box.innerHTML = printHeader + (recs.length
      ? '<table><thead><tr>' + cols.map(c => '<th>' + UI.esc(c[0]) + '</th>').join('') + '</tr></thead><tbody>' +
        recs.map(r => '<tr' + (r.conforme === false ? ' style="background:var(--red-light)"' : '') + '>' +
          cols.map(c => '<td>' + UI.esc(c[1](r) == null ? '' : c[1](r)) + '</td>').join('') + '</tr>').join('') +
        '</tbody></table>'
      : '<div class="empty">Aucun enregistrement sur cette période.</div>');
  }

  el.querySelector('#h-type').addEventListener('change', e => { state.type = e.target.value; refreshTable(); });
  el.querySelector('#h-from').addEventListener('change', e => { state.from = e.target.value; refreshTable(); });
  el.querySelector('#h-to').addEventListener('change', e => { state.to = e.target.value; refreshTable(); });
  el.querySelector('#h-print').addEventListener('click', () => window.print());

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
      const cols = EXPORT_COLUMNS[type];
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
    const cols = EXPORT_COLUMNS[state.type];
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
VIEWS.parametres = async function (el) {
  const FREQ_LABEL = { quotidien: 'Quotidien', hebdomadaire: 'Hebdomadaire', mensuel: 'Mensuel' };

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

    '<div class="card"><h2>☁️ Sauvegarde automatique Google Drive</h2>' +
    '<p class="muted" style="margin-bottom:12px">La tablette étant connectée à Internet, l’application peut déposer chaque jour une sauvegarde dans ton Google Drive. Il faut une seule fois coller l’adresse du script (voir la notice <b>GOOGLE-DRIVE.md</b>).</p>' +
    '<label class="field"><span class="lbl">Adresse du script Google Drive</span>' +
    '<input type="text" id="s-drive-url" value="' + UI.esc(SETTINGS.driveUrl || '') + '" placeholder="https://script.google.com/macros/s/.../exec"></label>' +
    '<label class="field" style="display:flex;align-items:center;gap:12px"><input type="checkbox" id="s-drive-auto" ' + (SETTINGS.driveAuto ? 'checked' : '') + ' style="width:26px;height:26px;min-height:0">' +
    '<span class="lbl" style="margin:0;text-transform:none;letter-spacing:0;font-size:15px">Sauvegarde automatique quotidienne</span></label>' +
    '<div class="row"><button class="btn small" id="s-drive-save">Enregistrer</button>' +
    '<button class="btn small secondary" id="s-drive-now">☁️ Sauvegarder maintenant</button></div>' +
    (lastAutoBackupDate() ? '<p class="muted" style="margin-top:10px">Dernière sauvegarde auto : ' + UI.frDate(lastAutoBackupDate()) + '</p>' : '') +
    '</div>' +

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

  // Google Drive
  el.querySelector('#s-drive-save').addEventListener('click', async () => {
    SETTINGS.driveUrl = el.querySelector('#s-drive-url').value.trim();
    SETTINGS.driveAuto = el.querySelector('#s-drive-auto').checked;
    await saveSettings();
    UI.toast('Réglages Drive enregistrés ✔', 'ok');
  });
  el.querySelector('#s-drive-now').addEventListener('click', async () => {
    SETTINGS.driveUrl = el.querySelector('#s-drive-url').value.trim();
    SETTINGS.driveAuto = el.querySelector('#s-drive-auto').checked;
    await saveSettings();
    if (!SETTINGS.driveUrl) { UI.toast('Colle d’abord l’adresse du script', 'bad'); return; }
    UI.toast('Envoi en cours…');
    const res = await sendToDrive();
    if (res.ok) { setLastAutoBackupDate(UI.todayISO()); UI.toast('Sauvegarde envoyée sur Drive ✔', 'ok'); render(); }
    else UI.toast(res.message, 'bad');
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
  el.querySelector('#s-backup').addEventListener('click', async () => {
    const records = await DB.getAllRecords();
    const blob = new Blob([JSON.stringify({ app: 'haccp-cuisine', version: 1, exportedAt: new Date().toISOString(), settings: SETTINGS, records })], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'sauvegarde-haccp-' + UI.todayISO() + '.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    UI.toast('Sauvegarde exportée ✔', 'ok');
  });
  el.querySelector('#s-restore').addEventListener('click', () => el.querySelector('#s-restore-file').click());
  el.querySelector('#s-restore-file').addEventListener('change', async e => {
    const f = e.target.files[0];
    if (!f) return;
    try {
      const data = JSON.parse(await f.text());
      if (data.app !== 'haccp-cuisine' || !Array.isArray(data.records)) throw new Error('fichier invalide');
      UI.confirm('Restaurer ' + data.records.length + ' enregistrements ? Ils s’ajoutent aux données actuelles.', async () => {
        SETTINGS = Object.assign({}, DEFAULT_SETTINGS, data.settings);
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
        UI.toast('Sauvegarde restaurée ✔', 'ok');
        render();
      });
    } catch {
      UI.toast('Fichier de sauvegarde invalide', 'bad');
    }
  });
};

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
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tryBackup(); });
})();
