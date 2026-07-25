(() => {
  "use strict";

  const brandName = document.body.dataset.brand || "Anime Index";

  // ---------------------------------------------------------------------
  // Telegram WebApp bootstrap (no-ops gracefully outside Telegram)
  // ---------------------------------------------------------------------
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    try {
      tg.ready();
      tg.expand();
      tg.setHeaderColor && tg.setHeaderColor("#0a0a12");
      tg.setBackgroundColor && tg.setBackgroundColor("#0a0a12");
    } catch (e) { /* not fatal */ }
  }
  const initData = tg ? tg.initData : "";

  function authHeaders() {
    return initData ? { "X-Telegram-Init-Data": initData } : {};
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
        ...(options.headers || {}),
      },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `Request failed (${res.status})`);
    }
    return res.json();
  }

  function debounce(fn, ms) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  // ---------------------------------------------------------------------
  // Generated placeholder thumbnail — used whenever an image is missing
  // or fails to load, so nothing ever shows a broken image icon.
  // ---------------------------------------------------------------------
  function hashStr(str) {
    let h = 0;
    for (let i = 0; i < (str || "").length; i++) {
      h = str.charCodeAt(i) + ((h << 5) - h);
      h |= 0;
    }
    return Math.abs(h);
  }

  const PALETTES = [
    ["#3a0ca3", "#f72585"], ["#7209b7", "#4361ee"], ["#ff6b35", "#9d0208"],
    ["#0b132b", "#5bc0be"], ["#22223b", "#c9184a"], ["#231942", "#e0b1cb"],
    ["#03045e", "#00b4d8"], ["#590d22", "#ff8fa3"], ["#1b4332", "#95d5b2"],
    ["#3d0000", "#ff6d00"], ["#14213d", "#fca311"], ["#240046", "#5a189a"],
  ];

  function generatedThumb(title) {
    const h = hashStr(title);
    const [c1, c2] = PALETTES[h % PALETTES.length];
    const angle = 110 + (h % 140);
    const div = document.createElement("div");
    div.className = "generated-thumb";
    div.style.background = `linear-gradient(${angle}deg, ${c1}, ${c2})`;
    return div;
  }

  function thumbImg(container, src, title) {
    if (!src) {
      container.appendChild(generatedThumb(title));
      return;
    }
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = src;
    img.alt = title;
    img.onerror = () => img.replaceWith(generatedThumb(title));
    container.appendChild(img);
  }

  // ---------------------------------------------------------------------
  // Elements
  // ---------------------------------------------------------------------
  const el = (id) => document.getElementById(id);

  const appView = el("app-view");
  const searchView = el("search-view");
  const genreView = el("genre-view");
  const notificationsView = el("notifications-view");
  const profileView = el("profile-view");
  const allViews = { app: appView, search: searchView, genre: genreView, notifications: notificationsView, profile: profileView };

  const homeSearchInput = el("search-input");
  const searchViewInput = el("search-view-input");
  const searchResults = el("search-results");
  const searchResultsGroups = el("search-results-groups");
  const searchResultsEmpty = el("search-results-empty");
  const searchLanding = el("search-landing");
  const popularSearchList = el("popular-search-list");
  const popularSearchClear = el("popular-search-clear");
  const genreTileGrid = el("genre-tile-grid");
  const genreBrowseGrid = el("genre-browse-grid");
  const genreViewTitle = el("genre-view-title");
  const genreChipRow = el("genre-chip-row");

  const pillTabs = document.querySelectorAll(".pill-tab");
  const tabAll = el("tab-all");
  const tabLibrary = el("tab-library");

  const featuredSection = el("featured-section");
  const featuredCarousel = el("featured-carousel");
  const featuredDots = el("featured-dots");
  const trendingRow = el("trending-row");
  const topAiringList = el("top-airing-list");
  const popularLoadMore = el("popular-load-more");

  const letterBar = el("letter-bar");
  const availableGroups = el("available-groups");
  const availableEmpty = el("available-empty");
  const adSlot = el("ad-slot");

  const navBtns = document.querySelectorAll(".nav-btn");
  const notifBadge = el("notif-badge");
  const notificationsList = el("notifications-list");
  const notificationsEmpty = el("notifications-empty");

  const detailOverlay = el("detail-overlay");
  const detailPoster = el("detail-poster");
  const detailThumb = el("detail-thumb");
  const detailTitle = el("detail-title");
  const detailSubtitle = el("detail-subtitle");
  const detailMetaPills = el("detail-meta-pills");
  const detailGenres = el("detail-genres");
  const detailDescription = el("detail-description");
  const detailReadMore = el("detail-readmore");
  const detailActionArea = el("detail-action-area");
  const reportOpenBtn = el("report-open-btn");

  const linkOverlay = el("link-overlay");
  const linkInput = el("link-input");

  const reportOverlay = el("report-overlay");
  const reportDetails = el("report-details");
  let selectedReason = null;

  const profileCard = el("profile-card");

  const toast = el("toast");
  let toastTimer = null;

  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.add("hidden"), 2200);
  }

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------
  let trending = [];
  let popular = [];
  let popularPage = 1;
  let popularHasNext = false;
  let newsItems = [];
  let available = [];
  let activeAd = null;
  let activeLetter = null;
  let libraryQuery = "";
  let profile = null;
  let featuredIndex = 0;
  let featuredTimer = null;

  const ALL_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("");

  function availableByTitle() {
    const map = new Map();
    available.forEach((a) => map.set(a.title.toLowerCase(), a));
    return map;
  }

  // ---------------------------------------------------------------------
  // Top-level navigation (Home / Search / Notifications / Profile)
  // ---------------------------------------------------------------------
  function showView(name) {
    Object.entries(allViews).forEach(([key, node]) => node.classList.toggle("hidden", key !== name));
    navBtns.forEach((b) => b.classList.toggle("active", b.dataset.nav === (name === "app" ? "home" : name)));
    if (featuredTimer) { clearInterval(featuredTimer); featuredTimer = null; }
    if (name === "app") startFeaturedAutoplay();
  }

  navBtns.forEach((btn) => btn.addEventListener("click", () => {
    const target = btn.dataset.nav;
    if (target === "home") showView("app");
    else if (target === "search") { showView("search"); renderSearchLanding(); }
    else if (target === "notifications") { showView("notifications"); loadNotifications(); }
    else if (target === "profile") { showView("profile"); openProfile(); }
  }));

  document.querySelectorAll("[data-back]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.back;
      if (target === "home") showView("app");
      else if (target === "search") showView("search");
    });
  });

  // Home's own search field is a shortcut into the dedicated Search page.
  homeSearchInput.addEventListener("click", () => {
    showView("search");
    renderSearchLanding();
    setTimeout(() => searchViewInput.focus(), 50);
  });

  // ---------------------------------------------------------------------
  // Poster / card builders
  // ---------------------------------------------------------------------
  function simplePosterCard(item, onOpen) {
    const card = document.createElement("div");
    card.className = "poster-card";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = item.poster_url || "";
    img.alt = item.title;
    card.appendChild(img);
    card.addEventListener("click", onOpen);
    return card;
  }

  function trendingCard(item, onOpen) {
    const card = document.createElement("div");
    card.className = "poster-card";

    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = item.poster_url || "";
    img.alt = item.title;
    card.appendChild(img);

    const hot = document.createElement("span");
    hot.className = "hot-badge";
    hot.textContent = "HOT";
    card.appendChild(hot);

    if (item.rating) {
      const rating = document.createElement("span");
      rating.className = "poster-rating";
      rating.textContent = "\u2605 " + item.rating.toFixed(1);
      card.appendChild(rating);
    }

    const meta = document.createElement("div");
    meta.className = "poster-meta";
    const title = document.createElement("p");
    title.className = "poster-title";
    title.textContent = item.title;
    meta.appendChild(title);
    if (item.genres && item.genres.length) {
      const genres = document.createElement("p");
      genres.className = "poster-genres";
      genres.textContent = item.genres.join(", ");
      meta.appendChild(genres);
    }
    if (item.episodes) {
      const eps = document.createElement("p");
      eps.className = "poster-episodes";
      eps.textContent = `${item.episodes} Episodes`;
      meta.appendChild(eps);
    }
    card.appendChild(meta);

    card.addEventListener("click", onOpen);
    return card;
  }

  function airingCard(item, rank, onOpen) {
    const card = document.createElement("div");
    card.className = "airing-card";

    const rankBox = document.createElement("div");
    rankBox.className = "airing-rank";
    thumbImg(rankBox, item.poster_url, item.title);
    const rankNum = document.createElement("div");
    rankNum.className = "airing-rank-number";
    rankNum.textContent = String(rank).padStart(2, "0");
    rankBox.appendChild(rankNum);
    card.appendChild(rankBox);

    const body = document.createElement("div");
    body.className = "airing-body";

    const topRow = document.createElement("div");
    topRow.className = "airing-top-row";
    const left = document.createElement("div");
    const badge = document.createElement("span");
    badge.className = "airing-new-ep";
    badge.textContent = "NEW EP";
    left.appendChild(badge);
    const title = document.createElement("p");
    title.className = "airing-title";
    title.textContent = item.title;
    left.appendChild(title);
    topRow.appendChild(left);
    if (item.rating) {
      const rating = document.createElement("div");
      rating.className = "airing-rating";
      rating.textContent = "\u2605 " + item.rating.toFixed(1);
      topRow.appendChild(rating);
    }
    body.appendChild(topRow);

    if (item.genres && item.genres.length) {
      const genreRow = document.createElement("div");
      genreRow.className = "airing-genres";
      item.genres.forEach((g) => {
        const pill = document.createElement("span");
        pill.className = "airing-genre-pill";
        pill.textContent = g;
        genreRow.appendChild(pill);
      });
      body.appendChild(genreRow);
    }

    if (item.synopsis) {
      const syn = document.createElement("p");
      syn.className = "airing-synopsis";
      syn.textContent = item.synopsis;
      body.appendChild(syn);
    }

    card.appendChild(body);

    const arrow = document.createElement("span");
    arrow.className = "airing-arrow";
    arrow.textContent = "\u2197";
    card.appendChild(arrow);

    card.addEventListener("click", onOpen);
    return card;
  }

  function matchesLibraryQuery(title) {
    return !libraryQuery || title.toLowerCase().includes(libraryQuery.toLowerCase());
  }

  // ---------------------------------------------------------------------
  // Featured carousel (Anime News items, styled as a promo banner)
  // ---------------------------------------------------------------------
  function renderFeatured() {
    featuredCarousel.innerHTML = "";
    featuredDots.innerHTML = "";
    if (!newsItems.length) {
      featuredSection.classList.add("hidden");
      return;
    }
    featuredSection.classList.remove("hidden");
    const item = newsItems[featuredIndex % newsItems.length];

    const card = document.createElement("div");
    card.className = "featured-card";
    thumbImg(card, item.image, item.title);

    const badge = document.createElement("span");
    badge.className = "featured-badge";
    badge.textContent = "FEATURED";
    card.appendChild(badge);

    const content = document.createElement("div");
    content.className = "featured-content";
    const title = document.createElement("p");
    title.className = "featured-title";
    title.textContent = item.title;
    content.appendChild(title);
    const desc = document.createElement("p");
    desc.className = "featured-desc";
    desc.textContent = item.summary || "";
    content.appendChild(desc);

    const actions = document.createElement("div");
    actions.className = "featured-actions";
    const watchBtn = document.createElement("button");
    watchBtn.className = "watch-now-btn";
    watchBtn.textContent = "\u25b6 Watch Now";
    watchBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (tg && tg.openLink) tg.openLink(item.link);
      else window.open(item.link, "_blank");
    });
    actions.appendChild(watchBtn);
    const plusBtn = document.createElement("button");
    plusBtn.className = "featured-plus-btn";
    plusBtn.textContent = "+";
    plusBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      openNewsArticleDetail(item);
    });
    actions.appendChild(plusBtn);
    content.appendChild(actions);
    card.appendChild(content);

    card.addEventListener("click", () => openNewsArticleDetail(item));
    featuredCarousel.appendChild(card);

    newsItems.forEach((_, i) => {
      const dot = document.createElement("button");
      dot.className = "dot" + (i === featuredIndex % newsItems.length ? " active" : "");
      dot.addEventListener("click", () => { featuredIndex = i; renderFeatured(); resetFeaturedAutoplay(); });
      featuredDots.appendChild(dot);
    });
  }

  function startFeaturedAutoplay() {
    if (featuredTimer || newsItems.length < 2) return;
    featuredTimer = setInterval(() => {
      featuredIndex = (featuredIndex + 1) % newsItems.length;
      renderFeatured();
    }, 6000);
  }
  function resetFeaturedAutoplay() {
    if (featuredTimer) { clearInterval(featuredTimer); featuredTimer = null; }
    startFeaturedAutoplay();
  }

  // ---------------------------------------------------------------------
  // Render: Home "All" tab — Trending, Top Airing (+ Load more), Genres
  // ---------------------------------------------------------------------
  function renderTrending() {
    trendingRow.innerHTML = "";
    const byTitle = availableByTitle();
    trending.forEach((item) => {
      const matched = byTitle.get(item.title.toLowerCase());
      item.matchedJoinLink = matched && matched.join_link ? matched.join_link : null;
      trendingRow.appendChild(trendingCard(item, () => openDiscoverDetail(item)));
    });
  }

  function renderTopAiring() {
    topAiringList.innerHTML = "";
    const byTitle = availableByTitle();
    popular.forEach((item, i) => {
      const matched = byTitle.get(item.title.toLowerCase());
      item.matchedJoinLink = matched && matched.join_link ? matched.join_link : null;
      topAiringList.appendChild(airingCard(item, i + 1, () => openDiscoverDetail(item)));
    });
    popularLoadMore.classList.toggle("hidden", !popularHasNext);
  }

  popularLoadMore.addEventListener("click", async () => {
    popularLoadMore.textContent = "Loading...";
    popularLoadMore.disabled = true;
    try {
      const data = await api(`/api/catalog/popular?page=${popularPage + 1}`);
      popularPage += 1;
      popular = popular.concat(data.results);
      popularHasNext = data.has_next;
      renderTopAiring();
    } catch (err) {
      showToast("Couldn't load more right now.");
    }
    popularLoadMore.textContent = "Load more";
    popularLoadMore.disabled = false;
  });

  function renderGenreChips() {
    genreChipRow.innerHTML = "";
    GENRES.forEach((g) => {
      const chip = document.createElement("button");
      chip.className = "genre-chip";
      chip.textContent = g;
      chip.addEventListener("click", () => openGenreView(g));
      genreChipRow.appendChild(chip);
    });
  }

  // ---------------------------------------------------------------------
  // Pill tabs: All (discovery) / Available (posted library)
  // ---------------------------------------------------------------------
  function setPillTab(tab) {
    pillTabs.forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    tabAll.classList.toggle("hidden", tab !== "all");
    tabLibrary.classList.toggle("hidden", tab !== "library");
    if (tab === "library") renderLibraryTab();
  }
  pillTabs.forEach((b) => b.addEventListener("click", () => setPillTab(b.dataset.tab)));

  // ---------------------------------------------------------------------
  // Render: Available/library tab (posted catalog, A–Z)
  // ---------------------------------------------------------------------
  function lettersWithData() {
    return new Set(available.map((a) => (a.title[0] || "").toUpperCase()));
  }

  function filteredLibrary() {
    let list = available;
    if (libraryQuery.trim()) {
      list = list.filter((a) => matchesLibraryQuery(a.title));
    } else if (activeLetter) {
      list = list.filter((a) => a.title[0].toUpperCase() === activeLetter);
    }
    return [...list].sort((a, b) => a.title.localeCompare(b.title));
  }

  function renderLetterBar() {
    letterBar.innerHTML = "";
    const has = lettersWithData();
    ALL_LETTERS.forEach((l) => {
      const btn = document.createElement("button");
      btn.className = "letter-btn" + (activeLetter === l ? " active" : "");
      btn.textContent = l;
      btn.disabled = !has.has(l);
      btn.addEventListener("click", () => {
        libraryQuery = "";
        activeLetter = activeLetter === l ? null : l;
        renderLibraryTab();
      });
      letterBar.appendChild(btn);
    });
  }

  function renderLibraryTab() {
    renderLetterBar();
    availableGroups.innerHTML = "";
    const list = filteredLibrary();
    availableEmpty.classList.toggle("hidden", list.length !== 0);

    const groups = {};
    list.forEach((a) => {
      const l = a.title[0].toUpperCase();
      (groups[l] = groups[l] || []).push(a);
    });

    Object.keys(groups).sort().forEach((letter) => {
      const wrap = document.createElement("div");
      wrap.className = "letter-group";
      const header = document.createElement("div");
      header.className = "letter-group-header";
      header.innerHTML = `<span class="letter-group-label">${letter}</span><span class="letter-group-line"></span>`;
      wrap.appendChild(header);
      const grid = document.createElement("div");
      grid.className = "available-grid";
      groups[letter].forEach((item) => {
        grid.appendChild(simplePosterCard(item, () => openLocalDetail(item)));
      });
      wrap.appendChild(grid);
      availableGroups.appendChild(wrap);
    });
  }

  // ---------------------------------------------------------------------
  // Ad slot (Available/library tab only)
  // ---------------------------------------------------------------------
  function renderAdSlot() {
    adSlot.innerHTML = "";
    if (!activeAd) {
      adSlot.classList.add("hidden");
      return;
    }
    adSlot.classList.remove("hidden");

    const card = document.createElement("div");
    card.className = "ad-card";
    thumbImg(card, activeAd.image_url, "Ad");

    const badge = document.createElement("span");
    badge.className = "ad-badge";
    badge.textContent = "AD";
    card.appendChild(badge);

    const wrap = document.createElement("div");
    wrap.className = "ad-caption-wrap";
    const summary = document.createElement("p");
    summary.className = "ad-caption-summary";
    summary.textContent = activeAd.caption;
    wrap.appendChild(summary);
    card.appendChild(wrap);

    card.addEventListener("click", openAdDetail);
    adSlot.appendChild(card);
  }

  function openAdDetail() {
    if (!activeAd) return;
    api("/api/ads/tap", { method: "POST" }).catch(() => {});
    openDetailSheet({
      title: "Sponsored",
      description: activeAd.caption,
      genres: [],
      poster_url: activeAd.image_url,
      link: activeAd.link,
    }, "ad");
  }

  // ---------------------------------------------------------------------
  // Detail sheet (compact centered modal)
  // ---------------------------------------------------------------------
  let currentDetail = null;
  let currentContext = null; // "available" | "news" | "newsarticle" | "ad" | "genre"
  let descriptionExpanded = false;

  function openDetailSheet(anime, context) {
    currentDetail = anime;
    currentContext = context;
    descriptionExpanded = false;

    detailPoster.src = "";
    detailPoster.style.display = "";
    const bannerSrc = anime.banner_url || anime.poster_url;
    if (bannerSrc) {
      detailPoster.src = bannerSrc;
      detailPoster.onerror = () => { detailPoster.style.display = "none"; };
    } else {
      detailPoster.style.display = "none";
    }

    // Overlapping thumbnail only makes sense when we have a distinct
    // poster separate from the banner (anime posts) — hide it for
    // single-image contexts (news articles, ads).
    if (anime.poster_url && anime.banner_url && anime.poster_url !== anime.banner_url) {
      detailThumb.src = anime.poster_url;
      detailThumb.classList.remove("hidden");
      detailThumb.onerror = () => detailThumb.classList.add("hidden");
    } else {
      detailThumb.classList.add("hidden");
    }

    detailTitle.textContent = anime.title;
    if (anime.alt_title) {
      detailSubtitle.textContent = anime.alt_title;
      detailSubtitle.classList.remove("hidden");
    } else {
      detailSubtitle.classList.add("hidden");
    }

    detailMetaPills.innerHTML = "";
    if (anime.format) addMetaPill(anime.format);
    if (anime.year) addMetaPill(String(anime.year));
    if (anime.duration) addMetaPill(`${anime.duration}m`);
    if (anime.rating) addMetaPill(`\u2605 ${anime.rating.toFixed(1)}`, true);

    detailGenres.innerHTML = "";
    (anime.genres || []).forEach((g) => {
      const pill = document.createElement("span");
      pill.className = "genre-pill";
      pill.textContent = g;
      detailGenres.appendChild(pill);
    });

    renderDescription();
    renderDetailAction(anime, context);
    detailOverlay.classList.remove("hidden");
  }

  function addMetaPill(text, isRating = false) {
    const pill = document.createElement("span");
    pill.className = "meta-pill" + (isRating ? " rating" : "");
    pill.textContent = text;
    detailMetaPills.appendChild(pill);
  }

  function renderDescription() {
    const text = currentDetail.description || "No synopsis available.";
    detailDescription.textContent = text;
    detailDescription.scrollTop = 0;
    detailDescription.classList.toggle("clamped", !descriptionExpanded);
    detailReadMore.classList.toggle("hidden", text.length < 180);
    detailReadMore.textContent = descriptionExpanded ? "Show Less" : "Read More";
  }

  detailReadMore.addEventListener("click", () => {
    descriptionExpanded = !descriptionExpanded;
    renderDescription();
  });

  function closeDetailSheet() {
    detailOverlay.classList.add("hidden");
    currentDetail = null;
    currentContext = null;
  }
  el("detail-close").addEventListener("click", closeDetailSheet);
  detailOverlay.addEventListener("click", (e) => {
    if (e.target === detailOverlay) closeDetailSheet();
  });

  function renderDetailAction(anime, context) {
    detailActionArea.innerHTML = "";
    reportOpenBtn.classList.toggle("hidden", context !== "available");

    if (context === "newsarticle") {
      const readBtn = document.createElement("button");
      readBtn.className = "btn btn-primary";
      readBtn.textContent = "Read Full Story";
      readBtn.addEventListener("click", () => {
        if (tg && tg.openLink) tg.openLink(anime.link);
        else window.open(anime.link, "_blank");
      });
      detailActionArea.appendChild(readBtn);
      return;
    }

    if (context === "ad" || context === "notification") {
      if (anime.link) {
        const clickBtn = document.createElement("button");
        clickBtn.className = "btn btn-primary";
        clickBtn.textContent = context === "ad" ? "Click Here" : "Open Link";
        clickBtn.addEventListener("click", () => {
          if (context === "ad") api("/api/ads/click", { method: "POST" }).catch(() => {});
          if (tg && tg.openLink) tg.openLink(anime.link);
          else window.open(anime.link, "_blank");
        });
        detailActionArea.appendChild(clickBtn);
      }
      return;
    }

    if (context === "news" || context === "genre") {
      if (anime.matchedJoinLink) {
        const joinBtn = document.createElement("button");
        joinBtn.className = "btn btn-primary";
        joinBtn.textContent = "\u25b6 Join";
        joinBtn.addEventListener("click", () => {
          if (tg && tg.openLink) tg.openLink(anime.matchedJoinLink);
          else window.open(anime.matchedJoinLink, "_blank");
        });
        detailActionArea.appendChild(joinBtn);
        return;
      }
      renderVoteButton(anime);
      return;
    }

    // context === "available"
    const row = document.createElement("div");
    row.className = "action-row";

    if (anime.join_link) {
      const joinBtn = document.createElement("button");
      joinBtn.className = "btn btn-primary";
      joinBtn.textContent = "\u25b6 Join";
      joinBtn.addEventListener("click", () => {
        if (tg && tg.openLink) tg.openLink(anime.join_link);
        else window.open(anime.join_link, "_blank");
      });
      row.appendChild(joinBtn);
    } else {
      const comingSoon = document.createElement("button");
      comingSoon.className = "btn btn-disabled";
      comingSoon.textContent = "Coming Soon";
      comingSoon.disabled = true;
      row.appendChild(comingSoon);
    }

    if (profile && profile.role === "admin" && anime.id) {
      const plus = document.createElement("button");
      plus.className = "plus-btn";
      plus.textContent = "+";
      plus.setAttribute("aria-label", "Set join link");
      plus.addEventListener("click", () => openLinkSheet(anime));
      row.appendChild(plus);
    }

    detailActionArea.appendChild(row);
  }

  function renderVoteButton(anime) {
    const btn = document.createElement("button");
    btn.className = "btn btn-primary";
    btn.textContent = "\U0001f5f3 Vote";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const result = await api("/api/vote", { method: "POST", body: JSON.stringify({ title: anime.title }) });
        btn.textContent = result.already_voted
          ? `\u2713 Already voted (${result.count})`
          : `\u2713 Voted (${result.count})`;
        showToast(result.already_voted ? "You already voted for this." : "Vote counted!");
      } catch (err) {
        btn.disabled = false;
        showToast(err.message || "Couldn't vote right now.");
      }
    });
    detailActionArea.appendChild(btn);
  }

  function openLocalDetail(item) {
    openDetailSheet(item, "available");
  }

  async function openDiscoverDetail(item) {
    openDetailSheet({ ...item, description: "Loading synopsis...", genres: item.genres || [] }, "news");
    try {
      const full = await api(`/api/anilist/${item.anilist_id}`);
      if (currentDetail && currentDetail.title === item.title) {
        openDetailSheet({ ...full, rating: item.rating ?? full.rating, matchedJoinLink: item.matchedJoinLink }, "news");
      }
    } catch (err) {
      if (currentDetail) detailDescription.textContent = "Couldn't load full details.";
    }
  }

  async function openGenreItemDetail(item) {
    const byTitle = availableByTitle();
    const matched = byTitle.get(item.title.toLowerCase());
    item.matchedJoinLink = matched && matched.join_link ? matched.join_link : null;
    openDetailSheet({ ...item, description: "Loading synopsis...", genres: [] }, "genre");
    try {
      const full = await api(`/api/anilist/${item.anilist_id}`);
      if (currentDetail && currentDetail.title === item.title) {
        openDetailSheet({ ...full, rating: item.rating ?? full.rating, matchedJoinLink: item.matchedJoinLink }, "genre");
      }
    } catch (err) {
      if (currentDetail) detailDescription.textContent = "Couldn't load full details.";
    }
  }

  function openNewsArticleDetail(item) {
    openDetailSheet({
      title: item.title,
      description: item.summary || "No summary available.",
      genres: [],
      poster_url: item.image,
      link: item.link,
    }, "newsarticle");
  }

  function openNotificationDetail(item) {
    openDetailSheet({
      title: "Notification",
      description: item.caption,
      genres: [],
      poster_url: item.image_url,
      link: item.link,
    }, "notification");
  }

  // ---------------------------------------------------------------------
  // Set Join Link sheet (admin only)
  // ---------------------------------------------------------------------
  let linkTargetAnime = null;

  function openLinkSheet(anime) {
    linkTargetAnime = anime;
    linkInput.value = anime.join_link || "";
    linkOverlay.classList.remove("hidden");
    linkInput.focus();
  }
  function closeLinkSheet() {
    linkOverlay.classList.add("hidden");
    linkTargetAnime = null;
  }
  el("link-cancel").addEventListener("click", closeLinkSheet);
  linkOverlay.addEventListener("click", (e) => { if (e.target === linkOverlay) closeLinkSheet(); });

  el("link-save").addEventListener("click", async () => {
    if (!linkTargetAnime) return;
    const value = linkInput.value.trim();
    try {
      const result = await api(`/api/anime/${linkTargetAnime.id}/link`, { method: "PATCH", body: JSON.stringify({ link: value }) });
      linkTargetAnime.join_link = value;
      if (currentDetail && currentDetail.id === linkTargetAnime.id) {
        currentDetail.join_link = value;
        renderDetailAction(currentDetail, currentContext);
      }
      closeLinkSheet();
      showToast(result.propagated ? `Link saved — applied to ${result.propagated} related season(s) too` : "Link saved");
      await loadAvailable();
    } catch (err) {
      showToast(err.message || "Couldn't save link");
    }
  });

  // ---------------------------------------------------------------------
  // Report sheet
  // ---------------------------------------------------------------------
  reportOpenBtn.addEventListener("click", () => {
    selectedReason = null;
    reportDetails.value = "";
    document.querySelectorAll(".reason-btn").forEach((b) => b.classList.remove("selected"));
    reportOverlay.classList.remove("hidden");
  });
  document.querySelectorAll(".reason-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      selectedReason = btn.dataset.reason;
      document.querySelectorAll(".reason-btn").forEach((b) => b.classList.remove("selected"));
      btn.classList.add("selected");
    });
  });
  el("report-cancel").addEventListener("click", () => reportOverlay.classList.add("hidden"));
  reportOverlay.addEventListener("click", (e) => { if (e.target === reportOverlay) reportOverlay.classList.add("hidden"); });
  el("report-submit").addEventListener("click", async () => {
    if (!selectedReason) { showToast("Pick a reason first"); return; }
    if (!currentDetail) return;
    try {
      await api("/api/report", {
        method: "POST",
        body: JSON.stringify({
          anime_id: currentDetail.id || null,
          anime_title: currentDetail.title,
          reason: selectedReason,
          details: reportDetails.value.trim(),
        }),
      });
      reportOverlay.classList.add("hidden");
      showToast("Report submitted — thank you.");
    } catch (err) {
      showToast(err.message || "Couldn't submit report");
    }
  });

  // ---------------------------------------------------------------------
  // Search page
  // ---------------------------------------------------------------------
  const GENRES = ["Action", "Adventure", "Comedy", "Drama", "Fantasy", "Romance", "Sci-Fi", "Horror"];
  let genreThumbs = {};

  function renderSearchLanding() {
    searchViewInput.value = "";
    searchResults.classList.add("hidden");
    searchLanding.classList.remove("hidden");
    renderPopularSearches();
    renderGenreTiles();
  }

  async function renderPopularSearches() {
    popularSearchList.innerHTML = "";
    popularSearchClear.classList.toggle("hidden", !(profile && profile.role === "admin"));
    let items = [];
    try {
      items = await api("/api/search/popular?limit=6");
    } catch (err) { /* silently empty */ }
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "popular-search-row";
      row.innerHTML = `<span class="popular-search-icon">\u{1F50D}</span>
        <span class="popular-search-text">${escapeHtml(item.query)}</span>
        <span class="popular-search-arrow">\u2197</span>`;
      row.addEventListener("click", () => {
        searchViewInput.value = item.query;
        runLibrarySearch(item.query, false);
      });
      popularSearchList.appendChild(row);
    });
  }

  popularSearchClear.addEventListener("click", async () => {
    try {
      await api("/api/search/clear", { method: "POST" });
      renderPopularSearches();
    } catch (err) {
      showToast(err.message || "Couldn't clear searches");
    }
  });

  async function renderGenreTiles() {
    genreTileGrid.innerHTML = "";
    if (!Object.keys(genreThumbs).length) {
      try {
        const data = await api("/api/genres");
        data.forEach((g) => { genreThumbs[g.genre] = g.thumbnail; });
      } catch (err) { /* fall back to generated thumbs below */ }
    }
    GENRES.forEach((g) => {
      const tile = document.createElement("div");
      tile.className = "genre-tile";
      thumbImg(tile, genreThumbs[g], g);
      const label = document.createElement("div");
      label.className = "genre-tile-label";
      label.innerHTML = `<span class="genre-tile-name">${g.toUpperCase()}</span><span class="genre-tile-explore">Explore &rsaquo;</span>`;
      tile.appendChild(label);
      tile.addEventListener("click", () => openGenreView(g));
      genreTileGrid.appendChild(tile);
    });
  }

  const trackSearch = debounce((q) => {
    api("/api/search/track", { method: "POST", body: JSON.stringify({ query: q }) }).catch(() => {});
  }, 600);

  function runLibrarySearch(q, track = true) {
    const query = q.trim();
    if (!query) { renderSearchLanding(); return; }
    searchLanding.classList.add("hidden");
    searchResults.classList.remove("hidden");
    const matches = available.filter((a) => a.title.toLowerCase().includes(query.toLowerCase()));
    searchResultsGroups.innerHTML = "";
    searchResultsEmpty.classList.toggle("hidden", matches.length !== 0);
    matches.forEach((item) => {
      const row = document.createElement("div");
      row.className = "search-result-row";
      thumbImg(row, item.poster_url, item.title);
      const title = document.createElement("span");
      title.className = "search-result-title";
      title.textContent = item.title;
      row.appendChild(title);
      row.addEventListener("click", () => openLocalDetail(item));
      searchResultsGroups.appendChild(row);
    });
    if (track) trackSearch(query);
  }

  searchViewInput.addEventListener("input", (e) => runLibrarySearch(e.target.value));

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  // ---------------------------------------------------------------------
  // Genre browse view
  // ---------------------------------------------------------------------
  async function openGenreView(genre) {
    showView("genre");
    genreViewTitle.textContent = genre;
    genreBrowseGrid.innerHTML = "";
    try {
      const data = await api(`/api/genres/${encodeURIComponent(genre)}`);
      data.results.forEach((item) => {
        genreBrowseGrid.appendChild(simplePosterCard(item, () => openGenreItemDetail(item)));
      });
    } catch (err) {
      showToast("Couldn't load that genre right now.");
    }
  }

  // ---------------------------------------------------------------------
  // Notifications
  // ---------------------------------------------------------------------
  async function loadNotifications() {
    notificationsList.innerHTML = "";
    let items = [];
    try {
      items = await api("/api/notifications");
    } catch (err) { /* leave empty */ }
    notificationsEmpty.classList.toggle("hidden", items.length !== 0);
    items.forEach((item) => {
      const card = document.createElement("div");
      card.className = "notification-card";
      thumbImg(card, item.image_url, "Notification");
      const body = document.createElement("div");
      body.className = "notification-body";
      const caption = document.createElement("p");
      caption.className = "notification-caption";
      caption.textContent = item.caption;
      body.appendChild(caption);
      const time = document.createElement("span");
      time.className = "notification-time";
      time.textContent = new Date(item.created_at * 1000).toLocaleString();
      body.appendChild(time);
      card.appendChild(body);
      card.addEventListener("click", () => openNotificationDetail(item));
      notificationsList.appendChild(card);
    });
    notifBadge.textContent = String(items.length);
    notifBadge.classList.toggle("hidden", items.length === 0);
  }

  // ---------------------------------------------------------------------
  // Profile
  // ---------------------------------------------------------------------
  function initials(name) {
    return (name || "?").trim().charAt(0).toUpperCase();
  }

  async function openProfile() {
    profileCard.innerHTML = `<p class="profile-hint">Loading profile\u2026</p>`;
    try {
      profile = await api("/api/profile");
      const displayName = profile.first_name || profile.username || "User";
      profileCard.innerHTML = `
        <div class="profile-header">
          <div class="profile-avatar">${initials(displayName)}</div>
          <div>
            <div class="profile-name">${escapeHtml(displayName)}</div>
            <div class="profile-username">${profile.username ? "@" + escapeHtml(profile.username) : "no username"}</div>
          </div>
        </div>
        <div class="profile-row"><span class="label">Telegram ID</span><span class="value">${profile.telegram_id}</span></div>
        <div class="profile-row"><span class="label">Registered in bot</span><span class="value">yes</span></div>
        <div class="profile-row"><span class="label">Role</span><span class="value">${escapeHtml(profile.role)}</span></div>
        <div class="profile-row"><span class="label">Access</span><span class="value">${escapeHtml(profile.access)}</span></div>
      `;
    } catch (err) {
      profileCard.innerHTML = `<p class="profile-hint">${escapeHtml(err.message || "Open this from inside Telegram to view your profile.")}</p>`;
    }
  }

  // ---------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------
  async function loadNews() {
    try {
      const [trendingData, popularData] = await Promise.all([
        api("/api/catalog/trending"),
        api("/api/catalog/popular"),
      ]);
      trending = trendingData.results;
      popular = popularData.results;
      popularHasNext = popularData.has_next;
      popularPage = 1;
    } catch (err) {
      trending = [];
      popular = [];
      popularHasNext = false;
    }
    renderTrending();
    renderTopAiring();
  }

  async function loadAnimeNews() {
    try {
      newsItems = await api("/api/news/latest?limit=10");
    } catch (err) {
      newsItems = [];
    }
    featuredIndex = 0;
    renderFeatured();
    startFeaturedAutoplay();
  }

  async function loadAvailable() {
    try {
      available = await api("/api/catalog/available");
    } catch (err) {
      available = [];
    }
    if (!tabLibrary.classList.contains("hidden")) renderLibraryTab();
  }

  async function loadAd() {
    try {
      activeAd = await api("/api/ads/active");
    } catch (err) {
      activeAd = null;
    }
    renderAdSlot();
  }

  async function preloadProfile() {
    try {
      profile = await api("/api/profile");
    } catch (err) {
      profile = null;
    }
  }

  // ---------------------------------------------------------------------
  // Deep links from the bot: ?anime=<id> opens that post directly,
  // ?search=<text>&tab=library pre-fills the Search page with that title.
  // ---------------------------------------------------------------------
  function applyDeepLink() {
    const params = new URLSearchParams(window.location.search);
    const animeId = params.get("anime");
    const searchParam = params.get("search");

    if (animeId) {
      const match = available.find((a) => String(a.id) === String(animeId));
      if (match) openLocalDetail(match);
    } else if (searchParam) {
      showView("search");
      renderSearchLanding();
      searchViewInput.value = searchParam;
      runLibrarySearch(searchParam);
    }
  }

  (async function init() {
    document.title = brandName;
    renderGenreChips();
    await Promise.all([loadNews(), loadAvailable(), loadAnimeNews(), loadAd(), preloadProfile()]);
    applyDeepLink();
  })();
})();
