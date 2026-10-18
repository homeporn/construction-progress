"use strict";
// Interface selection only. Quantities and document validation stay on the server.
(function () {
  var form = document.getElementById("work-order-form");
  if (!form) return;
  function filter(select, predicate) {
    Array.from(select.options).forEach(function (option) {
      var allowed = !option.value || predicate(option);
      option.disabled = !allowed; option.hidden = !allowed;
      if (!allowed && option.selected) select.value = "";
    });
  }
  function bindRemove(row) {
    row.querySelector("[data-remove-row]").addEventListener("click", function () { row.remove(); });
  }
  function bindLine(row) {
    var building = row.querySelector("[data-building]"), floor = row.querySelector("[data-floor]");
    var element = row.querySelector("[data-element]"), type = row.querySelector("[data-work-type]");
    var work = row.querySelector("[data-work-item]");
    var options = Array.from(work.options).filter(function (o) { return o.value; });
    function matches(o, withElement, withType) {
      return (!building.value || o.dataset.building === building.value) &&
        (!floor.value || o.dataset.floor === floor.value) &&
        (!withElement || !element.value || o.dataset.element === element.value) &&
        (!withType || !type.value || o.dataset.workType === type.value);
    }
    function update() {
      filter(floor, function (o) { return !building.value || o.dataset.building === building.value; });
      filter(element, function (o) { return options.some(function (w) { return matches(w, false, false) && w.dataset.element === o.value; }); });
      filter(type, function (o) { return options.some(function (w) { return matches(w, true, false) && w.dataset.workType === o.value; }); });
      filter(work, function (o) { return matches(o, true, true); });
      row.querySelector("span[data-unit]").textContent = work.selectedIndex >= 0 ? (work.options[work.selectedIndex].dataset.unit || "—") : "—";
    }
    [building, floor, element, type, work].forEach(function (select) { select.addEventListener("change", update); });
    bindRemove(row); update();
  }
  var contractor = form.querySelector('[name="contractor_id"]');
  function updateCrew(row) {
    var specialty = row.querySelector('[data-crew-field="specialty"]');
    var employees = row.querySelector('[data-crew-field="employee"]');
    Array.from(employees.options).forEach(function (o) {
      var allowed = (!specialty.value || o.dataset.specialty === specialty.value) &&
        (!o.dataset.contractor || !contractor.value || o.dataset.contractor === contractor.value);
      o.disabled = !allowed; o.hidden = !allowed;
      if (!allowed) o.selected = false;
    });
  }
  function bindCrew(row) {
    row.querySelector('[data-crew-field="specialty"]').addEventListener("change", function () { updateCrew(row); });
    bindRemove(row); updateCrew(row);
  }
  contractor.addEventListener("change", function () {
    form.querySelectorAll("[data-crew-row]").forEach(updateCrew);
    var foreman = form.querySelector('[name="foreman_employee_id"]');
    filter(foreman, function (o) { return o.dataset.active !== "0" && (!o.dataset.contractor || !contractor.value || o.dataset.contractor === contractor.value); });
  });
  form.querySelectorAll("[data-order-line]").forEach(bindLine);
  form.querySelectorAll("[data-crew-row]").forEach(bindCrew);
  var nextIndex = form.querySelectorAll("[data-crew-row]").length;
  document.getElementById("add-order-line").addEventListener("click", function () {
    var fragment = document.getElementById("order-line-template").content.cloneNode(true);
    var row = fragment.querySelector("[data-order-line]");
    document.getElementById("order-lines").appendChild(fragment); bindLine(row);
  });
  document.getElementById("add-order-crew").addEventListener("click", function () {
    var fragment = document.getElementById("order-crew-template").content.cloneNode(true);
    var row = fragment.querySelector("[data-crew-row]"), index = nextIndex++;
    row.querySelector('[name="crew_row"]').value = index;
    row.querySelectorAll("[data-crew-field]").forEach(function (input) { input.name = "crew_" + input.dataset.crewField + "_" + index; });
    document.getElementById("order-crew").appendChild(fragment); bindCrew(row);
  });
}());
