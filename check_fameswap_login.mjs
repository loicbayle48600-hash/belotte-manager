#!/usr/bin/env node
/**
 * check_fameswap_login.mjs
 *
 * Vérifie si des identifiants permettent de se connecter à https://app.fameswap.com/.
 * Les identifiants sont lus depuis un fichier texte (jamais en dur dans le code).
 *
 * Usage :
 *   node check_fameswap_login.mjs credentials.txt
 *   node check_fameswap_login.mjs credentials.txt --show     # navigateur visible
 *   node check_fameswap_login.mjs credentials.txt --debug    # capture d'écran + HTML en cas de doute
 *
 * Codes de sortie :
 *   0  -> connexion réussie
 *   1  -> connexion échouée (identifiants refusés)
 *   2  -> indéterminé (site inaccessible, Cloudflare, page inattendue...)
 *   3  -> erreur d'utilisation (fichier manquant, mauvais format...)
 *
 * Formats acceptés pour le fichier credentials.txt :
 *   1) Deux lignes :
 *        mon.email@exemple.com
 *        monMotDePasse
 *   2) Étiqueté :
 *        email = mon.email@exemple.com
 *        password = monMotDePasse
 *      (clés reconnues : email, mail, user, username, login, identifiant, pass, password, mdp, motdepasse)
 *   3) Une seule ligne :  identifiant:motdepasse
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { chromium } from 'playwright';

const LOGIN_URL = 'https://app.fameswap.com/login';
const NAV_TIMEOUT = 45000;

// ---------------------------------------------------------------------------
// 1. Lecture / analyse du fichier d'identifiants
// ---------------------------------------------------------------------------

function parseCredentials(filePath) {
  let raw;
  try {
    raw = readFileSync(filePath, 'utf8');
  } catch {
    fail(`Impossible de lire le fichier : ${filePath}`, 3);
  }

  const lines = raw
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('#'));

  if (lines.length === 0) fail('Le fichier d\'identifiants est vide.', 3);

  const ID_KEYS = ['email', 'mail', 'user', 'username', 'login', 'identifiant', 'id'];
  const PW_KEYS = ['pass', 'password', 'passwd', 'mdp', 'motdepasse', 'mot de passe'];

  let identifier;
  let password;

  // Format étiqueté : "clé = valeur" ou "clé: valeur"
  for (const line of lines) {
    const m = line.match(/^([\w .]+?)\s*[:=]\s*(.+)$/);
    if (!m) continue;
    const key = m[1].trim().toLowerCase();
    const val = m[2].trim();
    if (ID_KEYS.includes(key)) identifier = val;
    else if (PW_KEYS.includes(key)) password = val;
  }

  // Format une ligne "identifiant:motdepasse" (split sur le PREMIER ":" seulement)
  if ((!identifier || !password) && lines.length === 1 && lines[0].includes(':')) {
    const idx = lines[0].indexOf(':');
    identifier = lines[0].slice(0, idx).trim();
    password = lines[0].slice(idx + 1).trim();
  }

  // Format deux lignes brutes
  if (!identifier || !password) {
    if (lines.length >= 2) {
      identifier = identifier || lines[0];
      password = password || lines[1];
    }
  }

  if (!identifier || !password) {
    fail(
      'Format non reconnu. Attendu : 2 lignes (identifiant puis mot de passe), ' +
        'ou "email = ..." / "password = ...", ou "identifiant:motdepasse".',
      3,
    );
  }

  return { identifier, password };
}

// ---------------------------------------------------------------------------
// 2. Détection succès / échec après soumission
// ---------------------------------------------------------------------------

async function detectOutcome(page) {
  const url = page.url();
  const bodyText = ((await page.textContent('body').catch(() => '')) || '').toLowerCase();

  // Cloudflare / challenge anti-bot
  if (
    bodyText.includes('checking your browser') ||
    bodyText.includes('vérification') && bodyText.includes('cloudflare') ||
    bodyText.includes('cf-challenge') ||
    (await page.title()).toLowerCase().includes('just a moment')
  ) {
    return { status: 'unknown', reason: 'Challenge anti-bot (Cloudflare) détecté.' };
  }

  // Messages d'erreur classiques d'identifiants refusés
  const errorNeedles = [
    'invalid', 'incorrect', 'wrong', 'not match', "n'existe pas", 'introuvable',
    'identifiants invalides', 'mot de passe incorrect', 'email or password',
    'credentials do not', 'failed to log', 'échec de la connexion', 'unauthorized',
  ];
  if (errorNeedles.some((n) => bodyText.includes(n))) {
    return { status: 'fail', reason: 'Message d\'erreur d\'authentification détecté sur la page.' };
  }

  // Toujours un champ mot de passe visible => on est probablement resté sur le login
  const stillHasPassword = await page
    .locator('input[type="password"]:visible')
    .count()
    .catch(() => 0);

  // On a quitté la page de login => bon signe
  const leftLoginPage = !/\/login\b/i.test(url) && !/\/signin\b/i.test(url);

  // Indices de session ouverte
  const loggedInNeedles = ['logout', 'log out', 'déconnexion', 'sign out', 'my account', 'mon compte', 'dashboard', 'wallet', 'balance'];
  const looksLoggedIn = loggedInNeedles.some((n) => bodyText.includes(n));

  if (leftLoginPage && !stillHasPassword) {
    return { status: 'success', reason: `Redirigé hors de la page de connexion (${url}).` };
  }
  if (looksLoggedIn && !stillHasPassword) {
    return { status: 'success', reason: 'Élément de session connectée détecté (déconnexion / compte / dashboard).' };
  }
  if (stillHasPassword) {
    return { status: 'fail', reason: 'Toujours sur la page de connexion (champ mot de passe encore présent).' };
  }

  return { status: 'unknown', reason: `État indéterminé. URL actuelle : ${url}` };
}

// ---------------------------------------------------------------------------
// 3. Programme principal
// ---------------------------------------------------------------------------

function fail(msg, code) {
  console.error(`\n❌ ${msg}`);
  process.exit(code);
}

async function main() {
  const args = process.argv.slice(2);
  const flags = new Set(args.filter((a) => a.startsWith('--')));
  const positional = args.filter((a) => !a.startsWith('--'));
  const filePath = positional[0];

  if (!filePath) {
    fail('Usage : node check_fameswap_login.mjs <fichier_identifiants.txt> [--show] [--debug]', 3);
  }

  const { identifier, password } = parseCredentials(filePath);
  const masked = '*'.repeat(Math.min(password.length, 8));
  console.log(`\n🔎 Test de connexion à Fameswap`);
  console.log(`   Identifiant : ${identifier}`);
  console.log(`   Mot de passe : ${masked} (${password.length} caractères)\n`);

  // Le navigateur passe par le proxy HTTPS si l'environnement en impose un.
  const proxyServer = process.env.HTTPS_PROXY || process.env.https_proxy;
  const launchOpts = { headless: !flags.has('--show') };
  if (proxyServer) launchOpts.proxy = { server: proxyServer };

  const browser = await chromium.launch(launchOpts);
  const ctx = await browser.newContext({
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    viewport: { width: 1280, height: 800 },
    ignoreHTTPSErrors: true,
  });
  const page = await ctx.newPage();
  page.setDefaultTimeout(NAV_TIMEOUT);

  let exitCode = 2;
  try {
    const resp = await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    if (resp && resp.status() >= 400) {
      console.log(`⚠️  La page de connexion a répondu HTTP ${resp.status()}.`);
    }
    await page.waitForTimeout(3000); // laisse le SPA se charger

    // --- Localise le champ identifiant ---
    const idField = page
      .locator(
        'input[type="email"], input[name*="email" i], input[name*="user" i], ' +
          'input[name*="login" i], input[autocomplete="username"], ' +
          'input[placeholder*="mail" i], input[placeholder*="user" i]',
      )
      .first();

    const pwField = page.locator('input[type="password"]').first();

    if ((await pwField.count()) === 0) {
      console.log('⚠️  Aucun champ mot de passe trouvé sur la page.');
      const outcome = await detectOutcome(page);
      console.log(`\nRésultat : ${outcome.status.toUpperCase()} — ${outcome.reason}`);
      if (flags.has('--debug')) await dumpDebug(page);
      exitCode = outcome.status === 'success' ? 0 : 2;
      await browser.close();
      process.exit(exitCode);
    }

    // Si le champ identifiant "spécifique" est absent, on prend le 1er champ texte visible.
    let idLocator = idField;
    if ((await idField.count()) === 0) {
      idLocator = page
        .locator('input:not([type="password"]):not([type="hidden"]):not([type="checkbox"]):not([type="submit"])')
        .first();
    }

    await idLocator.fill(identifier);
    await pwField.fill(password);

    // --- Soumission : bouton explicite sinon touche Entrée ---
    const submitBtn = page
      .locator(
        'button[type="submit"], input[type="submit"], ' +
          'button:has-text("Log in"), button:has-text("Login"), ' +
          'button:has-text("Sign in"), button:has-text("Se connecter"), ' +
          'button:has-text("Connexion")',
      )
      .first();

    if ((await submitBtn.count()) > 0) {
      await Promise.all([
        page.waitForLoadState('networkidle', { timeout: NAV_TIMEOUT }).catch(() => {}),
        submitBtn.click(),
      ]);
    } else {
      await pwField.press('Enter');
      await page.waitForLoadState('networkidle', { timeout: NAV_TIMEOUT }).catch(() => {});
    }

    await page.waitForTimeout(3000); // laisse la redirection / le message d'erreur apparaître

    const outcome = await detectOutcome(page);

    if (outcome.status === 'success') {
      console.log(`\n✅ CONNEXION RÉUSSIE — ${outcome.reason}`);
      exitCode = 0;
    } else if (outcome.status === 'fail') {
      console.log(`\n❌ CONNEXION ÉCHOUÉE — ${outcome.reason}`);
      exitCode = 1;
    } else {
      console.log(`\n❔ RÉSULTAT INDÉTERMINÉ — ${outcome.reason}`);
      console.log('   Relancez avec --show pour voir le navigateur, et --debug pour une capture d\'écran.');
      exitCode = 2;
    }

    if (flags.has('--debug')) await dumpDebug(page);
  } catch (err) {
    console.log(`\n❔ RÉSULTAT INDÉTERMINÉ — erreur pendant le test : ${err.message}`);
    if (flags.has('--debug')) await dumpDebug(page).catch(() => {});
    exitCode = 2;
  } finally {
    await browser.close();
  }

  process.exit(exitCode);
}

async function dumpDebug(page) {
  try {
    await page.screenshot({ path: 'fameswap_debug.png', fullPage: true });
    writeFileSync('fameswap_debug.html', await page.content());
    console.log('   🛈 Débogage écrit : fameswap_debug.png et fameswap_debug.html');
  } catch (e) {
    console.log(`   (impossible d'écrire le débogage : ${e.message})`);
  }
}

main();
