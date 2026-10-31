// RailBook login: OpenID Connect Authorization Code + PKCE, written by hand (no library).
//
// 1. login(): make a random secret (the PKCE verifier), send only its SHA-256 hash (challenge)
//    and go to Keycloak's login page.
// 2. Keycloak sends the browser back to "/?code=…&state=…". init() swaps that one-time code,
//    plus the verifier, for tokens. A stolen code is useless without the verifier.
// 3. The access token goes in "Authorization: Bearer …" on API calls; the API checks it.
//
// Tokens live ONLY in variables of this module (memory), never in localStorage or cookies, so a
// script injected into the page can't read them from storage. A reload loses them; init() then
// asks Keycloak again with prompt=none, which answers without a password while the user's
// Keycloak session (its own cookie on auth.localhost) is still valid.

const Auth = (() => {
  // sessionStorage (this tab only, gone when it closes) holds nothing that works as a login:
  // the verifier is useless without a code, and is deleted as soon as it's used.
  const PKCE_KEY = "railbook.pkce";
  const LOGGED_IN_KEY = "railbook.loggedIn"; // "try a silent login after a reload"; not a secret

  let config = null; // { issuer, client_id } from the API (/auth-config)
  let endpoints = null; // from Keycloak's discovery document
  let tokens = null; // { access, refresh, id, expiresAt }
  let refreshTimer = null;
  const listeners = [];

  const b64url = (bytes) =>
    btoa(String.fromCharCode(...new Uint8Array(bytes)))
      .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const randomString = () => b64url(crypto.getRandomValues(new Uint8Array(32)));
  const sha256 = (text) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  const redirectUri = () => `${location.origin}/`;

  async function loadConfig() {
    if (endpoints) return;
    config = await (await fetch("/auth-config")).json();
    // OIDC discovery: every provider publishes its endpoint URLs at this well-known path.
    const d = await (await fetch(`${config.issuer}/.well-known/openid-configuration`)).json();
    endpoints = { auth: d.authorization_endpoint, token: d.token_endpoint, logout: d.end_session_endpoint };
  }

  async function login({ silent = false } = {}) {
    await loadConfig();
    const verifier = randomString();
    // state: a random value Keycloak sends back unchanged. If it doesn't match, the response
    // wasn't for a login this tab started (protects against forged login responses, CSRF).
    const state = randomString();
    sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state, silent }));
    const params = new URLSearchParams({
      client_id: config.client_id,
      response_type: "code",
      scope: "openid",
      redirect_uri: redirectUri(),
      state,
      code_challenge: b64url(await sha256(verifier)),
      code_challenge_method: "S256",
    });
    if (silent) params.set("prompt", "none"); // never show a login page; fail instead
    location.assign(`${endpoints.auth}?${params}`);
  }

  async function tokenRequest(form) {
    const res = await fetch(endpoints.token, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ client_id: config.client_id, ...form }),
    });
    if (!res.ok) throw new Error(`Login failed (token endpoint HTTP ${res.status}).`);
    return res.json();
  }

  function store(t) {
    tokens = {
      access: t.access_token,
      refresh: t.refresh_token,
      id: t.id_token ?? tokens?.id,
      expiresAt: Date.now() + t.expires_in * 1000,
    };
    sessionStorage.setItem(LOGGED_IN_KEY, "1");
    // Renew 30 s before the access token expires (5 min in Keycloak), so calls never carry an
    // expired one. The refresh token lasts as long as the Keycloak session.
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, Math.max(5, t.expires_in - 30) * 1000);
    notify();
  }

  function clear() {
    tokens = null;
    clearTimeout(refreshTimer);
    notify();
  }

  async function refresh() {
    if (!tokens?.refresh) return false;
    try {
      store(await tokenRequest({ grant_type: "refresh_token", refresh_token: tokens.refresh }));
      return true;
    } catch {
      clear(); // session ended in Keycloak (logout, timeout, restart): logged out here too
      return false;
    }
  }

  // Once per page load: finish a login that just came back from Keycloak, or after a reload,
  // try a silent one.
  async function init() {
    const url = new URL(location.href);
    const code = url.searchParams.get("code");
    const error = url.searchParams.get("error");
    if (code || error) {
      const saved = JSON.parse(sessionStorage.getItem(PKCE_KEY) || "null");
      sessionStorage.removeItem(PKCE_KEY);
      // Remove ?code=… from the address bar and history, so it isn't bookmarked or shared.
      history.replaceState(null, "", url.pathname);
      if (!saved || url.searchParams.get("state") !== saved.state) {
        throw new Error("Login response didn't match a login started here. Please log in again.");
      }
      if (error) {
        sessionStorage.removeItem(LOGGED_IN_KEY);
        // A silent login that finds no Keycloak session: simply logged out, not an error.
        if (saved.silent) return notify();
        throw new Error(url.searchParams.get("error_description") || `Login failed: ${error}`);
      }
      await loadConfig();
      store(await tokenRequest({
        grant_type: "authorization_code",
        code,
        redirect_uri: redirectUri(),
        code_verifier: saved.verifier,
      }));
      return;
    }
    if (sessionStorage.getItem(LOGGED_IN_KEY)) return login({ silent: true });
    notify();
  }

  async function logout() {
    await loadConfig();
    const idToken = tokens?.id;
    sessionStorage.removeItem(LOGGED_IN_KEY);
    clear();
    // End the Keycloak session too; otherwise the next "Log in" would succeed without a password.
    const params = new URLSearchParams({ client_id: config.client_id, post_logout_redirect_uri: redirectUri() });
    if (idToken) params.set("id_token_hint", idToken);
    location.assign(`${endpoints.logout}?${params}`);
  }

  function user() {
    if (!tokens) return null;
    // Decoded for DISPLAY only. The browser can't verify the signature and doesn't need to:
    // the API checks every token, and decides what this user may do.
    const json = atob(tokens.access.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"));
    const claims = JSON.parse(json);
    return {
      name: claims.preferred_username,
      isAdmin: (claims.realm_access?.roles || []).includes("railbook-admin"),
    };
  }

  // Headers for an API call: the access token, renewed first if it's about to expire.
  async function headers() {
    if (tokens && Date.now() > tokens.expiresAt - 10_000) await refresh();
    return tokens ? { Authorization: `Bearer ${tokens.access}` } : {};
  }

  function onChange(fn) { listeners.push(fn); }
  function notify() { for (const fn of listeners) fn(user()); }

  return { init, login: () => login(), logout, refresh, headers, user, onChange };
})();
