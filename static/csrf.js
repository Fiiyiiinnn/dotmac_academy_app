
    document.body.addEventListener('htmx:configRequest', function (evt) {
      var headers = evt.detail.headers;
      var hasCsrfHeader = Object.keys(headers).some(function (name) {
        return name.toLowerCase() === 'x-csrf-token';
      });
      if (hasCsrfHeader) { return; }

      var m = document.cookie.match(/(?:^|;\s*)__Host-csrf_token=([^;]+)/);
      if (!m) { m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/); }
      if (m) { headers['x-csrf-token'] = m[1]; }
    });

