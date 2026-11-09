document.body.addEventListener('htmx:afterSwap', function (e) {
  var target = e.detail && e.detail.target;
  var el = target && target.querySelector ? target.querySelector('[data-redirect-after]') : null;
  if (!el && target && target.hasAttribute && target.hasAttribute('data-redirect-after')) { el = target; }
  if (el) { setTimeout(function () { window.location.href = el.getAttribute('data-redirect-href'); }, parseInt(el.getAttribute('data-redirect-after'), 10)); }
});
