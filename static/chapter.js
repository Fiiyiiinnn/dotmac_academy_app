/* Dotmac Academy — focused chapter reading experience.
   Progressive enhancement for /courses/<slug>/chapters/<n>:
   subtopic step flow, completion, sidebar state, figure lightbox, code copy,
   and a section progress bar. No dependencies. */
(function () {
  "use strict";
  var article = document.querySelector("article.prose");
  if (!article) return;

  function slugify(s) {
    return (s || "").toLowerCase().trim()
      .replace(/[^\w\s-]/g, "").replace(/\s+/g, "-").replace(/-+/g, "-").slice(0, 64) || "section";
  }

  var courseSlug = article.dataset.courseSlug || "course";
  var chapterNumber = article.dataset.chapterNumber || "chapter";
  var groupedIntroSlugs = { "why-this-matters": true, "catch-up-sidebar": true };
  var subtopicLinks = [].slice.call(document.querySelectorAll("[data-subtopic-key]")).filter(function (link) {
    return !groupedIntroSlugs[link.dataset.subtopicSlug];
  });
  var headings = [].slice.call(article.querySelectorAll("h2, h3"));
  var sections = [];
  var activeSlug = null;
  var stepActions = document.getElementById("subtopic-step-actions");
  var stepStatus = document.getElementById("subtopic-step-status");
  var completeButton = document.getElementById("subtopic-complete-button");
  var nextButton = document.getElementById("subtopic-next-button");
  var chapterTestLink = document.getElementById("chapter-test-link");
  var chapterActivityLinks = [].slice.call(document.querySelectorAll(".chapter-activity-link"));
  var sidebarContinueLink = document.getElementById("sidebar-continue-link");
  var activityTaken = article.dataset.activityTaken === "true";
  var hasActivity = article.dataset.hasActivity === "true";

  function subtopicKey(slug) { return courseSlug + ":" + chapterNumber + ":" + slug; }

  /* Completion is server state, seeded from what the page was rendered with.
     It used to live in localStorage, which made progress per-browser: moving
     from phone to laptop, or clearing site data, silently re-locked the
     chapter's activities. The set below is a cache of the server's answer, not
     the record itself. */
  var completed = Object.create(null);
  subtopicLinks.forEach(function (link) {
    if (link.dataset.subtopicDone === "true") completed[link.dataset.subtopicKey] = true;
  });

  function isComplete(key) { return completed[key] === true; }

  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)__Host-csrf_token=([^;]+)/);
    if (!m) { m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/); }
    return m ? m[1] : null;
  }

  function setComplete(key, slug) {
    /* Optimistic: the learner sees the step complete immediately. A failed
       write leaves the server unchanged, and the next page load shows the
       truth rather than a local fiction. */
    completed[key] = true;
    var token = csrfToken();
    try {
      window.fetch(
        "/courses/" + encodeURIComponent(courseSlug) + "/chapters/" +
          encodeURIComponent(chapterNumber) + "/subtopics/" + encodeURIComponent(slug) + "/complete",
        {
          method: "POST",
          credentials: "same-origin",
          headers: token ? { "x-csrf-token": token } : {},
        }
      ).catch(function () { /* offline or transient: reading should not break */ });
    } catch (e) { /* fetch unavailable: the UI still advances */ }
  }
  function targetHref(link) { return link ? (link.dataset.subtopicHref || link.getAttribute("href")) : null; }
  function nextSubtopicHref(key) {
    var idx = subtopicLinks.findIndex(function (link) { return link.dataset.subtopicKey === key; });
    return idx >= 0 && subtopicLinks[idx + 1] ? targetHref(subtopicLinks[idx + 1]) : null;
  }
  function unlockSubtopicLink(link) {
    if (!link) return;
    link.classList.add("coursework-subtopic-complete");
    link.classList.remove("coursework-subtopic-locked", "action-disabled");
    link.setAttribute("href", targetHref(link));
    link.setAttribute("aria-disabled", "false");
    link.removeAttribute("tabindex");
  }
  function paintSubtopicState(key) {
    subtopicLinks.forEach(function (link) {
      if (link.dataset.subtopicKey === key) unlockSubtopicLink(link);
    });
  }
  function currentSubtopicLinks() {
    var prefix = courseSlug + ":" + chapterNumber + ":";
    return subtopicLinks.filter(function (link) {
      return (link.dataset.subtopicKey || "").indexOf(prefix) === 0;
    });
  }
  function allCurrentSubtopicsComplete() {
    var currentLinks = currentSubtopicLinks();
    return currentLinks.length === 0 || currentLinks.every(function (link) {
      return isComplete(link.dataset.subtopicKey);
    });
  }
  function setLinkDisabled(link, disabled) {
    if (!link) return;
    link.classList.toggle("action-disabled", disabled);
    link.setAttribute("aria-disabled", disabled ? "true" : "false");
    if (disabled) link.setAttribute("tabindex", "-1");
    else link.removeAttribute("tabindex");
  }
  function updateSidebarContinue(chapterComplete) {
    if (!sidebarContinueLink) return;
    var testHref = chapterTestLink ? chapterTestLink.getAttribute("href") : null;
    var nextHref = sidebarContinueLink.dataset.nextChapterHref || null;
    if (!chapterComplete) {
      sidebarContinueLink.setAttribute("href", "#");
      sidebarContinueLink.innerHTML = 'Complete subtopics <span aria-hidden="true">&rarr;</span>';
      setLinkDisabled(sidebarContinueLink, true);
      return;
    }
    if (hasActivity && !activityTaken && testHref) {
      sidebarContinueLink.setAttribute("href", testHref);
      sidebarContinueLink.innerHTML = 'Take test <span aria-hidden="true">&rarr;</span>';
      setLinkDisabled(sidebarContinueLink, false);
      return;
    }
    if (nextHref) {
      sidebarContinueLink.setAttribute("href", nextHref);
      sidebarContinueLink.innerHTML = 'Next chapter <span aria-hidden="true">&rarr;</span>';
      setLinkDisabled(sidebarContinueLink, false);
      return;
    }
    sidebarContinueLink.setAttribute("href", "#");
    sidebarContinueLink.innerHTML = 'Chapter complete <span aria-hidden="true">✓</span>';
    setLinkDisabled(sidebarContinueLink, true);
  }
  function updateChapterGates() {
    var chapterComplete = allCurrentSubtopicsComplete();
    setLinkDisabled(chapterTestLink, !chapterComplete);
    chapterActivityLinks.forEach(function (link) { setLinkDisabled(link, !chapterComplete); });
    updateSidebarContinue(chapterComplete);
  }
  function paintAllSubtopics() {
    subtopicLinks.forEach(function (link) {
      if (isComplete(link.dataset.subtopicKey)) unlockSubtopicLink(link);
    });
    updateChapterGates();
  }
  function setActiveSidebar(slug) {
    subtopicLinks.forEach(function (link) {
      link.classList.toggle(
        "coursework-subtopic-active",
        link.dataset.subtopicKey === subtopicKey(slug)
      );
    });
  }
  function updateProgress() {
    var bar = document.getElementById("reading-progress");
    if (!bar || !sections.length) return;
    var idx = sections.findIndex(function (entry) { return entry.slug === activeSlug; });
    var pct = idx >= 0 ? ((idx + 1) / sections.length) * 100 : 0;
    bar.style.transform = "scaleX(" + (pct / 100).toFixed(3) + ")";
  }
  function updateStepActions() {
    if (!stepActions || !completeButton || !nextButton || !activeSlug) return;
    var key = subtopicKey(activeSlug);
    var done = isComplete(key);
    stepActions.hidden = false;
    if (stepStatus) stepStatus.textContent = done ? "Subtopic completed" : "Ready to continue?";
    completeButton.textContent = done ? "Completed" : "Mark as complete";
    completeButton.classList.toggle("subtopic-complete-button-done", done);
    nextButton.textContent = nextSubtopicHref(key) ? "Next" : "Finish chapter";
    nextButton.disabled = !done;
    nextButton.setAttribute("aria-disabled", done ? "false" : "true");
  }
  function showSection(slug, opts) {
    if (!sections.length) return false;
    var entry = sections.find(function (section) { return section.slug === slug; }) || sections[0];
    sections.forEach(function (section) { section.el.hidden = section.slug !== entry.slug; });
    activeSlug = entry.slug;
    setActiveSidebar(entry.slug);
    updateProgress();
    updateStepActions();
    if (!opts || opts.updateHash !== false) history.replaceState(null, "", "#" + entry.slug);
    if (!opts || opts.scroll !== false) article.scrollIntoView({ behavior: "smooth", block: "start" });
    return true;
  }
  function goToNextSubtopic(key) {
    var href = nextSubtopicHref(key);
    if (!href) return;
    if (href.charAt(0) === "#") {
      showSection(href.slice(1));
    } else {
      window.location.href = href;
    }
  }

  /* --- heading ids + focused subtopic sections ------------------------ */
  var used = {};
  headings.forEach(function (h) {
    if (!h.id) {
      var base = slugify(h.textContent);
      var id = base, i = 2;
      while (used[id] || document.getElementById(id)) { id = base + "-" + i++; }
      used[id] = true; h.id = id;
    }
    var a = document.createElement("a");
    a.href = "#" + h.id; a.className = "heading-anchor"; a.setAttribute("aria-hidden", "true");
    a.textContent = "#"; h.appendChild(a);
  });

  var stepSlugs = {};
  currentSubtopicLinks().forEach(function (link) {
    stepSlugs[link.dataset.subtopicSlug] = true;
  });
  var stepHeadings = headings.filter(function (h) { return stepSlugs[h.id]; });
  if (!stepHeadings.length) stepHeadings = headings;

  stepHeadings.forEach(function (h) {
    var section = document.createElement("section");
    section.className = "coursework-section";
    section.id = "section-" + h.id;
    section.dataset.subtopicSlug = h.id;
    article.insertBefore(section, h);
    section.appendChild(h);
    while (section.nextSibling) {
      var next = section.nextSibling;
      if (next.matches && next.matches("h2, h3") && stepSlugs[next.id]) break;
      section.appendChild(next);
    }

    sections.push({ slug: h.id, title: h.textContent.replace(/#$/, "").trim(), tagName: h.tagName, el: section });
  });

  paintAllSubtopics();

  if (completeButton) {
    completeButton.addEventListener("click", function () {
      if (!activeSlug) return;
      var key = subtopicKey(activeSlug);
      setComplete(key, activeSlug);
      paintSubtopicState(key);
      updateStepActions();
      updateChapterGates();
    });
  }
  if (nextButton) {
    nextButton.addEventListener("click", function () {
      if (!activeSlug) return;
      var key = subtopicKey(activeSlug);
      if (!isComplete(key)) return;
      goToNextSubtopic(key);
    });
  }

  document.addEventListener("click", function (e) {
    var disabledAction = e.target.closest ? e.target.closest("a.action-disabled") : null;
    if (disabledAction) {
      e.preventDefault();
      return;
    }
    var link = e.target.closest ? e.target.closest("a[data-subtopic-key]") : null;
    if (!link) return;
    if (!isComplete(link.dataset.subtopicKey)) {
      e.preventDefault();
      return;
    }
    var href = targetHref(link);
    if (!href || href.charAt(0) !== "#") return;
    var slug = href.slice(1);
    if (!slug || !sections.some(function (section) { return section.slug === slug; })) return;
    e.preventDefault();
    showSection(slug);
  });

  if (sections.length) {
    var initialSlug = window.location.hash ? window.location.hash.slice(1) : sections[0].slug;
    showSection(initialSlug, { scroll: false, updateHash: Boolean(window.location.hash) });
  }

  /* --- figure lightbox ------------------------------------------------- */
  var box = document.createElement("div");
  box.className = "lightbox"; box.setAttribute("aria-hidden", "true");
  box.innerHTML = '<img alt="">';
  document.body.appendChild(box);
  function closeBox() { box.classList.remove("open"); box.setAttribute("aria-hidden", "true"); }
  box.addEventListener("click", closeBox);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeBox(); });
  [].forEach.call(article.querySelectorAll("img"), function (img) {
    img.classList.add("zoomable");
    img.addEventListener("click", function () {
      box.querySelector("img").src = img.currentSrc || img.src;
      box.classList.add("open"); box.setAttribute("aria-hidden", "false");
    });
  });

  /* --- code copy buttons ---------------------------------------------- */
  [].forEach.call(article.querySelectorAll("pre"), function (pre) {
    var btn = document.createElement("button");
    btn.type = "button"; btn.className = "code-copy"; btn.textContent = "Copy";
    btn.addEventListener("click", function () {
      var code = pre.querySelector("code") || pre;
      navigator.clipboard.writeText(code.innerText).then(function () {
        btn.textContent = "Copied"; setTimeout(function () { btn.textContent = "Copy"; }, 1500);
      }).catch(function () { btn.textContent = "Press Ctrl+C"; });
    });
    pre.style.position = "relative";
    pre.appendChild(btn);
  });
})();
