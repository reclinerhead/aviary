// The porch (#696): the date, the greeting, the station's name, and this
// week's photo on each tile. The page is finished without any of it: every
// failure here leaves the default in place and says nothing.

(function () {
  "use strict";

  var COVER_TIMEOUT_MS = 4000;

  // Each app answers { src, caption, credit } for this week's photo. The
  // routes are the apps' own (aviary #697 and its twins); this page ships
  // before them.
  var COVERS = [
    { app: "aviary", tile: "tile-aviary", url: "/aviary/cover" },
    { app: "quarry", tile: "tile-quarry", url: "/quarry/api/cover" },
    { app: "conservatory", tile: "tile-conservatory", url: "/conservatory/api/cover" },
  ];

  // An origin and nothing after it: scheme, host, optional port.
  var ORIGIN = /^https?:\/\/[a-z0-9.-]+(:\d+)?$/i;

  function partOfDay(hour) {
    if (hour < 12) return "morning";
    if (hour < 17) return "afternoon";
    return "evening";
  }

  function setGreeting(name) {
    var text = "Good " + partOfDay(new Date().getHours());
    document.getElementById("greeting").textContent =
      name ? text + ", " + name + "." : text + ".";
  }

  function setDate() {
    document.getElementById("today").textContent =
      new Date().toLocaleDateString("en-US", {
        weekday: "long",
        month: "long",
        day: "numeric",
      });
  }

  function cleanString(value) {
    return typeof value === "string" && value.trim() ? value.trim() : null;
  }

  function getJson(url, timeoutMs) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, timeoutMs);
    return fetch(url, { signal: controller.signal, headers: { accept: "application/json" } })
      .then(function (res) {
        if (!res.ok) throw new Error("not ok");
        return res.json();
      })
      .finally(function () { clearTimeout(timer); });
  }

  // Where an app lives when it is not under this origin (#700). On a station
  // all three share the box's one origin and the file says nothing. At the
  // house the Quarry and the Conservatory each have a name of their own, and
  // the station file's `apps` gives it: { "quarry": "https://quarry.lan" }.
  // Anything that is not a bare origin is ignored, and the tile keeps its path.
  function appOrigins(data) {
    var origins = {};
    var apps = data && typeof data.apps === "object" && data.apps;
    if (!apps) return origins;
    COVERS.forEach(function (cover) {
      var origin = cleanString(apps[cover.app]);
      if (origin && ORIGIN.test(origin)) origins[cover.app] = origin;
    });
    return origins;
  }

  // Caddy answers /porch/station.json with this box's file under
  // porch/stations/, by hostname. A box with no file is "Home", unnamed.
  // Resolves to the app origins the file names, or none.
  function loadStation() {
    return getJson("/porch/station.json", COVER_TIMEOUT_MS)
      .then(function (data) {
        var station = cleanString(data && data.station);
        if (station) {
          document.getElementById("station").textContent = station;
          document.title = station;
        }
        setGreeting(cleanString(data && data.greetingName));
        return appOrigins(data);
      })
      .catch(function () { return {}; });
  }

  // A cover is a path and nothing else, never a full URL: the page loads
  // photos only from this box's own apps.
  function isLocalPath(src) {
    return src.charAt(0) === "/" && src.charAt(1) !== "/" && src.charAt(1) !== "\\";
  }

  // The cover request is always to this origin (at the house Caddy passes
  // the twins' on). The photo is a path too, read against the app's own
  // origin when it has one: the twins there answer /media/..., at the root
  // of their own name.
  function loadCover(cover, origin) {
    var tile = document.getElementById(cover.tile);
    if (!tile) return;
    if (origin) tile.href = origin + "/";
    getJson(cover.url, COVER_TIMEOUT_MS)
      .then(function (data) {
        var src = cleanString(data && data.src);
        if (!src || !isLocalPath(src)) return;
        if (origin) src = origin + src;
        var caption = cleanString(data.caption);
        var credit = cleanString(data.credit);
        // Swap only once the photo has loaded, so a tile is never empty.
        var probe = new Image();
        probe.onload = function () {
          var photo = tile.querySelector(".tile-photo");
          var dab = tile.querySelector(".dab");
          var creditEl = tile.querySelector(".dab-credit");
          photo.src = src;
          photo.classList.add("is-live");
          tile.querySelector(".dab-caption").textContent = caption || "";
          creditEl.textContent = credit || "";
          creditEl.hidden = !credit;
          dab.hidden = !caption && !credit;
        };
        probe.src = src;
      })
      .catch(function () {});
  }

  setDate();
  setGreeting(null);
  loadStation().then(function (origins) {
    COVERS.forEach(function (cover) { loadCover(cover, origins[cover.app]); });
  });
})();
