

    // Autosave: persist each answer as it's chosen, so a dropped connection (or a
    // dead battery, or a closed tab) doesn't cost the candidate their one sitting.
    // The page re-opens with these answers pre-filled and the clock still running.
    // Fire-and-forget: a failed autosave must never interrupt the exam.
    (function () {
      var form = document.querySelector('#exam-result form');
      if (!form) return;
      var pending = null;
      function csrfToken() {
        var m = document.cookie.match(/(?:^|;\s*)__Host-csrf_token=([^;]+)/);
        if (!m) { m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/); }
        return m ? m[1] : null;
      }
      function save() {
        try {
          var body = new FormData(form);
          var token = csrfToken();
          fetch('/apply/assessment/save', {
            method: 'POST',
            body: body,
            headers: token ? { 'x-csrf-token': token } : {},
            keepalive: true          // still sent if the tab is closing
          }).catch(function () { /* offline — the next change retries */ });
        } catch (e) { /* never block the candidate */ }
      }
      form.addEventListener('change', function () {
        clearTimeout(pending);
        pending = setTimeout(save, 800);   // debounce rapid clicking
      });
      // Heartbeat while connected. The server derives elapsed time from its own
      // start timestamp; no browser-provided duration is trusted.
      setInterval(save, 10000);
      // Best-effort flush if they navigate away or lose the tab.
      window.addEventListener('pagehide', save);
    })();

    // Countdown: server-seeded remaining seconds (survives a re-open). At zero,
    // auto-submit whatever's answered — noValidate so partial answers go through.
    (function () {
      var el = document.getElementById('exam-timer');
      if (!el) return;
      var remaining = parseInt(el.getAttribute('data-remaining'), 10) || 0;
      var display = document.getElementById('timer-display');
      var form = document.querySelector('#exam-result form');
      function render() {
        var m = Math.floor(remaining / 60), s = remaining % 60;
        display.textContent = m + ':' + (s < 10 ? '0' : '') + s;
      }
      render();
      var iv = setInterval(function () {
        remaining -= 1;
        if (remaining <= 0) {
          clearInterval(iv);
          display.textContent = '0:00';
          if (form) { form.noValidate = true; form.requestSubmit(); }
          return;
        }
        render();
      }, 1000);
    })();

