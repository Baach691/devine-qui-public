import './style.css';
import { DiscordSDK } from '@discord/embedded-app-sdk';

const app = document.getElementById('app');
const render = (html) => { app.innerHTML = html; };
const status = (txt) => render(`<p class="status">${escapeHtml(txt)}</p>`);

// Injecté au build par Vite depuis le .env racine (VITE_DISCORD_CLIENT_ID).
const CLIENT_ID = import.meta.env.VITE_DISCORD_CLIENT_ID;

async function main() {
  if (!CLIENT_ID) {
    render(`<div class="card err"><h1>Config manquante</h1>
      <p>VITE_DISCORD_CLIENT_ID absent du <code>.env</code> racine.</p></div>`);
    return;
  }

  const sdk = new DiscordSDK(CLIENT_ID);

  // 1) Handshake avec le client Discord (l'iframe est prête).
  status('En attente de Discord…');
  await sdk.ready();

  // 2) Demande un code d'autorisation OAuth2 (côté client).
  status('Autorisation…');
  const { code } = await sdk.commands.authorize({
    client_id: CLIENT_ID,
    response_type: 'code',
    state: '',
    prompt: 'none',
    scope: ['identify', 'guilds'],
  });

  // 3) Échange le code contre un access_token (côté serveur, secret protégé).
  status('Connexion…');
  const res = await fetch('/.proxy/api/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
  const tokenPayload = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(`/api/token a renvoyé ${res.status}: ${JSON.stringify(tokenPayload)}`);
  }
  const { access_token } = tokenPayload;
  if (!access_token) throw new Error('access_token manquant dans la réponse');

  // 4) Authentifie la session dans l'iframe → on obtient l'utilisateur.
  const auth = await sdk.commands.authenticate({ access_token });
  if (!auth || !auth.user) throw new Error('authenticate a échoué');

  if (!sdk.guildId) {
    throw new Error("Lance l'Activity depuis un serveur Discord, pas depuis un message privé.");
  }

  // 5) Le backend valide le token + l'appartenance au serveur, puis crée le
  // lien Daily signé utilisé par l'interface web existante.
  status('Chargement du Daily…');
  const sessionRes = await fetch('/.proxy/api/activity/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      access_token,
      guild_id: sdk.guildId,
    }),
  });
  const sessionPayload = await sessionRes.json().catch(() => ({}));
  if (!sessionRes.ok) {
    throw new Error(
      `/api/activity/session a renvoyé ${sessionRes.status}: ${JSON.stringify(sessionPayload)}`
    );
  }
  if (!sessionPayload.url) throw new Error('URL du Daily manquante');
  window.location.assign(sessionPayload.url);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

main().catch((e) => {
  console.error(e);
  render(`<div class="card err"><h1>Erreur</h1>
    <pre>${escapeHtml((e && e.message) || String(e))}</pre>
    <p class="hint">Détail dans la console (clic droit → Inspecter dans l'Activity).</p></div>`);
});
