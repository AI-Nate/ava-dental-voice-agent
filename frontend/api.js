/* Auth gate + API client. Only allowlisted Google accounts get in; the backend enforces it too.
   Settings come from config.js (window.AVA_CONFIG). */
(function () {
  const CFG = window.AVA_CONFIG || {};
  const API = CFG.apiBase;
  const ALLOWED = (CFG.allowedEmails || []).map((e) => e.toLowerCase());
  let auth = null;

  async function firebaseConfig() {
    return CFG.firebase;
  }

  async function init(onReady, onGate) {
    try {
      if (!firebase.apps.length) firebase.initializeApp(await firebaseConfig());
    } catch (e) {
      onGate('error', 'Could not load sign-in: ' + e.message);
      return;
    }
    auth = firebase.auth();
    try { await auth.getRedirectResult(); } catch (e) { /* surfaced by the gate below */ }
    auth.onAuthStateChanged(async (user) => {
      if (!user) return onGate('signin');
      if (!ALLOWED.includes((user.email || '').toLowerCase())) return onGate('denied', user.email);
      try {
        const me = await call('GET', '/me');
        onReady(me);
      } catch (e) {
        onGate(e.status === 403 ? 'denied' : 'error', e.status === 403 ? user.email : e.message);
      }
    });
  }

  async function signIn() {
    const p = new firebase.auth.GoogleAuthProvider();
    p.setCustomParameters({ prompt: 'select_account' });
    try {
      await auth.signInWithPopup(p);
    } catch (e) {
      if (e.code === 'auth/popup-blocked' || e.code === 'auth/operation-not-supported-in-this-environment') {
        return auth.signInWithRedirect(p);
      }
      throw e;
    }
  }

  async function call(method, path, body) {
    const user = auth && auth.currentUser;
    if (!user) throw Object.assign(new Error('Not signed in'), { status: 401 });
    const token = await user.getIdToken();
    const r = await fetch(API + path, {
      method,
      headers: { Authorization: 'Bearer ' + token, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text }; }
    if (!r.ok) throw Object.assign(new Error((data && data.detail) || r.statusText), { status: r.status });
    return data;
  }

  window.VA = {
    init, signIn, call,
    signOut: () => auth.signOut(),
    user: () => auth && auth.currentUser,
  };
})();
