import { DiscordSDK } from '@discord/embedded-app-sdk';

const CLIENT_ID = import.meta.env.VITE_DISCORD_CLIENT_ID;
const OPEN_EXTERNAL_MESSAGE = 'daily-guessr:open-external';
const isNested = window.parent !== window;
const sdk = !isNested && CLIENT_ID ? new DiscordSDK(CLIENT_ID) : null;
const sdkReady = sdk
  ? sdk.ready()
  : Promise.reject(new Error('VITE_DISCORD_CLIENT_ID absent'));
if (!sdk) sdkReady.catch(() => {});

async function openOutsideActivity(link) {
  const url = new URL(link.href, window.location.href).href;

  if (isNested) {
    window.parent.postMessage(
      { type: OPEN_EXTERNAL_MESSAGE, url },
      window.location.origin,
    );
    return;
  }

  // Une fenêtre créée pendant le clic conserve l'autorisation navigateur.
  // Après un `await`, Discord/Chromium peut considérer l'ouverture comme une
  // popup automatique et la bloquer.
  const popup = window.open('about:blank', '_blank');
  if (popup) {
    popup.opener = null;
    popup.location.replace(url);
    return;
  }

  link.setAttribute('aria-busy', 'true');
  try {
    await sdkReady;
    const result = await sdk.commands.openExternalLink({ url });
    if (result?.opened) return;
    throw new Error('Discord a refusé le lien externe');
  } catch (error) {
    console.warn('Ouverture externe impossible', error);
  } finally {
    link.removeAttribute('aria-busy');
  }
}

document.addEventListener('click', (event) => {
  const link = event.target.closest('[data-activity-external]');
  if (!link) return;
  if (
    event.button !== 0
    || event.metaKey
    || event.ctrlKey
    || event.shiftKey
    || event.altKey
  ) {
    return;
  }
  event.preventDefault();
  openOutsideActivity(link);
});
