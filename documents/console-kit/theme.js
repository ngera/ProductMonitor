/* Console Kit — theme, density and accent switching. No dependencies.
 *
 * Load it SYNCHRONOUSLY in <head>, before any stylesheet paints, or the page
 * flashes the wrong theme on every navigation:
 *
 *   <script src="/static/theme.js"></script>
 *
 * It writes both localStorage (for static pages) and a cookie (so a server can
 * stamp <html data-theme="..."> itself and skip the flash entirely).
 */
(function () {
  var KEYS = { theme: 'ck-theme', density: 'ck-density', accent: 'ck-accent' };
  var root = document.documentElement;

  function read(k) {
    try { return localStorage.getItem(k); } catch (e) { return null; }   // private mode
  }
  function write(k, v) {
    try { localStorage.setItem(k, v); } catch (e) {}
    document.cookie = k + '=' + encodeURIComponent(v) + ';path=/;max-age=31536000;samesite=lax';
  }

  // Apply before first paint. An absent value means "follow the OS" for theme
  // and "use the default" for the others — so we set no attribute at all.
  ['theme', 'density', 'accent'].forEach(function (name) {
    var v = read(KEYS[name]);
    if (v) root.setAttribute('data-' + name, v);
  });

  var api = {
    set: function (name, value) {
      if (value == null) {
        root.removeAttribute('data-' + name);
        try { localStorage.removeItem(KEYS[name]); } catch (e) {}
        document.cookie = KEYS[name] + '=;path=/;max-age=0';
      } else {
        root.setAttribute('data-' + name, value);
        write(KEYS[name], value);
      }
      root.dispatchEvent(new CustomEvent('ck:change', { detail: { name: name, value: value } }));
    },
    get: function (name) {
      if (name === 'theme' && !root.getAttribute('data-theme')) {
        return matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
      }
      return root.getAttribute('data-' + name);
    },
    toggleTheme: function () { api.set('theme', api.get('theme') === 'dark' ? 'light' : 'dark'); },
    toggleDensity: function () {
      api.set('density', api.get('density') === 'comfortable' ? 'compact' : 'comfortable');
    }
  };

  // Wire any [data-ck-toggle="theme|density"] / [data-ck-set="name:value"].
  document.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('[data-ck-toggle],[data-ck-set]');
    if (!t) return;
    e.preventDefault();
    var tog = t.getAttribute('data-ck-toggle');
    if (tog === 'theme') return api.toggleTheme();
    if (tog === 'density') return api.toggleDensity();
    var set = t.getAttribute('data-ck-set');
    if (set) { var p = set.split(':'); api.set(p[0], p[1]); }
  });

  // Follow the OS while the user has made no explicit choice.
  matchMedia('(prefers-color-scheme: light)').addEventListener('change', function () {
    if (!read(KEYS.theme)) root.dispatchEvent(new CustomEvent('ck:change', { detail: { name: 'theme' } }));
  });

  window.ck = api;
})();
