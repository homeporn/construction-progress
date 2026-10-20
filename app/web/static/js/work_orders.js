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
      var allowed = o.dataset.available !== "0" && (!specialty.value || o.dataset.specialty === specialty.value) &&
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
    filter(foreman, function (o) { return o.dataset.active !== "0" && o.dataset.available !== "0" && (!o.dataset.contractor || !contractor.value || o.dataset.contractor === contractor.value); });
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

// Permanent brigades are expanded and validated again inside the server's
// transaction. This preview never creates assignments, hours or volume events.
(function () {
  var form=document.getElementById('work-order-form');
  if (!form) return;
  var select=document.getElementById('permanent-brigades');
  var employer=form.querySelector('[name="contractor_id"]');
  var preview=document.getElementById('brigade-preview');
  var button=document.getElementById('preview-brigades');
  if (!select || !button) return;
  function filterBrigades() {
    Array.from(select.options).forEach(function(o) {
      var allowed=!!employer.value && o.dataset.contractor===employer.value;
      o.disabled=!allowed; o.hidden=!allowed;
      if (!allowed) o.selected=false;
    });
    preview.textContent='';
  }
  employer.addEventListener('change',filterBrigades); filterBrigades();
  button.addEventListener('click',function () {
    var ids=Array.from(select.selectedOptions).map(function(o) { return o.value; });
    var date=form.querySelector('[name="shift_date"]').value;
    preview.textContent=ids.length?'Загрузка текущего состава…':'Выберите бригаду';
    Promise.all(ids.map(function(id) {
      return fetch('/personnel/brigades/'+encodeURIComponent(id)+'/members.json?as_of='+encodeURIComponent(date),{credentials:'same-origin'}).then(function(r) {
        if (!r.ok) throw new Error('Не удалось получить состав. Обновите страницу и проверьте объект.');
        return r.json();
      });
    })).then(function(groups) {
      preview.textContent=groups.map(function(g) {
        return 'Бригада № '+g.number+': '+(g.members.map(function(e) { return e.full_name+' · '+e.specialty_name+' (ID '+e.id+')'; }).join('; ') || 'Нет доступных работников на эту дату');
      }).join(' | ');
    }).catch(function(error) { preview.textContent=error.message; });
  });
}());

(function () {
  var form=document.getElementById('work-order-form');
  if (!form) return;
  var date=form.querySelector('[name="shift_date"]'),employer=form.querySelector('[name="contractor_id"]');
  var status=document.getElementById('brigade-preview'),generation=0;
  function replace(select,people,kind) {
    var selected=Array.from(select.selectedOptions).map(function(o) {return o.value;});
    while (select.firstChild) select.removeChild(select.firstChild);
    if (kind!=='crew') select.add(new Option('Указать до выдачи',''));
    people.forEach(function(e) {
      var o=new Option('ID '+e.id+' · '+e.full_name+' · '+(e.specialty_name || 'Без специальности'),String(e.id));
      o.dataset.contractor=e.contractor_id || '';o.dataset.specialty=e.specialty_id || '';
      o.dataset.active='1';o.dataset.available='1';o.selected=selected.indexOf(String(e.id))>=0;
      select.add(o);
    });
  }
  date.addEventListener('change',function () {
    var current=++generation;
    status.textContent='Обновление доступности работников на дату смены…';
    fetch('/personnel/available-workers.json?as_of='+encodeURIComponent(date.value),{credentials:'same-origin'}).then(function(r) {
      if (!r.ok) throw new Error('Не удалось обновить работников. Проверьте дату и обновите страницу.');
      return r.json();
    }).then(function(data) {
      if (current!==generation) return;
      if (String(data.project_id)!==form.querySelector('[name="workspace_id"]').value) throw new Error('Объект изменён в другой вкладке. Обновите страницу.');
      form.querySelectorAll('[data-crew-field="employee"]').forEach(function(s) {replace(s,data.employees,'crew');});
      var template=document.getElementById('order-crew-template');
      replace(template.content.querySelector('[data-crew-field="employee"]'),data.employees,'crew');
      ['issued_by_employee_id','foreman_employee_id'].forEach(function(name) {replace(form.querySelector('[name="'+name+'"]'),data.employees,'header');});
      employer.dispatchEvent(new Event('change'));status.textContent='Список обновлён на '+data.as_of+'. Проверьте выбранный состав и ответственных.';
    }).catch(function(error) {if (current===generation) status.textContent=error.message;});
  });
}());
