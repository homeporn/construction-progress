"use strict";

(function () {
  var body = document.body;
  var sidebar = document.getElementById("application-sidebar");
  var toggle = document.querySelector("[data-navigation-toggle]");
  var form = document.getElementById("navigation-state-form");
  if (!sidebar || !toggle || !form) return;
  var media = window.matchMedia("(max-width: 900px)");
  var groups = Array.from(sidebar.querySelectorAll("details[data-nav-group]"));
  var status = document.getElementById("navigation-status");
  var timer = null;
  var saving = false;
  var changed = false;

  function expanded() {
    return media.matches ? body.classList.contains("nav-mobile-open") : !body.classList.contains("nav-collapsed");
  }
  function updateButton() {
    toggle.setAttribute("aria-expanded", expanded() ? "true" : "false");
    toggle.setAttribute("aria-label", (expanded() ? "Свернуть" : "Развернуть") + " боковую панель");
  }
  function save() {
    if (saving || !changed) return;
    changed = false;
    saving = true;
    var data = new URLSearchParams();
    data.append("csrf_token", form.elements.csrf_token.value);
    data.append("workspace_id", form.elements.workspace_id.value);
    data.append("collapsed", body.classList.contains("nav-collapsed") ? "1" : "0");
    groups.forEach(function (group) {
      if (group.open) data.append("opened", group.getAttribute("data-nav-group"));
    });
    // Serialize writes so a slower response cannot replace a newer session cookie.
    fetch(form.action, {method: "POST", credentials: "same-origin", body: data,
      headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}})
      .then(function (response) {
        if (!response.ok) throw new Error("navigation preferences");
        if (status) status.textContent = "";
      })
      .catch(function () {
        if (status) status.textContent = "Не удалось сохранить вид меню. Обновите страницу, если объект был изменён в другой вкладке.";
      })
      .then(function () { saving = false; if (changed) save(); });
  }
  function persist() {
    changed = true;
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(save, 120);
  }
  function closeMobile() {
    body.classList.remove("nav-mobile-open");
    updateButton();
  }
  toggle.addEventListener("click", function () {
    if (media.matches) body.classList.toggle("nav-mobile-open");
    else { body.classList.toggle("nav-collapsed"); persist(); }
    updateButton();
  });
  document.querySelector("[data-navigation-close]").addEventListener("click", closeMobile);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && body.classList.contains("nav-mobile-open")) { closeMobile(); toggle.focus(); }
  });
  groups.forEach(function (group) {
    group.querySelector("summary").addEventListener("click", function (event) {
      if (!media.matches && body.classList.contains("nav-collapsed")) {
        event.preventDefault();
        body.classList.remove("nav-collapsed");
        group.open = true;
        updateButton();
        persist();
      }
    });
    group.addEventListener("toggle", persist);
  });
  function resize() { closeMobile(); updateButton(); }
  if (media.addEventListener) media.addEventListener("change", resize);
  else media.addListener(resize); // Safari on older supported macOS.
  updateButton();
})();
