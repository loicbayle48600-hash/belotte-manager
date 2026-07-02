/* ===== HACCP Cuisine — application =====
 * Seuils réglementaires (GBPH restauration collective) :
 *  - Froid positif : 0 à +4 °C  |  Froid négatif : ≤ −18 °C
 *  - Réception produits frais ≤ +4 °C ; surgelés ≤ −15 °C (tolérance ponctuelle)
 *  - Liaison chaude ≥ +63 °C  |  Liaison froide ≤ +10 °C
 *  - Refroidissement rapide : +63 °C → +10 °C en moins de 2 h
 *  - Remise en température : +10 °C → +63 °C en moins d'1 h
 */
const RULES = {
  fraisMax: 4,
  surgeleMax: -15,
  chaudMin: 63,
  froidMax: 10,
  refroidTarget: 10,
  refroidMinutes: 120,
  remiseTarget: 63,
  remiseMinutes: 60,
};

const DEFAULT_SETTINGS = {
  etablissement: 'Cuisine EHPAD / FAM',
  agents: [],
  equipements: [
    { id: 'e1', name: 'Frigo 1', type: 'positif', min: 0, max: 4 },
    { id: 'e2', name: 'Frigo 2', type: 'positif', min: 0, max: 4 },
    { id: 'e3', name: 'Chambre froide', type: 'positif', min: 0, max: 4 },
    { id: 'e4', name: 'Congélateur', type: 'negatif', min: -30, max: -18 },
  ],
  friteuses: ['Friteuse 1'],
  cleaningTasks: [
    { id: 'n1', name: 'Plans de travail', zone: 'Cuisine', freq: 'quotidien' },
    { id: 'n2', name: 'Sols cuisine', zone: 'Cuisine', freq: 'quotidien' },
    { id: 'n3', name: 'Éviers et robinetterie', zone: 'Plonge', freq: 'quotidien' },
    { id: 'n4', name: 'Poignées de portes et interrupteurs', zone: 'Cuisine', freq: 'quotidien' },
    { id: 'n5', name: 'Intérieur des frigos', zone: 'Chambre froide', freq: 'hebdomadaire' },
    { id: 'n6', name: 'Hotte et filtres', zone: 'Cuisine', freq: 'hebdomadaire' },
    { id: 'n7', name: 'Murs et étagères', zone: 'Réserve', freq: 'mensuel' },
    { id: 'n8', name: 'Dégivrage congélateur', zone: 'Chambre froide', freq: 'mensuel' },
  ],
};

const TYPE_LABELS = {
  temp: 'Température enceinte',
  reception: 'Réception livraison',
  refroid: 'Refroidissement / remise en T°',
  service: 'Température de service',
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
  SETTINGS = saved ? Object.assign({}, DEFAULT_SETTINGS, saved) : JSON.parse(JSON.stringify(DEFAULT_SETTINGS));
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

/* ================================================================
   TABLEAU DE BORD
================================================================ */
VIEWS.dashboard = async function (el) {
  const today = UI.todayISO();
  const [temps, receptions, services, nettoyages, refroids, all] = await Promise.all([
    DB.getByTypeAndRange('temp', today, today),
    DB.getByTypeAndRange('reception', today, today),
    DB.getByTypeAndRange('service', today, today),
    DB.getByTypeAndRange('nettoyage', today, today),
    DB.getByType('refroid'),
    DB.getByTypeAndRange('temp', UI.addDays(today, -6), today),
  ]);

  const dailyTasks = SETTINGS.cleaningTasks.filter(t => t.freq === 'quotidien');
  const doneTasks = new Set(nettoyages.map(n => n.taskId));
  const dailyDone = dailyTasks.filter(t => doneTasks.has(t.id)).length;
  const ncToday = [...temps, ...receptions, ...services].filter(r => r.conforme === false).length;
  const enCours = refroids.filter(r => r.status === 'encours');

  const equipsDone = new Set(temps.map(t => t.equipId));
  const equipsMissing = SETTINGS.equipements.filter(e => !equipsDone.has(e.id));

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
    '<div class="stat ' + (temps.length ? 'ok' : '') + '"><div class="n">' + temps.length + '/' + (SETTINGS.equipements.length * 2) + '</div><div class="t">Relevés enceintes (matin + soir)</div></div>' +
    '<div class="stat ' + (dailyDone === dailyTasks.length && dailyTasks.length ? 'ok' : '') + '"><div class="n">' + dailyDone + '/' + dailyTasks.length + '</div><div class="t">Nettoyage quotidien</div></div>' +
    '<div class="stat"><div class="n">' + receptions.length + '</div><div class="t">Réceptions du jour</div></div>' +
    '<div class="stat ' + (ncToday ? 'bad' : 'ok') + '"><div class="n">' + ncToday + '</div><div class="t">Non-conformités du jour</div></div>' +
    '</div>' +

    (enCours.length ? '<div class="card"><h2>⏱️ En cours</h2><div class="rec-list">' + enCours.map(r => {
      const mins = Math.round((Date.now() - new Date(r.startISO).getTime()) / 60000);
      const limit = r.mode === 'remise' ? RULES.remiseMinutes : RULES.refroidMinutes;
      return '<div class="rec-item ' + (mins > limit ? 'bad' : '') + '"><div class="big">' + mins + ' min</div>' +
        '<div class="body"><div class="title">' + UI.esc(r.produit) + '</div>' +
        '<div class="meta">' + (r.mode === 'remise' ? 'Remise en température' : 'Refroidissement') + ' — départ ' + UI.esc(r.timeStart) + ' à ' + UI.fmtTemp(r.tempStart) + (mins > limit ? ' — ⚠️ délai dépassé !' : '') + '</div></div>' +
        '<button class="btn small" data-go="refroidissement">Terminer</button></div>';
    }).join('') + '</div></div>' : '') +

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

  el.innerHTML = headerHTML('Enceintes froides', 'Relevés du ' + UI.frDate(today) + ' — 2 relevés/jour recommandés (matin et soir)') +
    '<div class="grid cols-3" id="equip-grid">' +
    SETTINGS.equipements.map(eq => {
      const rEq = recs.filter(r => r.equipId === eq.id).sort((a, b) => a.time < b.time ? -1 : 1);
      const hasBad = rEq.some(r => r.conforme === false);
      const cls = hasBad ? 'alert' : (rEq.length ? 'done' : '');
      return '<button class="equip-tile ' + cls + '" data-eq="' + eq.id + '">' +
        '<div class="name">' + (eq.type === 'negatif' ? '🧊' : '❄️') + ' ' + UI.esc(eq.name) + '</div>' +
        '<div class="range">Consigne : ' + eq.min + ' à ' + eq.max + ' °C</div>' +
        '<div class="last">' + (rEq.length
          ? rEq.map(r => '<span class="pill ' + (r.conforme === false ? 'bad' : 'ok') + '">' + UI.esc(r.moment) + ' ' + UI.fmtTemp(r.temp) + '</span>').join(' ')
          : '<span class="pill warn">Aucun relevé aujourd’hui</span>') + '</div>' +
        '</button>';
    }).join('') + '</div>' +
    (SETTINGS.equipements.length === 0 ? '<div class="empty"><span class="e-ico">⚙️</span>Ajoute tes équipements dans les Réglages.</div>' : '');

  el.querySelectorAll('[data-eq]').forEach(tile => tile.addEventListener('click', () => openTempModal(tile.dataset.eq)));
};

function openTempModal(equipId) {
  const eq = SETTINGS.equipements.find(e => e.id === equipId);
  if (!eq) return;
  const hour = new Date().getHours();
  const defMoment = hour < 14 ? 'matin' : 'soir';

  UI.modal(
    '<h2>' + UI.esc(eq.name) + '</h2>' +
    '<p class="muted" style="margin-bottom:14px">Consigne : ' + eq.min + ' à ' + eq.max + ' °C</p>' +
    '<label class="field"><span class="lbl">Température relevée (°C)</span>' +
    '<input type="number" step="0.1" inputmode="decimal" class="temp-input" data-f="temp" placeholder="0.0" autofocus></label>' +
    '<label class="field"><span class="lbl">Moment</span>' +
    UI.segHTML('moment', [{ value: 'matin', label: '🌅 Matin' }, { value: 'soir', label: '🌇 Soir' }], defMoment) + '</label>' +
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
        const ok = v >= eq.min && v <= eq.max;
        verdict.innerHTML = ok
          ? '<p class="pill ok" style="margin-bottom:12px">✔ Conforme</p>'
          : '<p class="pill bad" style="margin-bottom:12px">✘ NON CONFORME (' + eq.min + ' à ' + eq.max + ' °C)</p>';
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
      '<div class="meta">' + UI.frDate(r.date) + ' ' + UI.esc(r.time) + ' — ' + UI.esc(r.famille) + ' — ' + UI.esc(r.agent) +
      (r.conforme === false ? ' — ⚠️ ' + UI.esc(r.action || 'non conforme') : '') + '</div></div>' +
      '<span class="pill ' + (r.conforme === false ? 'bad' : 'ok') + '">' + (r.conforme === false ? 'Non conforme' : 'Conforme') + '</span></div>'
    ).join('') + '</div>' : '<div class="empty"><span class="e-ico">🚚</span>Aucune réception enregistrée cette semaine.</div>');

  el.querySelector('#new-rec').addEventListener('click', openReceptionModal);
};

async function openReceptionModal() {
  const past = await DB.getByType('reception');
  const fournisseurs = [...new Set(past.map(r => r.fournisseur).filter(Boolean))];

  UI.modal(
    '<h2>🚚 Nouvelle réception</h2>' +
    '<label class="field"><span class="lbl">Fournisseur</span>' +
    '<input type="text" data-f="fournisseur" list="dl-fourn" placeholder="Nom du fournisseur">' +
    '<datalist id="dl-fourn">' + fournisseurs.map(f => '<option value="' + UI.esc(f) + '">').join('') + '</datalist></label>' +
    '<label class="field"><span class="lbl">Produit / livraison</span>' +
    '<input type="text" data-f="produit" placeholder="Ex. : viande hachée, produits laitiers…"></label>' +
    '<label class="field"><span class="lbl">Famille de produits</span>' +
    UI.segHTML('famille', [
      { value: 'frais', label: '❄️ Frais (≤ 4 °C)' },
      { value: 'surgele', label: '🧊 Surgelé (≤ −15 °C)' },
      { value: 'epicerie', label: '📦 Épicerie / sec' },
    ], 'frais') + '</label>' +
    '<label class="field"><span class="lbl">Température à réception (°C)</span>' +
    '<input type="number" step="0.1" inputmode="decimal" class="temp-input" data-f="temp" placeholder="—"></label>' +
    '<label class="field"><span class="lbl">État (emballage, DLC, propreté camion)</span>' +
    UI.segHTML('etat', [{ value: 'ok', label: '✔ Correct' }, { value: 'bad', label: '✘ Défaut constaté', bad: true }], 'ok') + '</label>' +
    agentField() +
    '<div data-verdict></div>' +
    actionFieldHTML() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      UI.segWire(m);
      const verdict = m.querySelector('[data-verdict]');
      const actionField = m.querySelector('[data-action-field]');

      const evalConf = () => {
        const fam = UI.segValue(m, 'famille');
        const etat = UI.segValue(m, 'etat');
        const t = parseFloat(m.querySelector('[data-f="temp"]').value);
        let ok = etat !== 'bad';
        if (fam === 'frais' && !isNaN(t) && t > RULES.fraisMax) ok = false;
        if (fam === 'surgele' && !isNaN(t) && t > RULES.surgeleMax) ok = false;
        verdict.innerHTML = ok
          ? '<p class="pill ok" style="margin-bottom:12px">✔ Conforme</p>'
          : '<p class="pill bad" style="margin-bottom:12px">✘ NON CONFORME</p>';
        actionField.style.display = ok ? 'none' : 'block';
        return ok;
      };
      m.addEventListener('click', () => setTimeout(evalConf, 30));
      m.querySelector('[data-f="temp"]').addEventListener('input', evalConf);

      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const fournisseur = m.querySelector('[data-f="fournisseur"]').value.trim();
        const produit = m.querySelector('[data-f="produit"]').value.trim();
        if (!fournisseur || !produit) { UI.toast('Fournisseur et produit sont obligatoires', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        const ok = evalConf();
        const action = m.querySelector('[data-f="action"]').value.trim();
        if (!ok && !action) { UI.toast('Indique l’action corrective (refus, réserve…)', 'bad'); return; }
        const t = parseFloat(m.querySelector('[data-f="temp"]').value);
        await DB.addRecord({
          type: 'reception', date: UI.todayISO(), time: UI.nowHM(),
          fournisseur, produit, famille: UI.segValue(m, 'famille'),
          temp: isNaN(t) ? null : t, etat: UI.segValue(m, 'etat'),
          conforme: ok, action: ok ? '' : action, agent,
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
  const all = (await DB.getByTypeAndRange('refroid', UI.addDays(today, -6), today)).sort((a, b) => (b.date + b.timeStart).localeCompare(a.date + a.timeStart));
  const enCours = all.filter(r => r.status === 'encours');
  const finis = all.filter(r => r.status !== 'encours');

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

function openRefroidStartModal() {
  UI.modal(
    '<h2>Démarrer un suivi</h2>' +
    '<label class="field"><span class="lbl">Type</span>' +
    UI.segHTML('mode', [
      { value: 'refroidissement', label: '📉 Refroidissement (2 h max)' },
      { value: 'remise', label: '🔥 Remise en T° (1 h max)' },
    ], 'refroidissement') + '</label>' +
    '<label class="field"><span class="lbl">Préparation / plat</span>' +
    '<input type="text" data-f="produit" placeholder="Ex. : blanquette de veau"></label>' +
    '<label class="field"><span class="lbl">Température de départ (°C)</span>' +
    '<input type="number" step="0.1" inputmode="decimal" class="temp-input" data-f="temp" placeholder="63.0"></label>' +
    agentField() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">▶ Démarrer</button></div>',
    (m, close) => {
      UI.segWire(m);
      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const produit = m.querySelector('[data-f="produit"]').value.trim();
        const t = parseFloat(m.querySelector('[data-f="temp"]').value);
        if (!produit || isNaN(t)) { UI.toast('Renseigne le plat et la température', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        await DB.addRecord({
          type: 'refroid', date: UI.todayISO(), mode: UI.segValue(m, 'mode'),
          produit, tempStart: t, timeStart: UI.nowHM(), startISO: new Date().toISOString(),
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
    '<input type="number" step="0.1" inputmode="decimal" class="temp-input" data-f="temp" placeholder="—"></label>' +
    agentField(rec.agent) +
    '<div data-verdict></div>' +
    actionFieldHTML() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      const tempInput = m.querySelector('[data-f="temp"]');
      const verdict = m.querySelector('[data-verdict]');
      const actionField = m.querySelector('[data-action-field]');
      const mins = Math.round((Date.now() - new Date(rec.startISO).getTime()) / 60000);

      const check = () => {
        const v = parseFloat(tempInput.value);
        if (isNaN(v)) { verdict.innerHTML = ''; actionField.style.display = 'none'; return null; }
        const tempOK = rec.mode === 'remise' ? v >= target : v <= target;
        const ok = tempOK && mins <= limit;
        verdict.innerHTML = '<p class="pill ' + (ok ? 'ok' : 'bad') + '" style="margin-bottom:12px">' +
          (ok ? '✔ Conforme' : '✘ NON CONFORME') + ' — durée : ' + mins + ' min / ' + limit + ' min</p>';
        actionField.style.display = ok ? 'none' : 'block';
        return ok;
      };
      tempInput.addEventListener('input', check);

      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const v = parseFloat(tempInput.value);
        if (isNaN(v)) { UI.toast('Saisis la température finale', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        const tempOK = rec.mode === 'remise' ? v >= target : v <= target;
        const ok = tempOK && mins <= limit;
        const action = m.querySelector('[data-f="action"]').value.trim();
        if (!ok && !action) { UI.toast('Indique l’action corrective (prolongation, jet du produit…)', 'bad'); return; }
        Object.assign(rec, {
          tempEnd: v, timeEnd: UI.nowHM(), durationMin: mins,
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

  el.innerHTML = headerHTML('Températures de service', 'Liaison chaude ≥ 63 °C · liaison froide ≤ 10 °C — ' + UI.frDate(today),
      '<button class="btn" id="new-serv">➕ Nouveau contrôle</button>') +
    (recs.length ? '<div class="rec-list">' + recs.map(r =>
      '<div class="rec-item ' + (r.conforme === false ? 'bad' : 'ok') + '">' +
      '<div class="big">' + UI.fmtTemp(r.temp) + '</div>' +
      '<div class="body"><div class="title">' + UI.esc(r.plat) + (r.platTemoin ? ' <span class="pill info">Plat témoin ✔</span>' : '') + '</div>' +
      '<div class="meta">' + (r.liaison === 'chaude' ? '🔥 Liaison chaude' : '❄️ Liaison froide') + ' — ' + UI.esc(r.time) + ' — ' + UI.esc(r.agent) +
      (r.conforme === false ? ' — ⚠️ ' + UI.esc(r.action || '') : '') + '</div></div>' +
      '<span class="pill ' + (r.conforme === false ? 'bad' : 'ok') + '">' + (r.conforme === false ? 'Non conforme' : 'Conforme') + '</span></div>'
    ).join('') + '</div>' : '<div class="empty"><span class="e-ico">🍽️</span>Aucun contrôle aujourd’hui.<br>Pense au plat témoin (100 g, 5 jours entre 0 et 3 °C).</div>');

  el.querySelector('#new-serv').addEventListener('click', openServiceModal);
};

function openServiceModal() {
  UI.modal(
    '<h2>🍽️ Contrôle au service</h2>' +
    '<label class="field"><span class="lbl">Plat</span>' +
    '<input type="text" data-f="plat" placeholder="Ex. : purée, salade de betteraves…"></label>' +
    '<label class="field"><span class="lbl">Liaison</span>' +
    UI.segHTML('liaison', [
      { value: 'chaude', label: '🔥 Chaude (≥ 63 °C)' },
      { value: 'froide', label: '❄️ Froide (≤ 10 °C)' },
    ], 'chaude') + '</label>' +
    '<label class="field"><span class="lbl">Température (°C)</span>' +
    '<input type="number" step="0.1" inputmode="decimal" class="temp-input" data-f="temp" placeholder="—"></label>' +
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
        verdict.innerHTML = '<p class="pill ' + (ok ? 'ok' : 'bad') + '" style="margin-bottom:12px">' + (ok ? '✔ Conforme' : '✘ NON CONFORME') + '</p>';
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
   TRAÇABILITÉ ÉTIQUETTES (photos)
================================================================ */
VIEWS.tracabilite = async function (el) {
  const today = UI.todayISO();
  const recs = (await DB.getByTypeAndRange('etiquette', UI.addDays(today, -30), today)).sort((a, b) => (b.date + (b.time || '')).localeCompare(a.date + (a.time || '')));

  el.innerHTML = headerHTML('Traçabilité des étiquettes', 'Photographie les étiquettes des produits utilisés (30 derniers jours affichés)',
      '<button class="btn" id="new-eti">📷 Nouvelle étiquette</button>') +
    (recs.length ? '<div class="photo-grid">' + recs.map(r =>
      '<div class="photo-card" data-id="' + r.id + '">' +
      (r.photo ? '<img src="' + r.photo + '" alt="étiquette">' : '<div style="height:120px;display:flex;align-items:center;justify-content:center;font-size:40px;background:#eef">🏷️</div>') +
      '<div class="cap"><b>' + UI.esc(r.produit || 'Produit') + '</b>' + UI.frDate(r.date) + (r.dlc ? ' · DLC ' + UI.frDate(r.dlc) : '') + '</div></div>'
    ).join('') + '</div>' : '<div class="empty"><span class="e-ico">🏷️</span>Aucune étiquette enregistrée.</div>');

  el.querySelector('#new-eti').addEventListener('click', openEtiquetteModal);
  el.querySelectorAll('.photo-card').forEach(c => c.addEventListener('click', () => openEtiquetteDetail(Number(c.dataset.id))));
};

function openEtiquetteModal() {
  UI.modal(
    '<h2>🏷️ Nouvelle étiquette</h2>' +
    '<label class="field"><span class="lbl">Photo de l’étiquette</span>' +
    '<input type="file" accept="image/*" capture="environment" data-f="photo" style="min-height:52px;padding:12px;border:1.5px dashed var(--border);border-radius:12px;width:100%"></label>' +
    '<div data-preview style="margin-bottom:12px"></div>' +
    '<label class="field"><span class="lbl">Produit</span><input type="text" data-f="produit" placeholder="Ex. : escalope de dinde"></label>' +
    '<div class="row"><div class="grow"><label class="field"><span class="lbl">N° de lot (optionnel)</span><input type="text" data-f="lot"></label></div>' +
    '<div class="grow"><label class="field"><span class="lbl">DLC / DDM (optionnel)</span><input type="date" data-f="dlc"></label></div></div>' +
    agentField() +
    '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Enregistrer</button></div>',
    (m, close) => {
      let photoData = null;
      m.querySelector('[data-f="photo"]').addEventListener('change', async e => {
        const f = e.target.files[0];
        if (!f) return;
        try {
          photoData = await UI.shrinkImage(f, 1000);
          m.querySelector('[data-preview]').innerHTML = '<img src="' + photoData + '" class="photo-full" style="max-height:220px">';
        } catch { UI.toast('Impossible de lire la photo', 'bad'); }
      });
      m.querySelector('[data-x="cancel"]').onclick = close;
      m.querySelector('[data-x="save"]').onclick = async () => {
        const produit = m.querySelector('[data-f="produit"]').value.trim();
        if (!photoData && !produit) { UI.toast('Ajoute une photo ou le nom du produit', 'bad'); return; }
        const agent = requireAgent(m); if (!agent) return;
        await DB.addRecord({
          type: 'etiquette', date: UI.todayISO(), time: UI.nowHM(),
          produit, photo: photoData,
          lot: m.querySelector('[data-f="lot"]').value.trim(),
          dlc: m.querySelector('[data-f="dlc"]').value,
          agent,
        });
        close();
        UI.toast('Étiquette enregistrée ✔', 'ok');
        render();
      };
    }
  );
}

async function openEtiquetteDetail(id) {
  const r = await DB.getRecord(id);
  if (!r) return;
  UI.modal(
    '<h2>' + UI.esc(r.produit || 'Étiquette') + '</h2>' +
    (r.photo ? '<img src="' + r.photo + '" class="photo-full">' : '') +
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

  const sections = ['quotidien', 'hebdomadaire', 'mensuel'].map(freq => {
    const tasks = SETTINGS.cleaningTasks.filter(t => t.freq === freq);
    if (!tasks.length) return '';
    return '<div class="card"><h2>' + FREQ_LABEL[freq] + '</h2>' + tasks.map(t => {
      const last = lastDone[t.id];
      const isDone = last && last.date >= UI.addDays(today, -FREQ_DAYS[freq]);
      const doneToday = last && last.date === today;
      return '<div class="task-row ' + (isDone ? 'done' : '') + '" data-task="' + t.id + '">' +
        '<button class="check" data-check="' + t.id + '" data-donetoday="' + (doneToday ? '1' : '') + '">✔</button>' +
        '<div class="body" style="flex:1"><div class="tname">' + UI.esc(t.name) + '</div>' +
        '<div class="zone">' + UI.esc(t.zone) + ' · <span class="tag-freq">' + FREQ_LABEL[freq] + '</span>' +
        (last ? ' · fait le ' + UI.frDate(last.date) + ' par ' + UI.esc(last.agent) : ' · jamais fait') + '</div></div></div>';
    }).join('') + '</div>';
  }).join('');

  el.innerHTML = headerHTML('Plan de nettoyage', 'Coche chaque tâche une fois réalisée — traçabilité automatique (date + agent)') +
    (SETTINGS.cleaningTasks.length ? sections : '<div class="empty"><span class="e-ico">🧽</span>Ajoute les tâches de nettoyage dans les Réglages.</div>');

  el.querySelectorAll('[data-check]').forEach(btn => btn.addEventListener('click', async () => {
    const taskId = btn.dataset.check;
    const task = SETTINGS.cleaningTasks.find(t => t.id === taskId);
    if (!task) return;

    if (btn.dataset.donetoday === '1') {
      // décocher : supprime l'enregistrement du jour
      const todays = (await DB.getByTypeAndRange('nettoyage', today, today)).filter(r => r.taskId === taskId);
      for (const r of todays) await DB.deleteRecord(r.id);
      UI.toast('Tâche décochée');
      render();
      return;
    }

    const doSave = async agent => {
      await DB.addRecord({
        type: 'nettoyage', date: today, time: UI.nowHM(),
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
    '<label class="field"><span class="lbl">Objet</span>' +
    '<input type="text" data-f="objet" placeholder="Ex. : panne frigo 2, produit périmé en réserve…"></label>' +
    '<label class="field"><span class="lbl">Description</span>' +
    '<textarea data-f="description" placeholder="Décris le problème constaté"></textarea></label>' +
    '<label class="field"><span class="lbl">Action corrective mise en place</span>' +
    '<textarea data-f="action" placeholder="Ex. : denrées déplacées au frigo 1, dépanneur appelé…"></textarea></label>' +
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
  reception: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Fournisseur', r => r.fournisseur], ['Produit', r => r.produit], ['Famille', r => r.famille], ['Température (°C)', r => r.temp], ['État', r => r.etat === 'bad' ? 'Défaut' : 'Correct'], ['Conforme', r => r.conforme === false ? 'NON' : 'OUI'], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  refroid: [['Date', r => UI.frDate(r.date)], ['Type', r => r.mode === 'remise' ? 'Remise en T°' : 'Refroidissement'], ['Préparation', r => r.produit], ['T° départ', r => r.tempStart], ['Heure départ', r => r.timeStart], ['T° fin', r => r.tempEnd], ['Heure fin', r => r.timeEnd], ['Durée (min)', r => r.durationMin], ['Conforme', r => r.status === 'encours' ? 'En cours' : (r.conforme === false ? 'NON' : 'OUI')], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  service: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Plat', r => r.plat], ['Liaison', r => r.liaison], ['Température (°C)', r => r.temp], ['Plat témoin', r => r.platTemoin ? 'OUI' : 'NON'], ['Conforme', r => r.conforme === false ? 'NON' : 'OUI'], ['Action corrective', r => r.action], ['Agent', r => r.agent]],
  etiquette: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Produit', r => r.produit], ['Lot', r => r.lot], ['DLC', r => r.dlc ? UI.frDate(r.dlc) : ''], ['Photo', r => r.photo ? 'OUI' : 'NON'], ['Agent', r => r.agent]],
  nettoyage: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Tâche', r => r.taskName], ['Zone', r => r.zone], ['Fréquence', r => r.freq], ['Agent', r => r.agent]],
  huile: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Friteuse', r => r.friteuse], ['Opération', r => r.action], ['État huile', r => r.etat], ['Température (°C)', r => r.temp], ['Remarque', r => r.remarque], ['Agent', r => r.agent]],
  nonconf: [['Date', r => UI.frDate(r.date)], ['Heure', r => r.time], ['Objet', r => r.objet], ['Description', r => r.description], ['Action corrective', r => r.action], ['Statut', r => r.statut], ['Agent', r => r.agent]],
};

VIEWS.historique = async function (el) {
  const today = UI.todayISO();
  const state = VIEWS.historique._state || (VIEWS.historique._state = { type: 'temp', from: UI.addDays(today, -6), to: today });

  el.innerHTML = headerHTML('Historique & export', 'Consultation des enregistrements et export CSV pour les contrôles sanitaires') +
    '<div class="card"><div class="row">' +
    '<div class="grow"><label class="field" style="margin:0"><span class="lbl">Registre</span><select id="h-type">' +
    Object.keys(TYPE_LABELS).map(t => '<option value="' + t + '"' + (t === state.type ? ' selected' : '') + '>' + TYPE_LABELS[t] + '</option>').join('') +
    '</select></label></div>' +
    '<div><label class="field" style="margin:0"><span class="lbl">Du</span><input type="date" id="h-from" value="' + state.from + '"></label></div>' +
    '<div><label class="field" style="margin:0"><span class="lbl">Au</span><input type="date" id="h-to" value="' + state.to + '"></label></div>' +
    '</div><div class="spacer"></div><div class="row">' +
    '<button class="btn small" id="h-export">⬇️ Exporter ce registre (CSV)</button>' +
    '<button class="btn small secondary" id="h-print">🖨️ Imprimer</button>' +
    '</div></div>' +
    '<div class="card"><div class="table-wrap" id="h-table"></div></div>';

  async function refreshTable() {
    const recs = (await DB.getByTypeAndRange(state.type, state.from, state.to)).sort((a, b) => (b.date + (b.time || '')).localeCompare(a.date + (a.time || '')));
    const cols = EXPORT_COLUMNS[state.type];
    document.getElementById('h-table').innerHTML = recs.length
      ? '<table><thead><tr>' + cols.map(c => '<th>' + UI.esc(c[0]) + '</th>').join('') + '</tr></thead><tbody>' +
        recs.map(r => '<tr' + (r.conforme === false ? ' style="background:var(--red-light)"' : '') + '>' +
          cols.map(c => '<td>' + UI.esc(c[1](r) == null ? '' : c[1](r)) + '</td>').join('') + '</tr>').join('') +
        '</tbody></table>'
      : '<div class="empty">Aucun enregistrement sur cette période.</div>';
  }

  el.querySelector('#h-type').addEventListener('change', e => { state.type = e.target.value; refreshTable(); });
  el.querySelector('#h-from').addEventListener('change', e => { state.from = e.target.value; refreshTable(); });
  el.querySelector('#h-to').addEventListener('change', e => { state.to = e.target.value; refreshTable(); });
  el.querySelector('#h-print').addEventListener('click', () => window.print());
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

    '<div class="card"><h2>💾 Sauvegarde des données</h2>' +
    '<p class="muted" style="margin-bottom:12px">Les données restent sur la tablette. Exporte régulièrement une sauvegarde complète (JSON) et garde-la ailleurs (clé USB, ordinateur, mail).</p>' +
    '<div class="row"><button class="btn small" id="s-backup">⬇️ Exporter la sauvegarde</button>' +
    '<button class="btn small secondary" id="s-restore">⬆️ Restaurer une sauvegarde</button>' +
    '<input type="file" id="s-restore-file" accept="application/json" style="display:none"></div></div>';

  // Établissement
  el.querySelector('#s-etab-save').addEventListener('click', async () => {
    SETTINGS.etablissement = el.querySelector('#s-etab').value.trim() || DEFAULT_SETTINGS.etablissement;
    await saveSettings(); UI.toast('Enregistré ✔', 'ok');
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
      UI.segHTML('type', [{ value: 'positif', label: '❄️ Froid positif (0/+4)' }, { value: 'negatif', label: '🧊 Froid négatif (≤ −18)' }], 'positif') + '</label>' +
      '<div class="row"><div class="grow"><label class="field"><span class="lbl">Min (°C)</span><input type="number" step="0.5" data-f="min" value="0"></label></div>' +
      '<div class="grow"><label class="field"><span class="lbl">Max (°C)</span><input type="number" step="0.5" data-f="max" value="4"></label></div></div>' +
      '<div class="actions"><button class="btn ghost" data-x="cancel">Annuler</button><button class="btn" data-x="save">Ajouter</button></div>',
      (m, close) => {
        UI.segWire(m);
        m.querySelector('.seg[data-seg="type"]').addEventListener('click', () => setTimeout(() => {
          const neg = UI.segValue(m, 'type') === 'negatif';
          m.querySelector('[data-f="min"]').value = neg ? -30 : 0;
          m.querySelector('[data-f="max"]').value = neg ? -18 : 4;
        }, 30));
        m.querySelector('[data-x="cancel"]').onclick = close;
        m.querySelector('[data-x="save"]').onclick = async () => {
          const name = m.querySelector('[data-f="name"]').value.trim();
          const min = parseFloat(m.querySelector('[data-f="min"]').value);
          const max = parseFloat(m.querySelector('[data-f="max"]').value);
          if (!name || isNaN(min) || isNaN(max) || min >= max) { UI.toast('Vérifie le nom et les consignes', 'bad'); return; }
          SETTINGS.equipements.push({ id: uid(), name, type: UI.segValue(m, 'type'), min, max });
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
        for (const r of data.records) { delete r.id; await DB.addRecord(r); }
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
})();
