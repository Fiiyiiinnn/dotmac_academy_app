
    (function () {
      var toggle = document.querySelector('[data-sidebar-toggle]');
      var sidebar = document.getElementById('academy-sidebar');
      var dismissers = document.querySelectorAll('[data-sidebar-dismiss]');
      var lastFocused = null;
      if (!toggle || !sidebar) return;

      function focusableItems() {
        return Array.prototype.slice.call(sidebar.querySelectorAll(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        ));
      }

      function setSidebar(open, restoreFocus) {
        var mobile = window.innerWidth < 768;
        var shouldOpen = mobile && open;
        document.body.classList.toggle('sidebar-open', shouldOpen);
        toggle.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');

        if (!mobile) {
          sidebar.removeAttribute('inert');
          sidebar.removeAttribute('aria-hidden');
          lastFocused = null;
          return;
        }

        sidebar.toggleAttribute('inert', !shouldOpen);
        sidebar.setAttribute('aria-hidden', shouldOpen ? 'false' : 'true');
        if (shouldOpen) {
          lastFocused = document.activeElement;
          var items = focusableItems();
          if (items.length) items[0].focus();
        } else if (restoreFocus && lastFocused) {
          toggle.focus();
          lastFocused = null;
        }
      }

      toggle.addEventListener('click', function () {
        setSidebar(!document.body.classList.contains('sidebar-open'), true);
      });
      dismissers.forEach(function (item) {
        item.addEventListener('click', function () { setSidebar(false, true); });
      });
      document.addEventListener('keydown', function (event) {
        if (!document.body.classList.contains('sidebar-open')) return;
        if (event.key === 'Escape') {
          event.preventDefault();
          setSidebar(false, true);
          return;
        }
        if (event.key !== 'Tab') return;
        var items = focusableItems();
        if (!items.length) return;
        var first = items[0];
        var last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      });
      window.addEventListener('resize', function () { setSidebar(false, false); });
      setSidebar(false, false);
    }());
    (function () {
      var academySearch = document.querySelector('.shell-search input[type="search"]');
      if (!academySearch) return;
      document.addEventListener('keydown', function (event) {
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
          event.preventDefault();
          academySearch.focus();
          academySearch.select();
        }
      });
    }());
    (function () {
      var liveSelector = '[id*="result"], [id$="-flash"], [id*="-error"]';
      function prepareRegion(region) {
        if (!region.hasAttribute('role')) region.setAttribute('role', 'status');
        if (!region.hasAttribute('aria-live')) region.setAttribute('aria-live', 'polite');
        region.setAttribute('aria-atomic', 'true');
      }
      function prepareLiveRegions(root) {
        if (root.matches && root.matches(liveSelector)) prepareRegion(root);
        var regions = root.querySelectorAll ? root.querySelectorAll(liveSelector) : [];
        regions.forEach(prepareRegion);
      }
      prepareLiveRegions(document);
      document.body.addEventListener('htmx:beforeRequest', function (event) {
        event.detail.elt.setAttribute('aria-busy', 'true');
      });
      document.body.addEventListener('htmx:afterRequest', function (event) {
        event.detail.elt.removeAttribute('aria-busy');
      });
      document.body.addEventListener('htmx:afterSwap', function (event) {
        prepareLiveRegions(event.detail.target || document);
      });
    }());
  
window.addEventListener('error', function (e) { var t = e.target; if (t && t.hasAttribute && t.hasAttribute('data-hide-on-error')) t.style.display = 'none'; }, true);
document.addEventListener('change', function (e) { if (e.target.hasAttribute('data-submit-on-change') && e.target.form) e.target.form.submit(); });
document.addEventListener('submit', function (e) { var f = e.target; if (f.hasAttribute('data-version-action-form')) f.action = f.action.replace('__VERSION__', f.version_id.value); });
document.addEventListener('submit', function (e) { var f = e.target; if (f.hasAttribute('data-confirm-submit') && !confirm(f.getAttribute('data-confirm-submit'))) e.preventDefault(); });
