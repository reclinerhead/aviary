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
    { tile: "tile-aviary", url: "/aviary/cover" },
    { tile: "tile-quarry", url: "/quarry/api/cover" },
    { tile: "tile-conservatory", url: "/conservatory/api/cover" },
  ];

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

  // Caddy answers /porch/station.json with this box's file under
  // porch/stations/, by hostname. A box with no file is "Home", unnamed.
  function loadStation() {
    getJson("/porch/station.json", COVER_TIMEOUT_MS)
      .then(function (data) {
        var station = cleanString(data && data.station);
        if (station) {
          document.getElementById("station").textContent = station;
          document.title = station;
        }
        setGreeting(cleanString(data && data.greetingName));
      })
      .catch(function () {});
  }

  // A cover is a path on this box and nothing else: the page makes no
  // request that leaves the station.
  function isLocalPath(src) {
    return src.charAt(0) === "/" && src.charAt(1) !== "/" && src.charAt(1) !== "\\";
  }

  function loadCover(cover) {
    var tile = document.getElementById(cover.tile);
    if (!tile) return;
    getJson(cover.url, COVER_TIMEOUT_MS)
      .then(function (data) {
        var src = cleanString(data && data.src);
        if (!src || !isLocalPath(src)) return;
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
  loadStation();
  COVERS.forEach(loadCover);
})();
