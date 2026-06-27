(() => {
  const cfg = window.ADMIN_DAILY;
  const dateSelect = document.getElementById("admin-date");

  if (dateSelect) {
    dateSelect.addEventListener("change", () => {
      const url = new URL(window.location.href);
      url.searchParams.set("date", dateSelect.value);
      url.searchParams.delete("saved");
      window.location.assign(url);
    });
  }

  document.querySelectorAll(".admin-attempt").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const select = form.querySelector('select[name="guessed_id"]');
      const selected = select.options[select.selectedIndex].text.trim();
      const player = form.dataset.userName;
      if (!window.confirm(`Corriger la réponse de ${player} en « ${selected} » ?`)) {
        return;
      }

      const button = form.querySelector(".admin-save");
      button.disabled = true;
      button.textContent = "Recalcul…";
      try {
        const response = await fetch("/admin/correct", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            token: cfg.token,
            date: cfg.date,
            mode: cfg.mode,
            user_id: form.dataset.userId,
            guessed_id: select.value
          })
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.error || `Erreur ${response.status}`);
        }
        const url = new URL(window.location.href);
        url.searchParams.set("saved", payload.correct ? "win" : "loss");
        window.location.assign(url);
      } catch (error) {
        window.alert(`La correction a échoué : ${error.message}`);
        button.disabled = false;
        button.textContent = "Enregistrer";
      }
    });
  });
})();
