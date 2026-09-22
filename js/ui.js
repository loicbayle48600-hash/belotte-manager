/* ===== Aides d'interface (modales, toasts, formatage) ===== */
const UI = (() => {

  function esc(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function toast(msg, kind) {
    const root = document.getElementById('toast-root');
    const el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.textContent = msg;
    root.appendChild(el);
    setTimeout(() => el.remove(), 3200);
  }

  /** Ouvre une modale ; contentHTML + câblage via setup(modalEl, close). */
  function modal(contentHTML, setup) {
    const root = document.getElementById('modal-root');
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = '<div class="modal">' + contentHTML + '</div>';
    const close = () => overlay.remove();
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    root.appendChild(overlay);
    signWire(overlay); // boutons ± des champs température, quel que soit le formulaire
    if (setup) setup(overlay.querySelector('.modal'), close);
    return close;
  }

  function confirm(message, onYes) {
    modal(
      '<h2>Confirmation</h2><p style="font-size:16px">' + esc(message) + '</p>' +
      '<div class="actions"><button class="btn ghost" data-x="no">Annuler</button>' +
      '<button class="btn danger" data-x="yes">Confirmer</button></div>',
      (m, close) => {
        m.querySelector('[data-x="no"]').onclick = close;
        m.querySelector('[data-x="yes"]').onclick = () => { close(); onYes(); };
      }
    );
  }

  // --- Dates ---
  function todayISO() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  function nowHM() {
    const d = new Date();
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  function frDate(iso) {
    if (!iso) return '';
    const [y, m, d] = iso.split('-');
    return d + '/' + m + '/' + y;
  }

  function addDays(iso, n) {
    const d = new Date(iso + 'T12:00:00');
    d.setDate(d.getDate() + n);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  function fmtTemp(v) {
    if (v === null || v === undefined || v === '') return '—';
    return (Math.round(v * 10) / 10).toLocaleString('fr-FR') + ' °C';
  }

  /** Sélecteur segmenté : boutons exclusifs, met à jour un champ caché data-value. */
  function segHTML(name, options, selected) {
    return '<div class="seg" data-seg="' + esc(name) + '">' + options.map(o =>
      '<button type="button" data-val="' + esc(o.value) + '" class="' +
      (String(o.value) === String(selected) ? 'on' + (o.bad ? ' bad' : '') : '') +
      '" data-bad="' + (o.bad ? '1' : '') + '">' + esc(o.label) + '</button>'
    ).join('') + '</div>';
  }

  function segWire(container) {
    container.querySelectorAll('.seg').forEach(seg => {
      seg.querySelectorAll('button').forEach(btn => {
        btn.addEventListener('click', () => {
          seg.querySelectorAll('button').forEach(b => b.classList.remove('on', 'bad'));
          btn.classList.add('on');
          if (btn.dataset.bad === '1') btn.classList.add('bad');
          seg.dataset.value = btn.dataset.val;
        });
      });
      const on = seg.querySelector('button.on');
      if (on) seg.dataset.value = on.dataset.val;
    });
  }

  function segValue(container, name) {
    const seg = container.querySelector('.seg[data-seg="' + name + '"]');
    return seg ? seg.dataset.value : undefined;
  }

  /** Liste déroulante des agents + option saisie libre. */
  function agentSelectHTML(agents, selected) {
    return '<label class="field"><span class="lbl">Agent</span><select data-f="agent">' +
      '<option value="">— Choisir —</option>' +
      agents.map(a => '<option value="' + esc(a) + '"' + (a === selected ? ' selected' : '') + '>' + esc(a) + '</option>').join('') +
      '</select></label>';
  }

  /** Champ température avec bouton ± (le clavier Android decimal n'a pas toujours de touche moins). */
  function tempInputHTML(field, opts) {
    const o = opts || {};
    return '<div class="temp-wrap">' +
      '<input type="number" step="0.1" inputmode="decimal" class="temp-input" data-f="' + esc(field) + '" placeholder="' + esc(o.placeholder || '—') + '">' +
      '<button type="button" class="sign-btn" data-sign="' + esc(field) + '" title="Changer le signe">±</button>' +
      '</div>' +
      (o.hint ? '<span class="muted" style="font-size:12.5px">' + esc(o.hint) + '</span>' : '');
  }

  /** Câble les boutons ± : inverse le signe et redéclenche la validation. */
  function signWire(container) {
    container.querySelectorAll('[data-sign]').forEach(btn => {
      btn.addEventListener('click', () => {
        const input = container.querySelector('input[data-f="' + btn.dataset.sign + '"]');
        if (!input) return;
        const v = parseFloat(input.value);
        if (!isNaN(v) && v !== 0) input.value = -v;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus();
      });
    });
  }

  /** Contexte audio partagé, débloqué au premier geste de l'utilisateur :
   *  en PWA navigateur, un AudioContext créé sans geste reste « suspended »
   *  et le bip serait muet (dans l'APK Capacitor, pas de restriction). */
  let _audioCtx = null;
  function _unlockAudio() {
    try {
      if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (_audioCtx.state === 'suspended') _audioCtx.resume();
    } catch { /* pas d'audio sur cet appareil */ }
  }
  document.addEventListener('pointerdown', _unlockAudio, { once: true, capture: true });

  /** Triple bip d'alerte (WebAudio, aucune dépendance) + vibration si disponible. */
  function beep() {
    try {
      _unlockAudio();
      const ctx = _audioCtx;
      if (ctx && ctx.state === 'running') {
        for (let i = 0; i < 3; i++) {
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.connect(gain); gain.connect(ctx.destination);
          osc.frequency.value = 880;
          gain.gain.setValueAtTime(0.4, ctx.currentTime + i * 0.45);
          gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.45 + 0.3);
          osc.start(ctx.currentTime + i * 0.45);
          osc.stop(ctx.currentTime + i * 0.45 + 0.32);
        }
      }
    } catch { /* audio indisponible : la vibration et le toast restent */ }
    if (navigator.vibrate) navigator.vibrate([300, 150, 300, 150, 300]);
  }

  /** Réduit une image (fichier) en dataURL JPEG max 900 px. */
  function shrinkImage(file, maxDim) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        const scale = Math.min(1, (maxDim || 900) / Math.max(img.width, img.height));
        const c = document.createElement('canvas');
        c.width = Math.round(img.width * scale);
        c.height = Math.round(img.height * scale);
        c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
        URL.revokeObjectURL(url);
        resolve(c.toDataURL('image/jpeg', 0.78));
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('image illisible')); };
      img.src = url;
    });
  }

  /** Enregistre un fichier côté utilisateur.
   *  - APK (Capacitor) : écrit dans le cache de l'appli puis ouvre la feuille de
   *    partage Android (Enregistrer dans Fichiers/Drive, envoyer par mail…) —
   *    le téléchargement direct de blob ne fonctionne pas dans une WebView.
   *  - Navigateur : téléchargement classique. */
  async function saveFile(filename, mime, content) {
    const blob = content instanceof Blob ? content : new Blob([content], { type: mime });
    const cap = window.Capacitor;
    const plugins = cap && cap.Plugins;
    if (cap && cap.isNativePlatform && cap.isNativePlatform() && plugins && plugins.Filesystem) {
      try {
        const b64 = await new Promise((res, rej) => {
          const fr = new FileReader();
          fr.onload = () => res(String(fr.result).split(',')[1]);
          fr.onerror = () => rej(fr.error || new Error('lecture impossible'));
          fr.readAsDataURL(blob);
        });
        const w = await plugins.Filesystem.writeFile({ path: filename, data: b64, directory: 'CACHE' });
        if (plugins.Share) {
          try {
            await plugins.Share.share({ title: filename, files: [w.uri] });
            return true;
          } catch { /* partage annulé par l'utilisateur : pas une erreur */ return true; }
        }
        toast('Fichier prêt : ' + filename, 'ok');
        return true;
      } catch (e) {
        toast('Impossible d’enregistrer le fichier : ' + (e.message || e), 'bad');
        return false;
      }
    }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    return true;
  }

  /** Export CSV (séparateur ; pour Excel FR) et téléchargement. */
  function downloadCSV(filename, headers, rows) {
    const escCell = v => {
      let s = String(v == null ? '' : v);
      // Neutralise l'injection de formule dans Excel/LibreOffice (=, +, -, @, tab),
      // sans toucher aux vrais nombres (températures négatives : -18).
      if (/^[=+\-@\t]/.test(s) && !/^-?\d+([.,]\d+)?$/.test(s)) s = "'" + s;
      return /[;"\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    const csv = '﻿' + [headers, ...rows].map(r => r.map(escCell).join(';')).join('\r\n');
    return saveFile(filename, 'text/csv;charset=utf-8', new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  }

  return { esc, toast, modal, confirm, todayISO, nowHM, frDate, addDays, fmtTemp, segHTML, segWire, segValue, agentSelectHTML, tempInputHTML, signWire, beep, shrinkImage, saveFile, downloadCSV };
})();
