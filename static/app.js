(() => {
  "use strict";

  const brandName = document.body.dataset.brand || "Anime Eternals";

  // ---------------------------------------------------------------------
  // Telegram WebApp bootstrap (no-ops gracefully outside Telegram)
  // ---------------------------------------------------------------------
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    try {
      tg.ready();
      tg.expand();
      tg.setHeaderColor && tg.setHeaderColor("#f4f1e6");
      tg.setBackgroundColor && tg.setBackgroundColor("#f4f1e6");
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
  const profileView = el("profile-view");
  const allViews = { app: appView, search: searchView, genre: genreView, profile: profileView };

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

  const pillTabs = document.querySelectorAll(".pill-tab");
  const tabAll = el("tab-all");
  const tabLibrary = el("tab-library");

  const scrollArea = el("scroll-area");
  const trendingRow = el("trending-row");
  const topAiringList = el("top-airing-list");
  const popularLoadMore = el("popular-load-more");

  const letterBar = el("letter-bar");
  const availableGroups = el("available-groups");
  const availableEmpty = el("available-empty");
  const adSlot = el("ad-slot");

  const navBtns = document.querySelectorAll(".nav-btn");

  const detailOverlay = el("detail-overlay");
  const detailPoster = el("detail-poster");
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
  let available = [];
  let activeAd = null;
  let activeLetter = null;
  let libraryQuery = "";
  let profile = null;

  const ALL_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("");

  function availableByTitle() {
    const map = new Map();
    available.forEach((a) => map.set(a.title.toLowerCase(), a));
    return map;
  }

  // ---------------------------------------------------------------------
  // Top-level navigation (Home / Search / Profile)
  // ---------------------------------------------------------------------
  function showView(name) {
    Object.entries(allViews).forEach(([key, node]) => node.classList.toggle("hidden", key !== name));
    navBtns.forEach((b) => b.classList.toggle("active", b.dataset.nav === (name === "app" ? "home" : name)));
  }

  navBtns.forEach((btn) => btn.addEventListener("click", () => {
    const target = btn.dataset.nav;
    if (target === "home") showView("app");
    else if (target === "search") { showView("search"); renderSearchLanding(); }
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
    card.appendChild(meta);

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

  function popularGridCard(item, onOpen) {
    const card = document.createElement("div");
    card.className = "popular-card";

    const media = document.createElement("div");
    media.className = "popular-card-media";
    thumbImg(media, item.poster_url, item.title);

    const newEp = document.createElement("span");
    newEp.className = "new-ep-badge";
    newEp.textContent = "NEW EP";
    media.appendChild(newEp);
    card.appendChild(media);

    const titleWrap = document.createElement("div");
    titleWrap.className = "popular-card-title-wrap";
    const title = document.createElement("p");
    title.className = "popular-card-title";
    title.textContent = item.title;
    titleWrap.appendChild(title);

    const metaRow = document.createElement("div");
    metaRow.className = "popular-card-meta-row";
    const episode = document.createElement("span");
    episode.className = "popular-card-episode";
    episode.textContent = item.episodes ? `${item.episodes} Episodes` : "";
    metaRow.appendChild(episode);
    if (item.rating) {
      const rating = document.createElement("span");
      rating.className = "popular-card-rating";
      rating.textContent = "\u2605 " + item.rating.toFixed(1);
      metaRow.appendChild(rating);
    }
    titleWrap.appendChild(metaRow);
    card.appendChild(titleWrap);

    card.addEventListener("click", onOpen);
    return card;
  }

  function matchesLibraryQuery(title) {
    return !libraryQuery || title.toLowerCase().includes(libraryQuery.toLowerCase());
  }

  // ---------------------------------------------------------------------
  // Render: Home "All" tab — Trending, Top Airing (+ Load more)
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
    popular.forEach((item) => {
      const matched = byTitle.get(item.title.toLowerCase());
      item.matchedJoinLink = matched && matched.join_link ? matched.join_link : null;
      topAiringList.appendChild(popularGridCard(item, () => openDiscoverDetail(item)));
    });
  }

  let popularLoading = false;
  async function loadMorePopular() {
    if (popularLoading || !popularHasNext) return;
    popularLoading = true;
    popularLoadMore.classList.remove("hidden");
    try {
      const data = await api(`/api/catalog/popular?page=${popularPage + 1}`);
      popularPage += 1;
      popular = popular.concat(data.results);
      popularHasNext = data.has_next;
      renderTopAiring();
    } catch (err) {
      showToast("Couldn't load more right now.");
    }
    popularLoadMore.classList.add("hidden");
    popularLoading = false;
  }

  // Auto-load more Popular anime as the Home tab is scrolled, instead of
  // making the user tap a button.
  scrollArea.addEventListener("scroll", debounce(() => {
    if (tabAll.classList.contains("hidden")) return;
    const nearBottom = scrollArea.scrollTop + scrollArea.clientHeight > scrollArea.scrollHeight - 400;
    if (nearBottom) loadMorePopular();
  }, 150));

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
  let currentContext = null; // "available" | "discover" | "ad" | "genre"
  let descriptionExpanded = false;

  function openDetailSheet(anime, context) {
    currentDetail = anime;
    currentContext = context;
    descriptionExpanded = false;

    const sheetMedia = detailPoster.parentElement;
    sheetMedia.querySelectorAll(".generated-thumb").forEach((n) => n.remove());
    detailPoster.src = "";
    detailPoster.style.display = "";
    const hasRealBanner = !!anime.banner_url;
    const bannerSrc = anime.banner_url || anime.poster_url;
    // A true banner is already wide, so a centered cover-crop looks right.
    // A portrait poster forced into that same short, wide box can't be
    // cover-cropped without zooming in hard and losing most of the art
    // (usually landing on a jarring close-up of just the eyes). Instead,
    // show it uncropped over a blurred version of itself as a backdrop.
    detailPoster.classList.toggle("poster-fallback", !hasRealBanner);
    sheetMedia.classList.toggle("has-blur-bg", !hasRealBanner && !!bannerSrc);
    if (!hasRealBanner && bannerSrc) {
      sheetMedia.style.setProperty("--banner-img", `url("${bannerSrc}")`);
    } else {
      sheetMedia.style.removeProperty("--banner-img");
    }
    if (bannerSrc) {
      detailPoster.src = bannerSrc;
      detailPoster.onerror = () => {
        detailPoster.style.display = "none";
        sheetMedia.classList.remove("has-blur-bg");
        const gen = generatedThumb(anime.title);
        gen.style.position = "absolute";
        gen.style.inset = "0";
        gen.style.zIndex = "1";
        sheetMedia.insertBefore(gen, detailPoster);
      };
    } else {
      detailPoster.style.display = "none";
      const gen = generatedThumb(anime.title);
      gen.style.position = "absolute";
      gen.style.inset = "0";
      sheetMedia.insertBefore(gen, detailPoster);
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
    reportOpenBtn.classList.toggle("hidden", !["available", "discover", "genre"].includes(context));

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

    if (context === "discover" || context === "genre") {
      const row = document.createElement("div");
      row.className = "action-row";

      if (anime.matchedJoinLink) {
        const joinBtn = document.createElement("button");
        joinBtn.className = "btn btn-primary";
        joinBtn.textContent = "\u25b6 Join";
        joinBtn.addEventListener("click", () => {
          if (tg && tg.openLink) tg.openLink(anime.matchedJoinLink);
          else window.open(anime.matchedJoinLink, "_blank");
        });
        row.appendChild(joinBtn);
      } else {
        const voteBtn = document.createElement("button");
        voteBtn.className = "btn btn-primary";
        voteBtn.textContent = "Vote";
        voteBtn.addEventListener("click", async () => {
          voteBtn.disabled = true;
          try {
            const result = await api("/api/vote", { method: "POST", body: JSON.stringify({ title: anime.title }) });
            voteBtn.textContent = result.already_voted
              ? `\u2713 Already voted (${result.count})`
              : `\u2713 Voted (${result.count})`;
            showToast(result.already_voted ? "You already voted for this." : "Vote counted!");
          } catch (err) {
            voteBtn.disabled = false;
            showToast(err.message || "Couldn't send vote right now.");
          }
        });
        row.appendChild(voteBtn);
      }

      if (profile && profile.role === "admin" && anime.anilist_id) {
        const plus = document.createElement("button");
        plus.className = "plus-btn";
        plus.textContent = "+";
        plus.setAttribute("aria-label", "Set join link");
        plus.addEventListener("click", () => openLinkSheet(anime));
        row.appendChild(plus);
      }

      detailActionArea.appendChild(row);
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

  function openLocalDetail(item) {
    openDetailSheet(item, "available");
  }

  async function openDiscoverDetail(item) {
    openDetailSheet({ ...item, description: "Loading synopsis...", genres: item.genres || [] }, "discover");
    try {
      const full = await api(`/api/anilist/${item.anilist_id}`);
      if (currentDetail && currentDetail.title === item.title) {
        openDetailSheet({ ...full, anilist_id: item.anilist_id, rating: item.rating ?? full.rating, matchedJoinLink: item.matchedJoinLink }, "discover");
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
        openDetailSheet({ ...full, anilist_id: item.anilist_id, rating: item.rating ?? full.rating, matchedJoinLink: item.matchedJoinLink }, "genre");
      }
    } catch (err) {
      if (currentDetail) detailDescription.textContent = "Couldn't load full details.";
    }
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
      let result;
      if (linkTargetAnime.id) {
        result = await api(`/api/anime/${linkTargetAnime.id}/link`, { method: "PATCH", body: JSON.stringify({ link: value }) });
        linkTargetAnime.join_link = value;
        if (currentDetail && currentDetail.id === linkTargetAnime.id) {
          currentDetail.join_link = value;
        }
      } else {
        // Not in the local library yet (Discover/Genre post) — this creates
        // the library entry with the link already set, same result as
        // /addpost + Set Join Link in one step.
        result = await api(`/api/anime/link-anilist/${linkTargetAnime.anilist_id}`, {
          method: "POST",
          body: JSON.stringify({ link: value }),
        });
        linkTargetAnime.id = result.anime.id;
        linkTargetAnime.join_link = result.anime.join_link;
        linkTargetAnime.matchedJoinLink = result.anime.join_link;
        if (currentDetail && currentDetail.anilist_id === linkTargetAnime.anilist_id) {
          currentDetail.id = result.anime.id;
          currentDetail.join_link = result.anime.join_link;
          currentDetail.matchedJoinLink = result.anime.join_link;
        }
      }
      if (currentDetail) renderDetailAction(currentDetail, currentContext);
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

  function renderGenreTiles() {
    genreTileGrid.innerHTML = "";
    const tileByGenre = {};
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
      tileByGenre[g] = tile;
    });

    if (Object.keys(genreThumbs).length) return; // already fetched this session

    api("/api/genres").then((data) => {
      data.forEach((g) => {
        genreThumbs[g.genre] = g.thumbnail;
        if (!g.thumbnail) return;
        const tile = tileByGenre[g.genre];
        if (!tile) return;
        tile.querySelectorAll("img, .generated-thumb").forEach((n) => n.remove());
        thumbImg(tile, g.thumbnail, g.genre);
      });
    }).catch(() => { /* placeholders stay as-is */ });
  }

  const trackSearch = debounce((q) => {
    api("/api/search/track", { method: "POST", body: JSON.stringify({ query: q }) }).catch(() => {});
  }, 600);

  let searchQuery = "";
  let searchPage = 1;
  let searchHasNext = false;
  let searchLoading = false;
  let searchToken = 0;

  function searchResultRow(item, onOpen) {
    const row = document.createElement("div");
    row.className = "search-result-row";
    thumbImg(row, item.poster_url, item.title);
    const body = document.createElement("div");
    body.className = "search-result-body";
    const title = document.createElement("p");
    title.className = "search-result-title";
    title.textContent = item.title;
    body.appendChild(title);
    const meta = document.createElement("div");
    meta.className = "search-result-meta";
    if (item.year) {
      const year = document.createElement("span");
      year.className = "search-result-year";
      year.textContent = item.year;
      meta.appendChild(year);
    }
    if (item.rating) {
      const rating = document.createElement("span");
      rating.className = "search-result-rating";
      rating.textContent = "\u2605 " + item.rating.toFixed(1);
      meta.appendChild(rating);
    }
    body.appendChild(meta);
    if (item.genres && item.genres.length) {
      const genres = document.createElement("p");
      genres.className = "search-result-genres";
      genres.textContent = item.genres.join(", ");
      body.appendChild(genres);
    }
    row.appendChild(body);
    row.addEventListener("click", onOpen);
    return row;
  }

  async function runLibrarySearch(q, track = true) {
    const query = q.trim();
    if (!query) { renderSearchLanding(); return; }
    searchQuery = query;
    searchPage = 1;
    searchHasNext = false;
    searchLanding.classList.add("hidden");
    searchResults.classList.remove("hidden");
    searchResultsGroups.innerHTML = "";
    searchResultsEmpty.classList.add("hidden");

    const byTitle = availableByTitle();
    const localMatches = available.filter((a) => a.title.toLowerCase().includes(query.toLowerCase()));
    localMatches.forEach((item) => {
      searchResultsGroups.appendChild(searchResultRow(item, () => openLocalDetail(item)));
    });

    const myToken = ++searchToken;
    searchLoading = true;
    try {
      const data = await api(`/api/search/anime?q=${encodeURIComponent(query)}&page=1`);
      if (myToken !== searchToken) return; // a newer search superseded this one
      searchHasNext = data.has_next;
      const localTitles = new Set(localMatches.map((a) => a.title.toLowerCase()));
      data.results.forEach((item) => {
        if (localTitles.has(item.title.toLowerCase())) return; // already shown above
        const matched = byTitle.get(item.title.toLowerCase());
        item.matchedJoinLink = matched && matched.join_link ? matched.join_link : null;
        searchResultsGroups.appendChild(searchResultRow(item, () => openDiscoverDetail(item)));
      });
      searchResultsEmpty.classList.toggle("hidden", searchResultsGroups.children.length !== 0);
    } catch (err) {
      searchResultsEmpty.classList.toggle("hidden", searchResultsGroups.children.length !== 0);
    }
    searchLoading = false;
    if (track) trackSearch(query);
  }

  async function loadMoreSearchResults() {
    if (searchLoading || !searchHasNext || !searchQuery) return;
    searchLoading = true;
    const myToken = searchToken;
    try {
      const data = await api(`/api/search/anime?q=${encodeURIComponent(searchQuery)}&page=${searchPage + 1}`);
      if (myToken !== searchToken) return;
      searchPage += 1;
      searchHasNext = data.has_next;
      const byTitle = availableByTitle();
      data.results.forEach((item) => {
        const matched = byTitle.get(item.title.toLowerCase());
        item.matchedJoinLink = matched && matched.join_link ? matched.join_link : null;
        searchResultsGroups.appendChild(searchResultRow(item, () => openDiscoverDetail(item)));
      });
    } catch (err) { /* stop silently, user can keep scrolling to retry */ }
    searchLoading = false;
  }

  searchViewInput.addEventListener("input", debounce((e) => runLibrarySearch(e.target.value), 350));

  // Infinite scroll: the Search subview scrolls the document itself.
  window.addEventListener("scroll", debounce(() => {
    if (searchView.classList.contains("hidden") || searchResults.classList.contains("hidden")) return;
    const nearBottom = window.scrollY + window.innerHeight > document.documentElement.scrollHeight - 400;
    if (nearBottom) loadMoreSearchResults();
  }, 150));

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
  async function loadDiscover() {
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
    await Promise.all([loadDiscover(), loadAvailable(), loadAd(), preloadProfile()]);
    applyDeepLink();
  })();
})();
