// Mini-lecteur de fichier .env (aucune dépendance npm).
// Les variables déjà définies dans l'environnement ne sont jamais écrasées :
// en production, les secrets de l'hébergeur restent prioritaires.

import { readFileSync } from 'node:fs';

export function parseEnvFile(contents) {
  const values = {};
  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const withoutExport = line.startsWith('export ') ? line.slice(7).trim() : line;
    const separator = withoutExport.indexOf('=');
    if (separator === -1) continue;
    const key = withoutExport.slice(0, separator).trim();
    if (!key) continue;
    let value = withoutExport.slice(separator + 1).trim();
    const quote = value[0];
    if ((quote === '"' || quote === "'") && value.endsWith(quote) && value.length > 1) {
      value = value.slice(1, -1);
      if (quote === '"') value = value.replace(/\\n/g, '\n');
    } else {
      const comment = value.indexOf(' #');
      if (comment !== -1) value = value.slice(0, comment).trim();
    }
    values[key] = value;
  }
  return values;
}

export function loadEnvFile(path = '.env', env = process.env) {
  let contents;
  try {
    contents = readFileSync(path, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
  for (const [key, value] of Object.entries(parseEnvFile(contents))) {
    if (env[key] === undefined) env[key] = value;
  }
  return true;
}
