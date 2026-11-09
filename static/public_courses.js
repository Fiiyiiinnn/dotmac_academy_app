
(function(){
  document.querySelectorAll('.row-wrap').forEach(function(w){
    var row=w.querySelector('.row-scroll');
    w.querySelectorAll('.row-nav button').forEach(function(b){
      b.onclick=function(){var reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;row.scrollBy({left:(row.clientWidth*0.9)*parseInt(b.dataset.dir),behavior:reduce?'auto':'smooth'});};
    });
  });
})();

window.addEventListener('error', function (e) { var t = e.target; if (t && t.hasAttribute && t.hasAttribute('data-hide-on-error')) t.style.display = 'none'; }, true);
