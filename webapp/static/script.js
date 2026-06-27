// Interactions de la page daily : timer qui tourne, clic sur une proposition
// -> POST /daily/answer avec le temps écoulé -> mise à jour des boutons, stats
// et liste des joueurs.

(function () {
  const token = window.DAILY?.token;
  const alreadyPlayed = window.DAILY?.hasAttempt;

  const buttons = document.querySelectorAll(".option");
  const optionsContainer = document.getElementById("options");
  const statsStreak = document.getElementById("streak");
  const streakLabel = document.getElementById("streak-label");
  const statsBest = document.getElementById("best");
  const statsToday = document.getElementById("today-count");
  const playerList = document.getElementById("player-list");
  const playersHeader = document.querySelector(".players h3 .count");
  const timerEl = document.getElementById("timer");
  const contextSection = document.getElementById("context");
  const contextList = document.getElementById("context-list");
  const contextLoading = document.getElementById("context-loading");
  const contextEmpty = document.getElementById("context-empty");
  const startCard = document.getElementById("start-card");
  const startButton = document.getElementById("start-game");
  const messageCard = document.getElementById("message-card");

  // Précharge le média en arrière-plan dès l'ouverture (ce n'est pas un spoil :
  // c'est la question, pas la réponse). Évite l'apparition tardive / le flash sur
  // mobile une fois la carte révélée (l'image lazy chargeait trop tard).
  if (window.DAILY?.isMedia && window.DAILY?.mediaUrl) {
    try {
      if (window.DAILY.mediaIsVideo) {
        fetch(window.DAILY.mediaUrl, { mode: "no-cors", cache: "force-cache" });
      } else {
        const im = new Image();
        im.decoding = "async";
        im.src = window.DAILY.mediaUrl;
      }
    } catch (e) {
      /* préchargement best-effort */
    }
  }

  // --- Timer ---------------------------------------------------------------
  let startMs = null;
  let elapsedMs = 0;
  let tickInterval = null;

  function formatDuration(ms) {
    // Précision ms quand < 1 min (pour le badge final + classement).
    if (ms == null || ms < 0) return "";
    if (ms < 60000) return `${(ms / 1000).toFixed(3)}s`;
    const s = Math.floor(ms / 1000);
    if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}`;
    return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
  }

  function formatLiveTimer(ms) {
    // Pendant le chrono live : centièmes de seconde (refresh rapide) pour le côté
    // dynamique. Au-delà d'une minute on passe en m/h.
    if (ms == null || ms < 0) return "0.00s";
    if (ms < 60000) return `${(ms / 1000).toFixed(2)}s`;
    const s = Math.floor(ms / 1000);
    if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}`;
    return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
  }

  function streakEmoji(n) {
    if (n >= 25) return "☢️";
    if (n >= 10) return "💎";
    if (n >= 5) return "🚀";
    if (n >= 2) return "🔥";
    return "🧊";
  }

  const hardcoreMs = window.DAILY?.hardcoreMs || 10000;

  function updateTimer() {
    elapsedMs = startMs == null ? 0 : Date.now() - startMs;
    if (!timerEl) return;
    const locked = timerEl.classList.contains("locked");

    // Hardcore : compte à rebours. À 0 → défaite automatique (une seule fois).
    if (difficulty === "hardcore" && !locked) {
      const remaining = Math.max(0, hardcoreMs - elapsedMs);
      timerEl.textContent = `⏱ ${(remaining / 1000).toFixed(2)}s`;
      timerEl.classList.toggle("danger", remaining <= 3000);
      if (remaining <= 0 && !answering) {
        handleTimeout();
      }
      return;
    }

    const display = locked ? formatDuration(elapsedMs) : formatLiveTimer(elapsedMs);
    timerEl.textContent = `⏱ ${display}`;
  }

  function handleTimeout() {
    // On soumet une réponse forcément perdante (id 0) → défaite "temps écoulé".
    submitAnswer(0, null, { timedOut: true });
  }

  // --- Tentatives + classement en temps réel ------------------------------
  let realtimeStarted = false;
  let realtimeStopped = false;
  let eventSource = null;
  let streamWatchdog = null;
  let reconnectWatchdog = null;
  let pollTimer = null;
  let pollInFlight = false;

  function applyRealtimeState(data) {
    if (!data || !Array.isArray(data.results) || !Array.isArray(data.leaderboard)) {
      return;
    }
    repaintPlayers(data.results);
    repaintLeaderboard(data.leaderboard);
    if (statsToday) statsToday.textContent = data.results.length;
  }

  function clearStreamTimers() {
    if (streamWatchdog) {
      clearTimeout(streamWatchdog);
      streamWatchdog = null;
    }
    if (reconnectWatchdog) {
      clearTimeout(reconnectWatchdog);
      reconnectWatchdog = null;
    }
  }

  function closeEventStream() {
    clearStreamTimers();
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  function schedulePoll(delay) {
    if (realtimeStopped || document.hidden || pollTimer) return;
    pollTimer = setTimeout(() => {
      pollTimer = null;
      pollRealtimeState();
    }, delay);
  }

  async function pollRealtimeState() {
    if (realtimeStopped || document.hidden || pollInFlight) return;
    pollInFlight = true;
    try {
      const res = await fetch(
        `/.proxy/daily/state?t=${encodeURIComponent(token)}`,
        { cache: "no-store" },
      );
      if (res.status === 401 || res.status === 403 || res.status === 410) {
        stopRealtime();
        return;
      }
      if (res.ok) applyRealtimeState(await res.json());
    } catch (e) {
      /* Le prochain passage retentera automatiquement. */
    } finally {
      pollInFlight = false;
      schedulePoll(3000);
    }
  }

  function switchToPolling() {
    if (realtimeStopped) return;
    closeEventStream();
    pollRealtimeState();
  }

  function startRealtime() {
    if (realtimeStarted || realtimeStopped || !token) return;
    realtimeStarted = true;

    if (!window.EventSource) {
      switchToPolling();
      return;
    }

    eventSource = new EventSource(
      `/.proxy/daily/stream?t=${encodeURIComponent(token)}`,
    );
    // Le serveur envoie un état initial immédiatement. S'il n'arrive pas, le
    // proxy Discord bufferise probablement le SSE : on passe alors au polling.
    streamWatchdog = setTimeout(switchToPolling, 8000);

    eventSource.onopen = () => {
      if (reconnectWatchdog) {
        clearTimeout(reconnectWatchdog);
        reconnectWatchdog = null;
      }
    };
    eventSource.onmessage = (event) => {
      if (streamWatchdog) {
        clearTimeout(streamWatchdog);
        streamWatchdog = null;
      }
      if (reconnectWatchdog) {
        clearTimeout(reconnectWatchdog);
        reconnectWatchdog = null;
      }
      try {
        applyRealtimeState(JSON.parse(event.data));
      } catch (e) {
        /* Un message invalide ne doit pas arrêter les mises à jour suivantes. */
      }
    };
    eventSource.onerror = () => {
      // EventSource tente d'abord sa reconnexion native. Si elle ne revient pas
      // sous dix secondes, le polling prend le relais.
      if (!reconnectWatchdog) {
        reconnectWatchdog = setTimeout(switchToPolling, 10000);
      }
    };
  }

  function stopRealtime() {
    realtimeStopped = true;
    closeEventStream();
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (realtimeStopped || eventSource) return;
    if (document.hidden) {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    } else {
      pollRealtimeState();
    }
  });
  window.addEventListener("beforeunload", stopRealtime);

  // Bouton "voir les propositions" d'un Hardcore déjà joué (rendu serveur au
  // rechargement) : les propositions sont déjà dans le DOM mais masquées.
  const serverRevealBtn = document.getElementById("reveal-options-btn");
  if (serverRevealBtn) {
    serverRevealBtn.addEventListener("click", () => revealHardcoreOptions(null));
  }

  // --- Si déjà joué au chargement : on charge directement le contexte. -----
  if (alreadyPlayed) {
    startRealtime();
    loadContext();
    return;
  }

  let gameStarted = false;
  let answering = false;
  let difficulty = window.DAILY?.lockedDifficulty || "normal";
  const hardcoreEnabled = window.DAILY?.hardcoreEnabled;

  // --- Sélection de la difficulté (libre tant qu'on n'a pas cliqué Jouer) ---
  const diffButtons = document.querySelectorAll(".diff-btn");
  function highlightDifficulty() {
    diffButtons.forEach((b) => {
      b.classList.toggle("selected", b.dataset.difficulty === difficulty);
    });
  }
  if (hardcoreEnabled) {
    highlightDifficulty();
    diffButtons.forEach((b) => {
      b.addEventListener("click", () => {
        if (gameStarted || b.disabled) return;
        difficulty = b.dataset.difficulty;
        highlightDifficulty();
      });
    });
  }

  if (startButton) {
    startButton.addEventListener("click", startGame);
  }

  attachOptionListeners();
  function attachOptionListeners() {
    document.querySelectorAll(".option").forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", () => submitAnswer(btn.dataset.id, btn));
    });
  }

  async function startGame() {
    if (gameStarted) return;
    gameStarted = true;

    // Verrouille la difficulté côté serveur ; on respecte la valeur effective.
    try {
      const res = await fetch("/daily/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, difficulty }),
      });
      const d = await res.json();
      if (res.ok && d.difficulty) difficulty = d.difficulty;
    } catch (e) {
      /* en cas d'échec réseau on continue dans la difficulté locale */
    }

    startMs = Date.now();
    elapsedMs = 0;
    if (startCard) startCard.hidden = true;
    if (messageCard) messageCard.hidden = false;

    if (difficulty === "hardcore") {
      setupHardcore();
    } else {
      // Normal : si les propositions ne sont pas déjà dans le DOM (cas anti-triche),
      // on les charge à la demande maintenant que la difficulté est verrouillée.
      if (optionsContainer && !optionsContainer.querySelector(".option")) {
        await loadOptions();
      }
      if (optionsContainer) {
        optionsContainer.hidden = false;
        optionsContainer.removeAttribute("aria-hidden");
        optionsContainer.classList.remove("prestart");
      }
    }
    if (timerEl) timerEl.hidden = false;

    updateTimer();
    // ~21 fps : assez fluide pour voir défiler les centièmes sans surcharger.
    tickInterval = setInterval(updateTimer, 47);
  }

  async function loadOptions() {
    if (!optionsContainer) return;
    let options = [];
    try {
      const res = await fetch(`/daily/options?t=${encodeURIComponent(token)}`);
      const d = await res.json();
      options = d.options || [];
    } catch (e) {
      return;
    }
    optionsContainer.innerHTML = options
      .map(
        (o) => `
          <button class="option" data-id="${o.id}">
            ${o.avatar_url ? `<img class="option-avatar${o.reveal_only ? " reveal-avatar" : ""}" src="${escapeAttr(o.avatar_url)}" alt="" loading="lazy">` : ""}
            <span class="option-label">${escapeHtml(o.label)}</span>
          </button>`,
      )
      .join("");
    attachOptionListeners();
  }

  async function submitAnswer(guessedId, clickedBtn, opts) {
    if (!gameStarted || answering) return;
    if (clickedBtn && clickedBtn.classList.contains("disabled")) return;
    answering = true;
    const timedOut = !!(opts && opts.timedOut);
    if (tickInterval) {
      clearInterval(tickInterval);
      tickInterval = null;
    }
    updateTimer(); // figer la valeur finale
    if (timerEl) {
      timerEl.classList.add("locked");
      timerEl.classList.remove("danger");
    }
    document.querySelectorAll(".option").forEach((b) => b.classList.add("disabled"));
    lockHardcore();

    const guessText = opts && opts.guessText;
    let data;
    try {
      const res = await fetch("/daily/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token,
          guessed_id: guessedId,
          guess_text: guessText,
          time_taken_ms: elapsedMs,
        }),
      });
      data = await res.json();
      if (!res.ok) {
        showError(data?.error || "Erreur inconnue");
        return;
      }
    } catch (e) {
      showError("Impossible de joindre le serveur. Recharge la page.");
      return;
    }

    if (timedOut) data.timed_out = true;
    // Mode phrase : injecte les avatars des auteurs (envoyés seulement maintenant)
    // puis révèle. Anti-spoil : ils n'étaient pas dans le DOM avant la réponse.
    if (data.option_avatars) {
      document.querySelectorAll(".option").forEach((b) => {
        const url = data.option_avatars[String(b.dataset.id)];
        if (url && !b.querySelector(".option-avatar")) {
          b.insertAdjacentHTML(
            "afterbegin",
            `<img class="option-avatar reveal-avatar" src="${escapeAttr(url)}" alt="">`,
          );
        }
      });
    }
    if (optionsContainer) optionsContainer.classList.add("revealed");
    paintButtons(clickedBtn, data);
    paintDistribution(data);
    animateStats(data);
    repaintPlayers(data.results);
    repaintLeaderboard(data.leaderboard);
    startRealtime();
    showResult(data);
    setupRevealForHardcore(data);
    loadContext();
  }

  // --- Hardcore : révéler les propositions du mode Normal (+ %) au reveal ---
  function setupRevealForHardcore(data) {
    if (data.difficulty !== "hardcore" || !optionsContainer) return;
    if (document.getElementById("reveal-options-btn")) return;
    const btn = document.createElement("button");
    btn.id = "reveal-options-btn";
    btn.type = "button";
    btn.className = "reveal-options-btn";
    btn.textContent = "👁️ Voir les propositions du mode Normal (et les %)";
    btn.addEventListener("click", () => revealHardcoreOptions(data));
    optionsContainer.parentNode.insertBefore(btn, optionsContainer);
  }

  function revealHardcoreOptions(data) {
    // data fourni (flux immédiat) → on construit les boutons ; sinon (rechargement)
    // ils sont déjà rendus côté serveur, on se contente de les afficher.
    if (data && Array.isArray(data.reveal_options) && data.reveal_options.length) {
      renderRevealOptions(data);
    }
    if (optionsContainer) {
      optionsContainer.hidden = false;
      optionsContainer.removeAttribute("aria-hidden");
      optionsContainer.classList.remove("prestart");
      optionsContainer.classList.add("revealed");
    }
    const btn = document.getElementById("reveal-options-btn");
    if (btn) btn.remove();
  }

  function renderRevealOptions(data) {
    if (!optionsContainer) return;
    const correctId = String(data.correct_id);
    const guessedId = data.guessed_id != null ? String(data.guessed_id) : null;
    optionsContainer.innerHTML = data.reveal_options
      .map((o) => {
        const id = String(o.id);
        const isCorrect = id === correctId;
        const isGuess = guessedId && id === guessedId && !isCorrect;
        const cls =
          "option disabled" + (isCorrect ? " correct" : isGuess ? " wrong" : "");
        const avatar = o.avatar_url
          ? `<img class="option-avatar" src="${escapeAttr(o.avatar_url)}" alt="" loading="lazy">`
          : "";
        const icon = isCorrect
          ? '<span class="option-icon">✅</span>'
          : isGuess
            ? '<span class="option-icon">❌</span>'
            : "";
        const pct = o.pct != null ? `<span class="option-pct">${o.pct}%</span>` : "";
        return `<button class="${cls}" data-id="${id}">${avatar}<span class="option-label">${escapeHtml(o.label)}</span>${icon}${pct}</button>`;
      })
      .join("");
  }

  // --- Mode Hardcore : saisie libre (aucune liste, on tape le nom) ---------
  const hcSection = document.getElementById("hardcore-search");
  const hcInput = document.getElementById("hc-input");
  const hcSubmit = document.getElementById("hc-submit");
  const hcSuggest = document.getElementById("hc-suggestions");
  const hcPreview = document.getElementById("hc-preview");
  let hcSearchTimer = null;
  let hcSearchSeq = 0;
  let hcResults = [];   // suggestions courantes = SOURCE DE VÉRITÉ
  let hcIndex = -1;     // membre surligné/sélectionné (envoyé à la validation)

  function setupHardcore() {
    if (!hcSection) return;
    hcSection.hidden = false;
    if (hcInput) {
      hcInput.focus();
      hcInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          sendHardcore();
        } else if (e.key === "ArrowDown") {
          e.preventDefault();
          moveSelection(1);
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          moveSelection(-1);
        } else if (e.key === "Escape") {
          hideSuggestions();
        }
      });
      // Recherche dynamique. Vide → liste complète (choix à la main). Débouncée.
      hcInput.addEventListener("input", () => {
        const q = hcInput.value.trim();
        if (hcSearchTimer) clearTimeout(hcSearchTimer);
        hcSearchTimer = setTimeout(() => runSearch(q), 110);
      });
      // Revenir sur le champ ré-affiche la liste.
      hcInput.addEventListener("focus", () => runSearch(hcInput.value.trim()));
      hcInput.addEventListener("blur", () => setTimeout(hideSuggestions, 150));
    }
    if (hcSubmit) hcSubmit.addEventListener("click", sendHardcore);
    runSearch(""); // affiche d'emblée tout le roster (on peut choisir sans taper)
  }

  async function runSearch(q) {
    if (answering) return;
    const seq = ++hcSearchSeq;
    let d;
    try {
      const res = await fetch(
        `/daily/search?t=${encodeURIComponent(token)}&q=${encodeURIComponent(q)}`,
      );
      d = await res.json();
    } catch (e) {
      return;
    }
    if (seq !== hcSearchSeq) return; // une frappe plus récente a pris le dessus
    hcResults = Array.isArray(d.results) ? d.results : [];
    // Pré-sélection du meilleur candidat SEULEMENT si on a tapé quelque chose.
    // Liste complète (saisie vide) → rien de pré-sélectionné, choix libre.
    hcIndex = (q && hcResults.length) ? 0 : -1;
    renderSuggestions();
    updatePreview();
  }

  function renderSuggestions() {
    if (!hcSuggest) return;
    if (!hcResults.length) {
      hideSuggestions();
      return;
    }
    hcSuggest.innerHTML = hcResults
      .map(
        (r, i) => `
          <li class="hc-suggestion${i === hcIndex ? " active" : ""}" role="option"
              aria-selected="${i === hcIndex}" data-i="${i}">
            <img class="hc-avatar" src="${escapeAttr(r.avatar_url || "")}" alt="" loading="lazy">
            <span class="hc-name">${escapeHtml(r.name)}</span>
            ${r.alias ? `<span class="hc-alias">« ${escapeHtml(r.alias)} »</span>` : ""}
          </li>`,
      )
      .join("");
    hcSuggest.querySelectorAll(".hc-suggestion").forEach((li) => {
      li.addEventListener("mousedown", (e) => {
        // mousedown (pas click) pour devancer le blur de l'input.
        // Clic = on SÉLECTIONNE (pas d'envoi : 1 seul essai). On valide ensuite.
        e.preventDefault();
        selectIndex(Number(li.dataset.i));
        hideSuggestions();
        if (hcInput) hcInput.focus();
      });
    });
    hcSuggest.hidden = false;
    if (hcInput) hcInput.setAttribute("aria-expanded", "true");
  }

  function selectIndex(i) {
    if (i < 0 || i >= hcResults.length) return;
    hcIndex = i;
    if (hcSuggest) {
      hcSuggest.querySelectorAll(".hc-suggestion").forEach((li, j) => {
        li.classList.toggle("active", j === i);
        li.setAttribute("aria-selected", j === i);
      });
    }
    if (hcInput) hcInput.value = hcResults[i].name; // l'input reflète la sélection
    updatePreview();
  }

  function moveSelection(delta) {
    if (!hcResults.length) return;
    if (hcSuggest && hcSuggest.hidden) hcSuggest.hidden = false;
    let i = hcIndex + delta;
    if (i < 0) i = hcResults.length - 1;
    if (i >= hcResults.length) i = 0;
    selectIndex(i);
    const el = hcSuggest && hcSuggest.querySelector(".hc-suggestion.active");
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
  }

  function updatePreview() {
    // Confirme AVANT l'envoi exactement qui sera envoyé (= le membre surligné).
    if (!hcPreview) return;
    const sel = hcIndex >= 0 ? hcResults[hcIndex] : null;
    if (sel) {
      hcPreview.innerHTML = `➡️ Tu vas envoyer : <strong>${escapeHtml(sel.name)}</strong>`;
      hcPreview.classList.remove("none");
      hcPreview.hidden = false;
    } else if (hcResults.length) {
      // Liste affichée mais rien de choisi (saisie vide) → pas de confirmation.
      clearPreview();
    } else {
      hcPreview.innerHTML = "❓ Aucune correspondance — vérifie l'orthographe.";
      hcPreview.classList.add("none");
      hcPreview.hidden = false;
    }
  }

  function clearPreview() {
    if (!hcPreview) return;
    hcPreview.hidden = true;
    hcPreview.innerHTML = "";
  }

  function hideSuggestions() {
    // On masque seulement la liste : la sélection (hcResults/hcIndex) reste valide
    // pour la validation par Entrée / bouton.
    if (!hcSuggest) return;
    hcSuggest.hidden = true;
    if (hcInput) hcInput.setAttribute("aria-expanded", "false");
  }

  function sendHardcore() {
    if (answering) return;
    // Cas normal : un membre est SÉLECTIONNÉ → on envoie son id (ce que tu vois
    // dans la confirmation = ce qui part). Plus de divergence liste/envoi.
    if (hcIndex >= 0 && hcResults[hcIndex]) {
      submitAnswer(String(hcResults[hcIndex].user_id), null, {});
      return;
    }
    // Repli : texte libre sans aucune correspondance → résolution floue serveur.
    const text = (hcInput && hcInput.value || "").trim();
    if (!text) {
      if (hcInput) hcInput.focus();
      return;
    }
    submitAnswer(null, null, { guessText: text });
  }

  function lockHardcore() {
    if (hcInput) hcInput.disabled = true;
    if (hcSubmit) hcSubmit.disabled = true;
    hideSuggestions();
    // La barre n'a d'intérêt que pendant la saisie : on la masque au reveal.
    if (hcSection) hcSection.hidden = true;
  }

  // --- Sidebar : classement live -------------------------------------------
  function repaintLeaderboard(leaderboard) {
    const list = document.getElementById("lb-list");
    if (!list || !Array.isArray(leaderboard)) return;
    if (!leaderboard.length) {
      list.innerHTML = '<li class="lb-empty">Pas encore de joueurs classés.</li>';
      return;
    }
    const medal = (rank) => (rank <= 3 ? ["🥇", "🥈", "🥉"][rank - 1] : `${rank}.`);
    const streakBadge = (e) => {
      if (e.current_streak > 0) return `${streakEmoji(e.current_streak)}+${e.current_streak}`;
      if (e.current_loss_streak > 0) return `${lossEmoji(e.current_loss_streak)}-${e.current_loss_streak}`;
      return "🧊";
    };
    list.innerHTML = leaderboard
      .map(
        (e) => `
          <li class="lb-row${e.is_me ? " me" : ""}">
            <span class="lb-rank">${medal(e.rank)}</span>
            <img class="lb-avatar" src="${escapeAttr(e.avatar_url || "")}" alt="" loading="lazy">
            <span class="lb-name">${escapeHtml(e.name)}</span>
            <span class="lb-pts">${e.points} pts</span>
            <span class="lb-score">${e.correct}/${e.total}</span>
            <span class="lb-streak">${streakBadge(e)}</span>
          </li>`,
      )
      .join("");
  }

  function lossEmoji(n) {
    if (n >= 10) return "⚰️";
    if (n >= 5) return "💀";
    if (n >= 2) return "📉";
    return "🥶";
  }

  // --- Chargement du contexte de conversation (±5 messages) ----------------
  async function loadContext() {
    if (!contextSection) return;
    contextSection.hidden = false;
    contextLoading.hidden = false;
    contextList.hidden = true;
    contextEmpty.hidden = true;
    contextSection.scrollIntoView({ behavior: "smooth", block: "nearest" });

    let data;
    try {
      const res = await fetch(`/daily/context?t=${encodeURIComponent(token)}`);
      data = await res.json();
      if (!res.ok) {
        contextLoading.hidden = true;
        contextEmpty.hidden = false;
        contextEmpty.textContent = data?.error
          ? `Impossible de charger l'historique (${data.error}).`
          : "Impossible de charger l'historique.";
        return;
      }
    } catch (e) {
      contextLoading.hidden = true;
      contextEmpty.hidden = false;
      contextEmpty.textContent = "Erreur réseau pendant le chargement de l'historique.";
      return;
    }

    renderContext(data);
  }

  function renderContext(data) {
    const before = data.before || [];
    const after = data.after || [];
    contextLoading.hidden = true;

    if (!before.length && !after.length) {
      contextEmpty.hidden = false;
      return;
    }

    const dailyHtml = `
      <li class="context-msg highlight">
        <div class="ctx-author-line">
          <span class="ctx-author">${escapeHtml(data.daily.author_name)}</span>
          <span class="ctx-tag">DAILY</span>
        </div>
        <div class="ctx-content">${escapeHtml(data.daily.content)}</div>
      </li>`;

    const lineHtml = (m) => `
      <li class="context-msg">
        <img class="ctx-avatar" src="${escapeAttr(m.avatar_url)}" alt="" loading="lazy">
        <div class="ctx-body">
          <div class="ctx-author-line">
            <span class="ctx-author">${escapeHtml(m.author_name)}</span>
            <span class="ctx-time">${formatRelativeTime(m.created_at)}</span>
          </div>
          <div class="ctx-content">${escapeHtml(m.content)}</div>
        </div>
      </li>`;

    contextList.innerHTML =
      before.map(lineHtml).join("") + dailyHtml + after.map(lineHtml).join("");
    contextList.hidden = false;
  }

  function formatRelativeTime(iso) {
    try {
      const d = new Date(iso);
      const opts = {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      };
      return d.toLocaleString("fr-FR", opts);
    } catch (e) {
      return "";
    }
  }

  function paintButtons(clicked, data) {
    // Comparaison en chaînes : les IDs Discord (snowflakes) dépassent la
    // précision des nombres JS, on ne doit jamais les convertir en Number.
    const correctId = String(data.correct_id);
    const opts = document.querySelectorAll(".option");
    opts.forEach((b) => {
      if (String(b.dataset.id) === correctId) {
        b.classList.add("correct");
      } else if (b === clicked && !data.correct) {
        b.classList.add("wrong");
      }
    });
    const correctBtn = Array.from(opts).find(
      (b) => String(b.dataset.id) === correctId,
    );
    if (correctBtn && !correctBtn.querySelector(".option-icon")) {
      correctBtn.insertAdjacentHTML("beforeend", '<span class="option-icon">✅</span>');
    }
    // En Hardcore il n'y a pas de bouton cliqué (réponse via la recherche).
    if (!data.correct && clicked && !clicked.querySelector(".option-icon")) {
      clicked.insertAdjacentHTML("beforeend", '<span class="option-icon">❌</span>');
    }
  }

  function paintDistribution(data) {
    // % de joueurs ayant choisi chaque proposition (révélé après la réponse).
    if (!data.option_stats) return;
    document.querySelectorAll(".option").forEach((b) => {
      const st = data.option_stats[String(b.dataset.id)];
      if (st && st.pct != null && !b.querySelector(".option-pct")) {
        const span = document.createElement("span");
        span.className = "option-pct";
        span.title = "Part des joueurs ayant choisi cette réponse";
        span.textContent = `${st.pct}%`;
        b.appendChild(span);
      }
    });
  }

  function animateStats(data) {
    statsStreak.textContent = data.current_streak;
    statsBest.textContent = data.best_streak;
    statsToday.textContent = data.stats.total;
    if (streakLabel) {
      streakLabel.textContent = `${streakEmoji(data.current_streak)} Streak`;
    }
    document.querySelectorAll(".stat-value").forEach((el) => {
      el.style.animation = "none";
      void el.offsetWidth;
      el.style.animation = "pop 0.35s ease-out";
    });
  }

  function repaintPlayers(results) {
    if (!playerList) return;
    // On ne révèle la liste des joueurs qu'après avoir joué (anti-spoil / moins relou).
    const playersSection = document.getElementById("players");
    if (playersSection) playersSection.hidden = false;
    if (playersHeader) {
      const correct = results.filter((r) => r.correct).length;
      playersHeader.textContent = `(${correct} ✅ / ${results.length})`;
    }
    if (!results.length) {
      playerList.innerHTML = '<li class="player empty">Personne n\'a encore tenté aujourd\'hui.</li>';
      return;
    }
    playerList.innerHTML = results
      .map(
        (r) => `
          <li class="player ${r.correct ? "win" : "lose"}">
            <img class="avatar" src="${escapeAttr(r.avatar_url)}" alt="" loading="lazy">
            <span class="name">${escapeHtml(r.user_name)}</span>
            ${r.difficulty === "hardcore" ? `<span class="diff-badge" title="Mode Hardcore">💀</span>` : ""}
            ${guessBadge(r)}
            ${r.time_taken_str ? `<span class="time-badge">${escapeHtml(r.time_taken_str)}</span>` : ""}
            <span class="status">${r.correct ? "✅" : "❌"}</span>
          </li>`,
      )
      .join("");
  }

  function guessBadge(r) {
    // Ce que le joueur a répondu (mauvaises réponses uniquement) : le nom complet
    // de la personne devinée. Site uniquement.
    if (!r.guess_label) return "";
    return `<span class="guess-badge guess-name" title="Sa réponse">${escapeHtml(r.guess_label)}</span>`;
  }

  function showResult(data) {
    const existing = document.getElementById("result");
    if (existing) existing.remove();
    const section = document.createElement("section");
    section.className = "result";
    section.id = "result";
    const isPhrase = window.DAILY?.isPhrase;
    const subject = window.DAILY?.subjectName || data.correct_name;
    const reveal = isPhrase
      ? `C'était la phrase de <strong>${escapeHtml(subject)}</strong>.`
      : `C'était <strong>${escapeHtml(data.correct_name)}</strong>.`;
    const ptsLine =
      data.correct && data.points_awarded
        ? `<p class="result-points">+${data.points_awarded} point${data.points_awarded > 1 ? "s" : ""}${
            data.difficulty === "hardcore" ? " 💀 (Hardcore)" : ""
          }</p>`
        : "";
    const heading = data.correct
      ? "✅ Bien joué !"
      : data.timed_out
        ? "⏱ Temps écoulé !"
        : "❌ Raté.";
    // Hardcore : rappelle vers qui la saisie a été reliée (utile en cas de faute).
    const guessLine =
      !data.correct && data.resolved_name
        ? `<p class="result-guess">Ta réponse : <strong>${escapeHtml(data.resolved_name)}</strong></p>`
        : "";
    section.innerHTML = `
      <h2>${heading}</h2>
      ${guessLine}
      <p>${reveal}</p>
      ${ptsLine}
    `;
    optionsContainer.parentNode.insertBefore(section, optionsContainer);
    section.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function showError(code) {
    const map = {
      already_answered: "Tu as déjà joué aujourd'hui.",
      expired_token: "Ce lien est expiré, refais /daily dans Discord.",
      invalid_token: "Lien invalide, refais /daily dans Discord.",
      no_daily: "Aucun défi du jour, refais /daily dans Discord.",
      not_allowed: "Tu n'es pas dans la liste des joueurs autorisés.",
    };
    alert(map[code] || `Erreur : ${code}`);
    answering = false;
    document.querySelectorAll(".option").forEach((b) => b.classList.remove("disabled"));
    if (hcInput) hcInput.disabled = false;
    // La réponse n'est pas passée (ex: réseau) : on ré-affiche la barre Hardcore
    // masquée par lockHardcore, pour que le joueur puisse réessayer.
    if (hcSection && difficulty === "hardcore") hcSection.hidden = false;
    if (timerEl) timerEl.classList.remove("locked");
    if (gameStarted && !tickInterval) tickInterval = setInterval(updateTimer, 47);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
  }
  function escapeAttr(s) {
    return escapeHtml(s);
  }
})();
