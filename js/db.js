/* ===== Couche de stockage (IndexedDB) =====
 * Deux magasins :
 *  - records  : tous les enregistrements HACCP { id, type, date (AAAA-MM-JJ), time, agent, ...données }
 *  - settings : configuration clé/valeur (équipements, agents, plan de nettoyage…)
 */
const DB = (() => {
  const DB_NAME = 'haccp-cuisine';
  const DB_VERSION = 1;
  let dbPromise = null;

  function open() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('records')) {
          const store = db.createObjectStore('records', { keyPath: 'id', autoIncrement: true });
          store.createIndex('type', 'type');
          store.createIndex('date', 'date');
          store.createIndex('type_date', ['type', 'date']);
        }
        if (!db.objectStoreNames.contains('settings')) {
          db.createObjectStore('settings', { keyPath: 'key' });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return dbPromise;
  }

  function tx(store, mode, fn) {
    return open().then(db => new Promise((resolve, reject) => {
      const t = db.transaction(store, mode);
      const s = t.objectStore(store);
      let result;
      try { result = fn(s); } catch (e) { reject(e); return; }
      t.oncomplete = () => resolve(result && 'result' in result ? result.result : result);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error);
    }));
  }

  return {
    /** Ajoute un enregistrement ; retourne son id. */
    addRecord(rec) {
      rec.createdAt = new Date().toISOString();
      return tx('records', 'readwrite', s => s.add(rec));
    },

    updateRecord(rec) {
      return tx('records', 'readwrite', s => s.put(rec));
    },

    deleteRecord(id) {
      return tx('records', 'readwrite', s => s.delete(id));
    },

    getRecord(id) {
      return tx('records', 'readonly', s => s.get(id));
    },

    /** Tous les enregistrements d'un type, entre deux dates incluses (AAAA-MM-JJ). */
    getByTypeAndRange(type, from, to) {
      return open().then(db => new Promise((resolve, reject) => {
        const t = db.transaction('records', 'readonly');
        const idx = t.objectStore('records').index('type_date');
        const range = IDBKeyRange.bound([type, from], [type, to]);
        const req = idx.getAll(range);
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      }));
    },

    getByType(type) {
      return open().then(db => new Promise((resolve, reject) => {
        const t = db.transaction('records', 'readonly');
        const idx = t.objectStore('records').index('type');
        const req = idx.getAll(IDBKeyRange.only(type));
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      }));
    },

    getAllRecords() {
      return open().then(db => new Promise((resolve, reject) => {
        const req = db.transaction('records', 'readonly').objectStore('records').getAll();
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      }));
    },

    /** Paramètre de configuration (retourne fallback si absent). */
    getSetting(key, fallback) {
      return tx('settings', 'readonly', s => s.get(key)).then(row => (row ? row.value : fallback));
    },

    setSetting(key, value) {
      return tx('settings', 'readwrite', s => s.put({ key, value }));
    },
  };
})();
