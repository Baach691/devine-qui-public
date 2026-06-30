import './style.css';
import { DiscordSDK } from '@discord/embedded-app-sdk';

const app = document.getElementById('app');
const render = (html) => { app.innerHTML = html; };
const status = (txt) => render(`<p class="status">${escapeHtml(txt)}</p>`);

// Injecté au build par Vite depuis le .env racine (VITE_DISCORD_CLIENT_ID).
const CLIENT_ID = import.meta.env.VITE_DISCORD_CLIENT_ID;
const OPEN_EXTERNAL_MESSAGE = 'daily-guessr:open-external';

function mountDaily(url, sdk) {
  const dailyUrl = new URL(url, window.location.href);
  if (
    dailyUrl.origin !== window.location.origin
    || dailyUrl.pathname !== '/daily'
  ) {
    throw new Error('URL du Daily refusée');
  }

  const frame = document.createElement('iframe');
  frame.className = 'daily-frame';
  frame.src = `${dailyUrl.pathname}${dailyUrl.search}`;
  frame.title = 'Daily Guessr';
  frame.allow = 'autoplay; fullscreen';
  frame.allowFullscreen = true;

  window.addEventListener('message', async (event) => {
    if (
      event.origin !== window.location.origin
      || event.source !== frame.contentWindow
      || event.data?.type !== OPEN_EXTERNAL_MESSAGE
    ) {
      return;
    }
    try {
      const externalUrl = new URL(event.data.url);
      if (externalUrl.protocol !== 'https:') {
        throw new Error('Seuls les liens HTTPS sont autorisés');
      }
      await sdk.commands.openExternalLink({ url: externalUrl.href });
    } catch (error) {
      console.warn('Ouverture externe impossible', error);
    }
  });

  document.documentElement.classList.add('game-loaded');
  app.replaceChildren(frame);
}

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
    scope: ['identify', 'guilds', 'rpc.activities.write'],
  });

  // 3) Échange le code contre un access_token (côté serveur, secret protégé).
  status('Connexion…');
  const res = await fetch('/api/token', {
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

  // Les clés "1" et "2" correspondent aux images téléversées dans
  // Developer Portal > Rich Presence > Art Assets.
  try {
    await sdk.commands.setActivity({
      activity: {
        type: 0,
        details: 'Daily en cours',
        state: 'Quatre modes à compléter',
        assets: {
          large_image: '1',
          large_text: 'Daily Guessr',
          small_image: '2',
          small_text: 'Un essai par mode',
        },
        timestamps: {
          start: Math.floor(Date.now() / 1000),
        },
      },
    });
  } catch (error) {
    // La Rich Presence est décorative : son échec ne doit pas bloquer le jeu.
    console.warn('Impossible de mettre à jour la Rich Presence', error);
  }

  if (!sdk.guildId) {
    throw new Error("Lance l'Activity depuis un serveur Discord, pas depuis un message privé.");
  }

  // 5) Le backend valide le token + l'appartenance au serveur, puis crée le
  // lien Daily signé utilisé par l'interface web existante.
  status('Chargement du Daily…');
  const sessionRes = await fetch('/api/activity/session', {
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
  mountDaily(sessionPayload.url, sdk);
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
