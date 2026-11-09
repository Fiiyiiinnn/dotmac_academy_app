
(function(){
  var c=document.getElementById('heroCarousel');if(!c)return;
  var slides=c.querySelectorAll('.slide'),dots=c.querySelector('.dots'),i=0,t;
  slides.forEach(function(slide,n){var b=document.createElement('button');b.type='button';
    slide.id='hero-slide-'+(n+1);
    slide.setAttribute('role','group');
    slide.setAttribute('aria-roledescription','slide');
    slide.setAttribute('aria-label',(n+1)+' of '+slides.length);
    slide.setAttribute('aria-hidden',n?'true':'false');

    b.setAttribute('aria-controls',slide.id);
    b.setAttribute('aria-label','Show slide '+(n+1));
    b.setAttribute('aria-pressed',n?'false':'true');
    if(!n)b.className='on';
    b.onclick=function(){go(n);restart();};dots.appendChild(b);});
  function go(n){slides[i].classList.remove('on');slides[i].setAttribute('aria-hidden','true');
    dots.children[i].classList.remove('on');dots.children[i].setAttribute('aria-pressed','false');
    i=n%slides.length;slides[i].classList.add('on');slides[i].setAttribute('aria-hidden','false');
    dots.children[i].classList.add('on');dots.children[i].setAttribute('aria-pressed','true');}
  function restart(){clearInterval(t);start();}
  function start(){if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;
    t=setInterval(function(){go(i+1);},5000);}
  c.addEventListener('mouseenter',function(){clearInterval(t);});
  c.addEventListener('mouseleave',restart);
  c.addEventListener('focusin',function(){clearInterval(t);});
  c.addEventListener('focusout',restart);
  start();
})();
