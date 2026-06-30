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
  const liveList = document.getElementById("live-list");
  const liveSummary = document.getElementById("live-summary");
  const timerEl = document.getElementById("timer");
  const contextSection = document.getElementById("context");
  const contextList = document.getElementById("context-list");
  const contextLoading = document.getElementById("context-loading");
  const contextEmpty = document.getElementById("context-empty");
  const startCard = document.getElementById("start-card");
  const startButton = document.getElementById("start-game");
  const messageCard = document.getElementById("message-card");
  const sequenceList = document.getElementById("sequence-list");
  const sequenceSubmit = document.getElementById("sequence-submit");
  let liveDetailTooltip = null;

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

  const mediaExpand = document.getElementById("media-expand");
  const mediaCard = messageCard?.classList.contains("media-card")
    ? messageCard
    : null;
  if (mediaExpand && mediaCard) {
    const setMediaExpanded = (expanded) => {
      mediaCard.classList.toggle("expanded", expanded);
      document.body.classList.toggle("media-overlay-open", expanded);
      mediaExpand.setAttribute("aria-pressed", String(expanded));
      mediaExpand.setAttribute(
        "aria-label",
        expanded ? "Réduire le média" : "Agrandir le média",
      );
      mediaExpand.title = expanded ? "Réduire le média" : "Agrandir le média";
      mediaExpand.textContent = expanded ? "×" : "⛶";
    };
    mediaExpand.addEventListener("click", () => {
      setMediaExpanded(!mediaCard.classList.contains("expanded"));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && mediaCard.classList.contains("expanded")) {
        setMediaExpanded(false);
        mediaExpand.focus();
      }
    });
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
    const tiers = [
      [50, "♾️"], [45, "🌌"], [40, "🪐"], [35, "🏆"],
      [30, "🌟"], [25, "☢️"], [20, "👑"], [15, "✨"],
      [10, "💎"], [5, "🚀"], [2, "🔥"],
    ];
    for (const [threshold, emoji] of tiers) {
      if (n >= threshold) return emoji;
    }
    return "🧊";
  }

  const hardcoreBaseMs = window.DAILY?.hardcoreBaseMs || 10000;
  const mediaHardcoreMaxMs = window.DAILY?.mediaHardcoreMaxMs || 150000;
  let hardcoreLimitMs = window.DAILY?.hardcoreMs || hardcoreBaseMs;

  function updateTimer() {
    elapsedMs = startMs == null ? 0 : Date.now() - startMs;
    if (!timerEl) return;
    const locked = timerEl.classList.contains("locked");

    // Hardcore : compte à rebours. À 0 → défaite automatique (une seule fois).
    if (difficulty === "hardcore" && !locked) {
      const remaining = Math.max(0, hardcoreLimitMs - elapsedMs);
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
  let presenceTimer = null;

  function applyRealtimeState(data) {
    if (!data || !Array.isArray(data.progress)) {
      return;
    }
    repaintLiveProgress(data.progress);
    if (
      data.unlocked
      && Array.isArray(data.results)
      && Array.isArray(data.leaderboard)
    ) {
      repaintLeaderboard(data.leaderboard);
    }
    if (statsToday) statsToday.textContent = data.participant_count ?? data.progress.length;
    if (liveSummary) {
      const activeCount = data.progress.filter((player) => player.active).length;
      liveSummary.textContent = activeCount
        ? `${activeCount} joueur${activeCount > 1 ? "s" : ""} actif${activeCount > 1 ? "s" : ""} maintenant`
        : "Progression du daily, sans dévoiler les réponses";
    }
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
        `/daily/state?t=${encodeURIComponent(token)}`,
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
      `/daily/stream?t=${encodeURIComponent(token)}`,
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
    if (presenceTimer) {
      clearInterval(presenceTimer);
      presenceTimer = null;
    }
  }

  async function heartbeatPresence() {
    if (realtimeStopped || document.hidden || !token) return;
    try {
      await fetch("/daily/presence", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
        cache: "no-store",
      });
    } catch (e) {
      /* La prochaine pulsation retentera sans interrompre le jeu. */
    }
  }

  function startPresence() {
    heartbeatPresence();
    presenceTimer = setInterval(heartbeatPresence, 15000);
  }

  document.addEventListener("visibilitychange", () => {
    if (realtimeStopped || eventSource) return;
    if (document.hidden) {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    } else {
      heartbeatPresence();
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

  applyRealtimeState(window.DAILY?.initialRealtimeState);
  startRealtime();
  startPresence();

  // --- Si déjà joué au chargement : on charge directement le contexte. -----
  if (alreadyPlayed) {
    if (!window.DAILY?.isSequence) loadContext();
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
  setupSequenceControls();

  attachOptionListeners();
  function attachOptionListeners() {
    document.querySelectorAll(".option").forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", () => submitAnswer(btn.dataset.id, btn));
    });
  }

  function setupSequenceControls() {
    if (!sequenceList || !sequenceSubmit) return;
    let dragged = null;

    const refreshPositions = () => {
      const items = [...sequenceList.querySelectorAll(".sequence-item")];
      items.forEach((item, index) => {
        const position = item.querySelector(".sequence-position");
        if (position) position.textContent = String(index + 1);
        const up = item.querySelector('[data-direction="-1"]');
        const down = item.querySelector('[data-direction="1"]');
        if (up) up.disabled = index === 0;
        if (down) down.disabled = index === items.length - 1;
      });
    };

    sequenceList.querySelectorAll(".sequence-item").forEach((item) => {
      item.addEventListener("dragstart", (event) => {
        dragged = item;
        item.classList.add("dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", item.dataset.messageId);
      });
      item.addEventListener("dragover", (event) => {
        if (!dragged || dragged === item) return;
        event.preventDefault();
        item.classList.add("drag-over");
        const rect = item.getBoundingClientRect();
        const after = event.clientY > rect.top + rect.height / 2;
        sequenceList.insertBefore(dragged, after ? item.nextSibling : item);
      });
      item.addEventListener("dragleave", () => item.classList.remove("drag-over"));
      item.addEventListener("drop", (event) => {
        event.preventDefault();
        item.classList.remove("drag-over");
        refreshPositions();
      });
      item.addEventListener("dragend", () => {
        item.classList.remove("dragging");
        sequenceList.querySelectorAll(".drag-over").forEach(
          (entry) => entry.classList.remove("drag-over"),
        );
        dragged = null;
        refreshPositions();
      });
    });

    sequenceList.querySelectorAll(".sequence-move").forEach((button) => {
      button.addEventListener("click", () => {
        const item = button.closest(".sequence-item");
        const direction = Number(button.dataset.direction);
        const sibling = direction < 0
          ? item.previousElementSibling
          : item.nextElementSibling;
        if (!sibling) return;
        if (direction < 0) {
          sequenceList.insertBefore(item, sibling);
        } else {
          sequenceList.insertBefore(sibling, item);
        }
        refreshPositions();
        button.focus();
      });
    });

    sequenceSubmit.addEventListener("click", () => {
      const guessOrder = [...sequenceList.querySelectorAll(".sequence-item")]
        .map((item) => String(item.dataset.messageId));
      submitAnswer(null, null, { guessOrder });
    });
    refreshPositions();
  }

  async function startGame() {
    if (gameStarted) return;
    gameStarted = true;

    const mediaDurationMs = difficulty === "hardcore"
      ? await getMediaDurationMs()
      : 0;
    if (
      difficulty === "hardcore"
      && window.DAILY?.isMedia
      && window.DAILY?.mediaIsVideo
    ) {
      hardcoreLimitMs = Math.min(
        hardcoreBaseMs + mediaDurationMs,
        mediaHardcoreMaxMs,
      );
    } else if (
      difficulty === "hardcore"
      && window.DAILY?.isMedia
      && window.DAILY?.mediaIsGif
    ) {
      hardcoreLimitMs = hardcoreBaseMs + 15000;
    }

    // Verrouille la difficulté côté serveur ; on respecte la valeur effective.
    try {
      const res = await fetch("/daily/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token,
          difficulty,
          media_duration_ms: mediaDurationMs,
        }),
      });
      const d = await res.json();
      if (res.ok && d.difficulty) difficulty = d.difficulty;
      if (res.ok && Number.isFinite(d.hardcore_limit_ms)) {
        hardcoreLimitMs = d.hardcore_limit_ms;
      }
    } catch (e) {
      /* en cas d'échec réseau on continue dans la difficulté locale */
    }

    startMs = Date.now();
    elapsedMs = 0;
    if (startCard) startCard.hidden = true;
    if (messageCard) messageCard.hidden = false;
    const dailyVideo = document.getElementById("daily-media");
    if (dailyVideo instanceof HTMLVideoElement) {
      dailyVideo.play().catch(() => {
        /* Certains clients Discord exigent un clic direct sur la vidéo. */
      });
    }

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

  async function getMediaDurationMs() {
    if (!window.DAILY?.isMedia || !window.DAILY?.mediaIsVideo) return 0;
    const video = document.getElementById("daily-media");
    if (!(video instanceof HTMLVideoElement)) return 0;

    const durationMs = () => (
      Number.isFinite(video.duration) && video.duration > 0
        ? Math.round(video.duration * 1000)
        : 0
    );
    if (durationMs()) return durationMs();

    video.load();
    await new Promise((resolve) => {
      const done = () => {
        clearTimeout(timeout);
        video.removeEventListener("loadedmetadata", done);
        video.removeEventListener("error", done);
        resolve();
      };
      const timeout = setTimeout(done, 8000);
      video.addEventListener("loadedmetadata", done, { once: true });
      video.addEventListener("error", done, { once: true });
    });
    return durationMs();
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
    if (sequenceSubmit) sequenceSubmit.disabled = true;
    document.querySelectorAll(".sequence-move").forEach((button) => {
      button.disabled = true;
    });
    lockHardcore();

    const guessText = opts && opts.guessText;
    const guessOrder = opts && opts.guessOrder;
    let data;
    try {
      const res = await fetch("/daily/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token,
          guessed_id: guessedId,
          guess_text: guessText,
          guess_order: guessOrder,
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
    if (window.DAILY?.isSequence) {
      revealSequence(data.correct_order || [], data.guessed_id);
    }
    paintButtons(clickedBtn, data);
    paintDistribution(data);
    animateStats(data);
    repaintLeaderboard(data.leaderboard);
    repaintLiveProgress(data.progress || []);
    startRealtime();
    showResult(data);
    setupRevealForHardcore(data);
    if (!window.DAILY?.isSequence) loadContext();
  }

  function revealSequence(correctOrder, exactScore) {
    if (!sequenceList || !Array.isArray(correctOrder)) return;
    const items = new Map(
      [...sequenceList.querySelectorAll(".sequence-item")]
        .map((item) => [String(item.dataset.messageId), item]),
    );
    const guessedItems = [...sequenceList.querySelectorAll(".sequence-item")];
    guessedItems.forEach((item, index) => {
      item.draggable = false;
      item.classList.add("revealed");
      item.querySelector(".sequence-handle")?.remove();
      item.querySelector(".sequence-controls")?.remove();
      const position = item.querySelector(".sequence-position");
      if (position) position.textContent = String(index + 1);
      const isCorrect = String(item.dataset.messageId) === String(correctOrder[index]);
      if (!item.querySelector(".sequence-result-mark")) {
        item.insertAdjacentHTML(
          "beforeend",
          `<span class="sequence-result-mark ${isCorrect ? "correct" : "wrong"}"
                 aria-label="${isCorrect ? "Position correcte" : "Position incorrecte"}">
             ${isCorrect ? "✓" : "×"}
           </span>`,
        );
      }
    });

    sequenceList.classList.remove("sequence-list-interactive");
    sequenceList.classList.add("sequence-list-guess");

    const score = Math.max(0, Math.min(5, Number(exactScore) || 0));
    const guessBlock = document.createElement("section");
    guessBlock.className = "sequence-answer-block sequence-guess-block";
    guessBlock.innerHTML = `
      <div class="sequence-answer-label">
        <h3>Ton ordre</h3>
        <strong>${score}/5</strong>
      </div>`;
    sequenceList.parentNode.insertBefore(guessBlock, sequenceList);
    guessBlock.appendChild(sequenceList);

    const correctBlock = document.createElement("section");
    correctBlock.className = "sequence-answer-block";
    correctBlock.id = "sequence-correct-block";
    correctBlock.innerHTML = `
      <div class="sequence-answer-label">
        <h3>Bon ordre</h3>
      </div>
      <ol class="sequence-list sequence-list-correct" id="sequence-correct-list"></ol>`;
    const correctList = correctBlock.querySelector(".sequence-list");
    correctOrder.forEach((messageId, index) => {
      const source = items.get(String(messageId));
      if (!source) return;
      const item = source.cloneNode(true);
      item.draggable = false;
      item.querySelector(".sequence-handle")?.remove();
      item.querySelector(".sequence-controls")?.remove();
      item.querySelector(".sequence-result-mark")?.remove();
      const position = item.querySelector(".sequence-position");
      if (position) position.textContent = String(index + 1);
      correctList.appendChild(item);
    });
    guessBlock.insertAdjacentElement("afterend", correctBlock);
    if (sequenceSubmit) sequenceSubmit.remove();
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

  // --- Panneau droit : progression des quatre modes ------------------------
  function repaintLiveProgress(players) {
    if (!liveList || !Array.isArray(players)) return;
    if (!players.length) {
      liveList.innerHTML = '<li class="live-empty">Personne n’a encore ouvert le daily.</li>';
      return;
    }

    const modes = ["author", "phrase", "media", "sequence"];
    const statusView = {
      win: { symbol: "✓", label: "Réussi" },
      fail: { symbol: "×", label: "Raté" },
      playing: { symbol: "⌛", label: "En cours" },
      complete: { symbol: "✓", label: "Terminé, résultat masqué" },
      waiting: { symbol: "—", label: "Pas commencé" },
    };
    liveList.innerHTML = players
      .map((player) => {
        const shareButton = player.can_share
          ? `<button
               type="button"
               class="live-share-button"
               title="Publier mon résultat dans le salon du daily"
               aria-label="Publier mon résultat dans le salon du daily">
               ↗
             </button>`
          : "";
        const statuses = modes
          .map((mode) => {
            const key = statusView[player.statuses?.[mode]]
              ? player.statuses[mode]
              : "waiting";
            const status = statusView[key];
            const detail = player.details?.[mode];
            let statusClass = key;
            let symbol = status.symbol;
            let label = status.label;
            if (mode === "sequence" && detail?.score !== null && detail?.score !== undefined) {
              const score = Math.max(0, Math.min(5, Number(detail.score) || 0));
              statusClass = score === 5 ? "win" : score === 0 ? "fail" : "partial";
              symbol = score === 5 ? "✓" : score === 0 ? "×" : String(score);
              label = `${score}/5 bien placé${score === 1 ? "" : "s"}`;
            }
            if (!detail) {
              return `<span class="live-status ${key}" title="${status.label}" aria-label="${status.label}">${status.symbol}</span>`;
            }
            const ariaLabel = `${label}. Temps : ${detail.time}. Réponse : ${detail.guess}`;
            return `<span
              class="live-status ${statusClass} has-details"
              tabindex="0"
              aria-label="${escapeAttr(ariaLabel)}"
              data-time="${escapeAttr(detail.time)}"
              data-guess="${escapeAttr(detail.guess)}">${symbol}</span>`;
          })
          .join("");
        return `
          <li class="live-player${player.active ? " active" : ""}${player.playing ? " playing" : ""}${player.is_me ? " me" : ""}">
            <div class="live-identity">
              <img class="live-avatar" src="${escapeAttr(player.avatar_url || "")}" alt="" loading="lazy">
              <div class="live-copy">
                <div class="live-name-row">
                  <div class="live-name">${escapeHtml(player.name)}${player.is_me ? " · Toi" : ""}</div>
                  ${shareButton}
                </div>
                <div class="live-activity">${escapeHtml(player.activity || "")}</div>
              </div>
            </div>
            ${statuses}
          </li>`;
      })
      .join("");

    liveList.querySelectorAll(".live-status.has-details").forEach((status) => {
      status.addEventListener("mouseenter", () => showLiveDetailTooltip(status));
      status.addEventListener("mouseleave", hideLiveDetailTooltip);
      status.addEventListener("focus", () => showLiveDetailTooltip(status));
      status.addEventListener("blur", hideLiveDetailTooltip);
    });
    liveList.querySelectorAll(".live-share-button").forEach((button) => {
      button.addEventListener("click", () => shareDailyResult(button));
    });
  }

  async function shareDailyResult(button) {
    if (!button || button.dataset.loading === "1") return;
    button.dataset.loading = "1";
    button.disabled = true;
    const previousLabel = button.textContent;
    button.textContent = "…";
    try {
      const response = await fetch("/daily/share", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
        cache: "no-store",
      });
      const payload = await response.json().catch(() => ({}));
      if (response.ok) {
        button.textContent = "✓";
        button.title = "Résultat publié. Cliquer pour le repartager";
        button.setAttribute("aria-label", "Repartager mon résultat");
        button.disabled = false;
        window.setTimeout(() => {
          if (!button.isConnected || button.dataset.loading === "1") return;
          button.textContent = "↗";
          button.title = "Publier mon résultat dans le salon du daily";
          button.setAttribute("aria-label", "Publier mon résultat dans le salon du daily");
        }, 1200);
        return;
      }
      const messages = {
        daily_not_complete: "Termine les quatre modes avant de partager.",
        share_channel_unavailable: "Le salon du daily est introuvable.",
        share_failed: "Discord n’a pas pu publier le résultat.",
      };
      throw new Error(messages[payload.error] || "Partage impossible.");
    } catch (error) {
      button.disabled = false;
      button.textContent = previousLabel;
      alert(error.message || "Partage impossible.");
    } finally {
      delete button.dataset.loading;
    }
  }

  function showLiveDetailTooltip(target) {
    hideLiveDetailTooltip();
    const tooltip = document.createElement("div");
    tooltip.className = "live-detail-tooltip";
    tooltip.setAttribute("role", "tooltip");
    tooltip.innerHTML = `
      <span class="tooltip-time">⏱ ${escapeHtml(target.dataset.time || "Temps inconnu")}</span>
      <span class="tooltip-guess"><strong>Réponse :</strong> ${escapeHtml(target.dataset.guess || "Inconnue")}</span>`;
    document.body.appendChild(tooltip);

    const targetRect = target.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    const left = Math.max(
      8,
      Math.min(
        window.innerWidth - tooltipRect.width - 8,
        targetRect.left + targetRect.width / 2 - tooltipRect.width / 2,
      ),
    );
    let top = targetRect.top - tooltipRect.height - 8;
    if (top < 8) top = targetRect.bottom + 8;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
    liveDetailTooltip = tooltip;
  }

  function hideLiveDetailTooltip() {
    if (liveDetailTooltip) {
      liveDetailTooltip.remove();
      liveDetailTooltip = null;
    }
  }

  // --- Panneau gauche : classement du mode courant -------------------------
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
            <span class="lb-name">${escapeHtml(e.name)}${e.played_today ? '<i class="lb-today" title="A joué aujourd’hui" aria-label="A joué aujourd’hui"></i>' : ""}</span>
            <span class="lb-pts">${e.points} pts</span>
            <span class="lb-score">${e.correct}/${e.total}</span>
            <span class="lb-streak">${streakBadge(e)}</span>
          </li>`,
      )
      .join("");
  }

  function lossEmoji(n) {
    const tiers = [
      [50, "🌑"], [45, "🫥"], [40, "🪦"], [35, "🧨"],
      [30, "🚨"], [25, "☠️"], [20, "🌋"], [15, "🕳️"],
      [10, "⚰️"], [5, "💀"], [2, "📉"],
    ];
    for (const [threshold, emoji] of tiers) {
      if (n >= threshold) return emoji;
    }
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
    statsToday.textContent = data.participant_count ?? data.stats.total;
    if (streakLabel) {
      streakLabel.textContent = `${streakEmoji(data.current_streak)} Streak`;
    }
    document.querySelectorAll(".stat-value").forEach((el) => {
      el.style.animation = "none";
      void el.offsetWidth;
      el.style.animation = "pop 0.35s ease-out";
    });
  }

  function showResult(data) {
    const existing = document.getElementById("result");
    if (existing) existing.remove();
    const section = document.createElement("section");
    section.className = "result";
    section.id = "result";
    const isPhrase = window.DAILY?.isPhrase;
    const isSequence = window.DAILY?.isSequence;
    const subject = window.DAILY?.subjectName || data.correct_name;
    const reveal = isSequence
      ? (
          data.correct
            ? "Les 5 messages sont dans le bon ordre."
            : `Tu avais <strong>${escapeHtml(data.guessed_id)}/5</strong> positions exactes. L’ordre correct est affiché au-dessus.`
        )
      : isPhrase
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
    const anchor = optionsContainer || messageCard;
    anchor.parentNode.insertBefore(
      section,
      optionsContainer || messageCard.nextSibling,
    );
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
    if (sequenceSubmit) sequenceSubmit.disabled = false;
    document.querySelectorAll(".sequence-move").forEach((button) => {
      button.disabled = false;
    });
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
