/*
 * Codzienne Spojrzenie - audio player.
 * Mirrors the app player: -15 s, play/pause, +15 s, stop, seek bar.
 * Extras: per-edition resume position, playback speed, "play whole edition"
 * (general -> tech) and lock-screen controls (Media Session).
 */
(function () {
  "use strict";

  var cfg = window.CC_CONFIG;
  var ORDER = ["general", "tech"];

  var audio = document.getElementById("audio");
  var el = {
    bar: document.getElementById("player"),
    label: document.getElementById("player-label"),
    title: document.getElementById("player-title"),
    toggle: document.getElementById("p-toggle"),
    back: document.getElementById("p-back"),
    fwd: document.getElementById("p-fwd"),
    stop: document.getElementById("p-stop"),
    speed: document.getElementById("p-speed"),
    seek: document.getElementById("p-seek"),
    cur: document.getElementById("p-current"),
    dur: document.getElementById("p-duration")
  };

  var state = {
    edition: "",
    track: null,
    mode: "single", // "single" | "all"
    seeking: false,
    durations: {},
    speedIdx: 0,
    lastSave: 0
  };
  var listeners = [];
  var errorHandler = function () {};

  // ---------- helpers ----------
  function lsGet(key) {
    try { return JSON.parse(localStorage.getItem(key)); } catch (e) { return null; }
  }
  function lsSet(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* storage full or blocked */ }
  }

  function fmt(sec) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    sec = Math.floor(sec);
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    var mm = h ? String(m).padStart(2, "0") : String(m);
    return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
  }

  function posKey(track) { return "cc:pos:" + track + ":" + (state.edition || "latest"); }
  function savedPos(track) { return lsGet(posKey(track)); }

  function savePos(force) {
    if (!state.track || !isFinite(audio.duration)) return;
    var now = Date.now();
    if (!force && now - state.lastSave < 3000) return;
    state.lastSave = now;
    lsSet(posKey(state.track), { t: audio.currentTime, d: audio.duration });
  }

  function srcFor(track) {
    var url = cfg.feeds[track].audio;
    // Edition-based cache-buster: the Dropbox URL stays the same every day.
    return url + (url.indexOf("?") > -1 ? "&" : "?") + "v=" + encodeURIComponent(state.edition || "latest");
  }

  function isPlaying() { return !!state.track && !audio.paused && !audio.ended; }

  function emit() {
    var snapshot = {
      track: state.track,
      mode: state.mode,
      playing: isPlaying(),
      durations: state.durations
    };
    listeners.forEach(function (fn) { fn(snapshot); });
  }

  // ---------- bar UI ----------
  function setPlayerHeight() {
    var h = el.bar.hidden ? 0 : el.bar.offsetHeight + 8;
    document.documentElement.style.setProperty("--player-h", h + "px");
  }

  function showBar() {
    if (el.bar.hidden) {
      el.bar.hidden = false;
      requestAnimationFrame(setPlayerHeight);
    }
  }

  function hideBar() {
    el.bar.hidden = true;
    setPlayerHeight();
  }

  function renderBar() {
    var playing = isPlaying();
    el.toggle.classList.toggle("is-playing", playing);
    el.toggle.setAttribute("aria-label", playing ? "Wstrzymaj" : "Odtwórz");
    el.speed.textContent = String(currentSpeed()).replace(".", ",") + "×";
    if (state.track) {
      var feed = cfg.feeds[state.track];
      el.title.textContent = feed.title;
      el.label.textContent = state.mode === "all"
        ? "Całe wydanie · " + (ORDER.indexOf(state.track) + 1) + "/" + ORDER.length
        : "Teraz odtwarzane";
    }
    renderTime();
  }

  function renderTime() {
    var d = audio.duration;
    var t = state.seeking ? (el.seek.value / 1000) * (d || 0) : audio.currentTime;
    el.cur.textContent = fmt(t);
    el.dur.textContent = isFinite(d) ? fmt(d) : "--:--";
    if (!state.seeking) {
      el.seek.value = isFinite(d) && d > 0 ? Math.round((audio.currentTime / d) * 1000) : 0;
    }
    el.seek.style.setProperty("--p", (el.seek.value / 10) + "%");
  }

  // ---------- media session ----------
  function updateMediaSession() {
    if (!("mediaSession" in navigator) || !state.track) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: cfg.feeds[state.track].title,
        artist: cfg.siteName,
        album: state.editionLabel || "Wydanie dnia",
        artwork: [{ src: new URL("assets/img/favicon.svg", location.href).href, sizes: "any", type: "image/svg+xml" }]
      });
    } catch (e) { /* unsupported */ }
  }

  function updatePositionState() {
    if (!("mediaSession" in navigator) || !navigator.mediaSession.setPositionState) return;
    if (!isFinite(audio.duration)) return;
    try {
      navigator.mediaSession.setPositionState({
        duration: audio.duration,
        playbackRate: audio.playbackRate,
        position: Math.min(audio.currentTime, audio.duration)
      });
    } catch (e) { /* ignore */ }
  }

  function bindMediaSession() {
    if (!("mediaSession" in navigator)) return;
    var ms = navigator.mediaSession;
    var handlers = {
      play: function () { play(); },
      pause: function () { audio.pause(); },
      stop: function () { stop(); },
      seekbackward: function (d) { skip(-(d.seekOffset || cfg.skipSeconds)); },
      seekforward: function (d) { skip(d.seekOffset || cfg.skipSeconds); },
      seekto: function (d) { if (isFinite(d.seekTime)) audio.currentTime = d.seekTime; },
      nexttrack: function () { next(); },
      previoustrack: function () { prev(); }
    };
    Object.keys(handlers).forEach(function (action) {
      try { ms.setActionHandler(action, handlers[action]); } catch (e) { /* unsupported action */ }
    });
  }

  // ---------- core ----------
  function currentSpeed() { return cfg.speeds[state.speedIdx] || 1; }

  function applySpeed() {
    audio.defaultPlaybackRate = currentSpeed();
    audio.playbackRate = currentSpeed();
  }

  function play() {
    var p = audio.play();
    if (p && p.catch) {
      p.catch(function (err) {
        if (err && err.name === "AbortError") return;
        if (err && err.name === "NotAllowedError") return; // autoplay policy: user can press play
        errorHandler("Nie udało się odtworzyć nagrania. Spróbuj ponownie.");
      });
    }
  }

  function load(track, autoplay) {
    if (state.track && state.track !== track) savePos(true);
    state.track = track;
    el.bar.classList.add("is-loading");
    audio.src = srcFor(track);
    applySpeed();

    var saved = savedPos(track);
    if (saved && saved.t > 3 && saved.d && saved.t < saved.d - 5) {
      audio.addEventListener("loadedmetadata", function once() {
        // Resume only the same recording: Dropbox may already have a newer MP3
        // than the text edition (different length = different recording).
        if (sameRecording(saved, audio.duration)) audio.currentTime = saved.t;
      }, { once: true });
    }

    showBar();
    updateMediaSession();
    renderBar();
    emit();
    if (autoplay) play();
  }

  function toggle() {
    if (!state.track) return;
    if (audio.paused || audio.ended) play(); else audio.pause();
  }

  function skip(sec) {
    if (!state.track || !isFinite(audio.duration)) return;
    audio.currentTime = Math.max(0, Math.min(audio.duration - 0.25, audio.currentTime + sec));
    renderTime();
  }

  function stop() {
    // Like the app's Stop: stop playback and reset this track to the beginning.
    if (state.track) lsSet(posKey(state.track), { t: 0, d: audio.duration || 0 });
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    state.track = null;
    state.mode = "single";
    el.bar.classList.remove("is-loading");
    hideBar();
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "none";
    emit();
  }

  function next() {
    var i = ORDER.indexOf(state.track);
    if (state.mode === "all" && i > -1 && i < ORDER.length - 1) load(ORDER[i + 1], true);
  }

  function prev() {
    var i = ORDER.indexOf(state.track);
    if (audio.currentTime > 5 || state.mode !== "all" || i <= 0) {
      audio.currentTime = 0;
    } else {
      load(ORDER[i - 1], true);
    }
  }

  // ---------- public API ----------
  function playTrack(track) {
    if (state.track === track) {
      state.mode = "single";
      toggle();
      emit();
      return;
    }
    state.mode = "single";
    load(track, true);
  }

  function playAll() {
    if (state.mode === "all" && state.track) { toggle(); return; }
    state.mode = "all";
    if (state.track && ORDER.indexOf(state.track) > -1) {
      // Already listening to one track: continue it, then move on to the rest.
      if (audio.paused) play();
      renderBar();
      emit();
      return;
    }
    load(ORDER[0], true);
  }

  function progress(track) {
    if (track === state.track && isFinite(audio.duration) && audio.duration > 0) {
      return audio.currentTime / audio.duration;
    }
    var s = savedPos(track);
    if (!s || !s.d || !sameRecording(s, state.durations[track])) return 0;
    return Math.min(1, s.t / s.d);
  }

  /** True when a saved position belongs to the recording of this length (±2 s). */
  function sameRecording(saved, duration) {
    return isFinite(duration) && duration > 0 && Math.abs(saved.d - duration) <= 2;
  }

  function preloadDurations() {
    ORDER.forEach(function (track) {
      var probe = new Audio();
      probe.preload = "metadata";
      probe.addEventListener("loadedmetadata", function () {
        if (isFinite(probe.duration)) {
          state.durations[track] = probe.duration;
          emit();
        }
        probe.removeAttribute("src");
        probe.load();
      }, { once: true });
      probe.src = srcFor(track);
    });
  }

  // ---------- events ----------
  audio.addEventListener("play", function () {
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "playing";
    renderBar(); emit();
  });
  audio.addEventListener("pause", function () {
    savePos(true);
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "paused";
    renderBar(); emit();
  });
  audio.addEventListener("timeupdate", function () {
    renderTime();
    savePos(false);
    emitThrottled();
  });
  audio.addEventListener("loadedmetadata", function () {
    if (state.track && isFinite(audio.duration)) state.durations[state.track] = audio.duration;
    renderTime(); updatePositionState(); emit();
  });
  audio.addEventListener("waiting", function () { el.bar.classList.add("is-loading"); });
  audio.addEventListener("playing", function () { el.bar.classList.remove("is-loading"); updatePositionState(); });
  audio.addEventListener("canplay", function () { el.bar.classList.remove("is-loading"); });
  audio.addEventListener("ratechange", updatePositionState);
  audio.addEventListener("seeked", updatePositionState);
  audio.addEventListener("ended", function () {
    lsSet(posKey(state.track), { t: audio.duration, d: audio.duration });
    if (state.mode === "all" && ORDER.indexOf(state.track) < ORDER.length - 1) {
      next();
    } else {
      renderBar(); emit();
    }
  });
  audio.addEventListener("error", function () {
    if (!state.track) return; // src removed by stop()
    el.bar.classList.remove("is-loading");
    errorHandler("Nagranie jest chwilowo niedostępne. Spróbuj ponownie później.");
    renderBar(); emit();
  });

  var lastEmit = 0;
  function emitThrottled() {
    var now = Date.now();
    if (now - lastEmit > 1000) { lastEmit = now; emit(); }
  }

  el.toggle.addEventListener("click", toggle);
  el.back.addEventListener("click", function () { skip(-cfg.skipSeconds); });
  el.fwd.addEventListener("click", function () { skip(cfg.skipSeconds); });
  el.stop.addEventListener("click", stop);
  el.speed.addEventListener("click", function () {
    state.speedIdx = (state.speedIdx + 1) % cfg.speeds.length;
    try { localStorage.setItem("cc:speed", String(state.speedIdx)); } catch (e) {}
    applySpeed();
    renderBar();
  });
  el.seek.addEventListener("input", function () {
    state.seeking = true;
    renderTime();
  });
  el.seek.addEventListener("change", function () {
    if (isFinite(audio.duration)) audio.currentTime = (el.seek.value / 1000) * audio.duration;
    state.seeking = false;
    renderTime();
  });

  window.addEventListener("pagehide", function () { savePos(true); });
  window.addEventListener("resize", setPlayerHeight);

  // ---------- init ----------
  (function init() {
    var idx = -1;
    try { idx = parseInt(localStorage.getItem("cc:speed"), 10); } catch (e) { /* storage blocked */ }
    if (idx >= 0 && idx < cfg.speeds.length) state.speedIdx = idx;
    bindMediaSession();
    renderBar();
  })();

  window.CCPlayer = {
    /** Sets the edition id (e.g. "09112026") used for resume positions and cache-busting. */
    setEdition: function (edition, label) {
      state.edition = edition || "";
      state.editionLabel = label || "";
      preloadDurations();
      emit();
    },
    playTrack: playTrack,
    playAll: playAll,
    progress: progress,
    onChange: function (fn) { listeners.push(fn); },
    onError: function (fn) { errorHandler = fn; },
    format: fmt
  };
})();
