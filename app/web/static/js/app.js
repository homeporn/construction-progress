"use strict";

// Only interface behavior. All volume, labor and progress calculations are server-side.
document.querySelectorAll("form[data-confirm]").forEach(function (form) {
  form.addEventListener("submit", function (event) {
    if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
  });
});

document.querySelectorAll("form[data-dependent-form]").forEach(function (form) {
  var building = form.querySelector("select[data-building]");
  var floor = form.querySelector("select[data-floor]");
  var work = form.querySelector("select[data-work-item]");
  var correction = form.querySelector("select[data-correction]");

  function filterOptions(select, predicate) {
    if (!select) return;
    Array.from(select.options).forEach(function (option) {
      var available = !option.value || predicate(option);
      option.disabled = !available;
      option.hidden = !available;
      if (!available && option.selected) select.value = "";
    });
  }

  function update() {
    filterOptions(floor, function (option) {
      return !building || !building.value || option.getAttribute("data-building") === building.value;
    });
    filterOptions(work, function (option) {
      return (!building || !building.value || option.getAttribute("data-building") === building.value) &&
        (!floor || !floor.value || option.getAttribute("data-floor") === floor.value);
    });
    filterOptions(correction, function (option) {
      return work && work.value && option.getAttribute("data-item") === work.value;
    });
  }
  [building, floor, work].forEach(function (select) {
    if (select) select.addEventListener("change", update);
  });
  update();
});

document.querySelectorAll("select[data-reset-version]").forEach(function (select) {
  select.addEventListener("change", function () {
    var version = select.form.querySelector('select[name="version_id"]');
    if (version) version.value = "";
  });
});
