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
  var element = form.querySelector("select[data-element]");
  var workType = form.querySelector("select[data-work-type]");
  var pendingHint = form.querySelector("[data-pending-hint]");
  var order = form.querySelector('select[data-order-line]');
  var orderHint = form.querySelector('p[data-order-hint]');

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
        (!floor || !floor.value || option.getAttribute("data-floor") === floor.value) &&
        (!element || !element.value || option.getAttribute("data-element") === element.value) &&
        (!workType || !workType.value || option.getAttribute("data-work-type") === workType.value);
    });
    filterOptions(correction, function (option) {
      return work && work.value && option.getAttribute("data-item") === work.value;
    });
    filterOptions(order, function (option) { return work && work.value && option.getAttribute('data-item') === work.value; });
    if (order) {
      order.disabled = Boolean(correction && correction.value) || Boolean(correction && correction.required);
      if (orderHint) {
        var origin = correction && correction.selectedIndex >= 0 ? correction.options[correction.selectedIndex] : null;
        var chosen = order.selectedIndex >= 0 ? order.options[order.selectedIndex] : null;
        orderHint.textContent = order.disabled ? 'Связь корректировки: ' + (origin && origin.dataset.orderLabel || 'выберите исходное событие') + '. Исходное событие не изменяется.' :
          chosen && chosen.value ? chosen.dataset.orderHint : 'Без наряда. Общие объёмы работы учитываются независимо от выбора документа.';
      }
    }
    if (pendingHint) {
      var selected = work && work.selectedIndex >= 0 ? work.options[work.selectedIndex] : null;
      // Display the server's exact result. Never compute Decimal quantities in JS.
      pendingHint.textContent = selected && selected.value ? selected.getAttribute("data-pending-label") : "Выберите работу";
    }
  }
  [building, floor, element, workType, work, order, correction].forEach(function (select) {
    if (select) select.addEventListener("change", update);
  });
  update();
});

document.querySelectorAll("form[data-project-create]").forEach(function (form) {
  var count = form.querySelector("[data-building-count]");
  var body = form.querySelector("[data-building-rows]");
  var generation = 0;
  function updateRows() {
    var target = Number(count.value);
    if (!Number.isSafeInteger(target) || target < 0) return;
    var currentGeneration = ++generation;
    function batch() {
      if (currentGeneration !== generation) return;
      var steps = 0;
      while (body.rows.length > target && steps++ < 100) body.deleteRow(body.rows.length - 1);
      while (body.rows.length < target && steps++ < 100) {
        var number = body.rows.length + 1;
        var row = body.insertRow();
        [["building_code", "К" + number, 80, "Код"], ["building_name", "Корпус " + number, 200, "Название"]].forEach(function (field) {
          var input = document.createElement("input");
          input.name = field[0]; input.value = field[1]; input.maxLength = field[2]; input.required = true;
          input.setAttribute("aria-label", field[3] + " корпуса " + number);
          row.insertCell().appendChild(input);
        });
      }
      if (body.rows.length !== target) window.requestAnimationFrame(batch);
    }
    batch();
  }
  count.addEventListener("input", updateRows);
});

document.querySelectorAll("select[data-reset-version]").forEach(function (select) {
  select.addEventListener("change", function () {
    var version = select.form.querySelector('select[name="version_id"]');
    if (version) version.value = "";
  });
});

// Analytics dependency resets only; all numerical work stays on the server.
document.querySelectorAll('form[data-analytics-filters]').forEach(function (form) {
  var building = form.querySelector('[name="building_id"]');
  var direction = form.querySelector('[name="discipline"]');
  var group = form.querySelector('[name="group_id"]');
  if (building) building.addEventListener('change', function () { form.querySelector('[name="floor_id"]').value = ''; });
  if (direction) direction.addEventListener('change', function () {
    form.querySelector('[name="version_id"]').value = '';
    group.value = ''; form.querySelector('[name="group_version"]').value = '';
  });
  if (group) group.addEventListener('change', function () { form.querySelector('[name="group_version"]').value = ''; });
});
