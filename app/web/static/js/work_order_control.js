"use strict";
// Search narrows a bound select; the server still validates every selected ID.
(function () {
  function searchSelect(input) {
    var select=document.getElementById(input.dataset.searchSelect);
    if (!select) return;
    input.addEventListener('input',function () {
      var query=input.value.trim().toLocaleLowerCase('ru');
      Array.from(select.options).forEach(function (option) {
        option.hidden=!!option.value && !option.selected && option.text.toLocaleLowerCase('ru').indexOf(query)<0;
      });
    });
  }
  document.querySelectorAll('[data-search-select]').forEach(searchSelect);
  var form=document.getElementById('work-order-transfer-form');
  if (!form) return;
  var employer=form.querySelector('[name="contractor_id"]'),next=0;
  function filterCrew(row) {
    var specialty=row.querySelector('[data-crew-field="specialty"]'),search=row.querySelector('[data-crew-search]');
    var query=search.value.trim().toLocaleLowerCase('ru');
    Array.from(row.querySelector('[data-crew-field="employee"]').options).forEach(function(o) {
      var allowed=(!o.dataset.contractor || o.dataset.contractor===employer.value) && (!specialty.value || o.dataset.specialty===specialty.value);
      o.disabled=!allowed;
      o.hidden=!allowed || (!o.selected && o.text.toLocaleLowerCase('ru').indexOf(query)<0);
      if (!allowed) o.selected=false;
    });
  }
  function bindCrew(row) {
    row.querySelector('[data-remove-crew]').addEventListener('click',function(){row.remove();});
    row.querySelector('[data-crew-field="specialty"]').addEventListener('change',function(){filterCrew(row);});
    row.querySelector('[data-crew-search]').addEventListener('input',function(){filterCrew(row);});
    filterCrew(row);
    next=Math.max(next,Number(row.querySelector('[name="crew_row"]').value)+1);
  }
  form.querySelectorAll('[data-transfer-crew]').forEach(bindCrew);
  employer.addEventListener('change',function () {
    form.querySelectorAll('[data-transfer-crew]').forEach(filterCrew);
    ['brigade_id','foreman_employee_id'].forEach(function(name) {
      Array.from(form.querySelector('[name="'+name+'"]').options).forEach(function(o) {
        var allowed=!o.value || !o.dataset.contractor || o.dataset.contractor===employer.value;
        o.disabled=!allowed;o.hidden=!allowed;
        if (!allowed) o.selected=false;
      });
    });
  });
  var generation=0,date=form.querySelector('[name="shift_date"]'),status=document.getElementById('transfer-worker-status');
  function replacePeople(select,people,crew) {
    var chosen=Array.from(select.selectedOptions).map(function(o){return o.value;});
    select.replaceChildren();
    if (!crew) select.add(new Option('Указать до выдачи',''));
    people.forEach(function(e) {
      var option=new Option('ID '+e.id+' · '+e.full_name+' · '+(e.specialty_name || ''),String(e.id));
      option.dataset.contractor=e.contractor_id || '';option.dataset.specialty=e.specialty_id || '';
      option.selected=chosen.indexOf(String(e.id))>=0;select.add(option);
    });
  }
  date.addEventListener('change',function () {
    var current=++generation;status.textContent='Обновление доступности на дату смены…';
    fetch('/personnel/available-workers.json?as_of='+encodeURIComponent(date.value),{credentials:'same-origin'}).then(function(response) {
      if (!response.ok) throw new Error('Не удалось обновить список. Проверьте дату и откройте форму заново.');
      return response.json();
    }).then(function(data) {
      if (current!==generation) return;
      if (String(data.project_id)!==form.querySelector('[name="workspace_id"]').value) throw new Error('Объект изменён. Откройте форму заново.');
      form.querySelectorAll('[data-crew-field="employee"]').forEach(function(select){replacePeople(select,data.employees,true);});
      replacePeople(document.getElementById('transfer-crew-template').content.querySelector('[data-crew-field="employee"]'),data.employees,true);
      ['issued_by_employee_id','foreman_employee_id'].forEach(function(name){replacePeople(form.querySelector('[name="'+name+'"]'),data.employees,false);});
      employer.dispatchEvent(new Event('change'));
      status.textContent='Работники обновлены на '+data.as_of+'. Проверьте выбранный состав. Постоянные бригады будут раскрыты на эту дату при сохранении.';
    }).catch(function(error){if (current===generation) status.textContent=error.message;});
  });
  employer.dispatchEvent(new Event('change'));
  document.getElementById('add-transfer-crew').addEventListener('click',function () {
    var fragment=document.getElementById('transfer-crew-template').content.cloneNode(true),row=fragment.querySelector('[data-transfer-crew]'),index=next++;
    row.querySelector('[name="crew_row"]').value=index;
    row.querySelectorAll('[data-crew-field]').forEach(function(input){input.name='crew_'+input.dataset.crewField+'_'+index;});
    document.getElementById('transfer-crew').appendChild(fragment);bindCrew(row);
  });
}());
