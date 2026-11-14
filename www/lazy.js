/*
 * Minimal lazysizes-style image loader.
 *
 *   <img class="lazyload"
 *        src="/img/300w/review.png"
 *        data-srcset="/img/600w/review.png 600w, /img/900w/review.png 900w, /img/original/review.png 3364w"
 *        data-sizes="auto"
 *        alt="Review">
 *
 * When the image nears the viewport, data-src / data-srcset / data-sizes are
 * promoted to their real attributes (on <source> siblings inside a <picture>
 * too). Classes progress lazyload -> lazyloading -> lazyloaded for styling.
 * data-sizes="auto" sizes the image to its rendered width and keeps growing it
 * on resize; it never shrinks, so a larger candidate is never swapped back out.
 */
(function () {
  'use strict';

  var cfg = Object.assign({
    lazyClass: 'lazyload',
    loadingClass: 'lazyloading',
    loadedClass: 'lazyloaded',
    rootMargin: '300px 0px',
  }, window.lazySizesConfig || {});

  var autoSized = [];

  function autoSize(el) {
    var width = el.offsetWidth || (el.parentNode && el.parentNode.offsetWidth) || 0;
    if (width <= (el._lazyWidth || 0)) return;
    el._lazyWidth = width;
    var sources = el.parentNode && el.parentNode.nodeName === 'PICTURE'
      ? el.parentNode.getElementsByTagName('source') : [];
    for (var i = 0; i < sources.length; i++) {
      if (sources[i].getAttribute('data-sizes') === 'auto') sources[i].sizes = width + 'px';
    }
    el.sizes = width + 'px';
  }

  function promote(el) {
    var sizes = el.getAttribute('data-sizes');
    if (sizes && sizes !== 'auto') el.setAttribute('sizes', sizes);
    var srcset = el.getAttribute('data-srcset');
    if (srcset) el.setAttribute('srcset', srcset);
    var src = el.getAttribute('data-src');
    if (src) el.setAttribute('src', src);
  }

  function finish(el) {
    el.classList.remove(cfg.loadingClass);
    el.classList.add(cfg.loadedClass);
  }

  function unveil(el) {
    if (!el.classList.contains(cfg.lazyClass)) return;
    el.classList.remove(cfg.lazyClass);

    if (!el.hasAttribute('data-src') && !el.hasAttribute('data-srcset')) {
      finish(el);
      return;
    }

    el.classList.add(cfg.loadingClass);
    var done = function () {
      el.removeEventListener('load', done);
      el.removeEventListener('error', done);
      finish(el);
    };
    el.addEventListener('load', done);
    el.addEventListener('error', done);

    if (el.getAttribute('data-sizes') === 'auto') {
      autoSized.push(el);
      autoSize(el);
    }

    var parent = el.parentNode;
    if (parent && parent.nodeName === 'PICTURE') {
      var sources = parent.getElementsByTagName('source');
      for (var i = 0; i < sources.length; i++) promote(sources[i]);
    }
    promote(el);
  }

  var observer = 'IntersectionObserver' in window
    ? new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          observer.unobserve(entry.target);
          unveil(entry.target);
        });
      }, { rootMargin: cfg.rootMargin })
    : null;

  function scan(root) {
    var els = (root || document).getElementsByClassName(cfg.lazyClass);
    for (var i = els.length - 1; i >= 0; i--) {
      if (observer) observer.observe(els[i]);
      else unveil(els[i]);
    }
  }

  var resizeQueued = false;
  window.addEventListener('resize', function () {
    if (resizeQueued) return;
    resizeQueued = true;
    requestAnimationFrame(function () {
      resizeQueued = false;
      autoSized.forEach(autoSize);
    });
  });

  window.lazySizes = { cfg: cfg, scan: scan, unveil: unveil };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { scan(); });
  } else {
    scan();
  }
})();
