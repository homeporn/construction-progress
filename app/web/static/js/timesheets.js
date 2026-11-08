(function () {
  'use strict';
  var editor = document.querySelector('#cell-editor form');
  if (editor) {
    document.querySelectorAll('.ts-cell').forEach(function (button) {
      button.addEventListener('click', function () {
        editor.elements.employee_id.value = button.dataset.employee;
        editor.elements.work_date.value = button.dataset.date;
        editor.elements.code.value = button.dataset.code || 'Я';
        editor.elements.hours.value = button.dataset.hours || '';
        editor.elements.comment.value = button.dataset.comment || '';
        document.getElementById('cell-editor').scrollIntoView({ block: 'center', behavior: 'smooth' });
        editor.elements.hours.focus({ preventScroll: true });
      });
    });
  }
  document.querySelectorAll('[data-select-employees]').forEach(function (button) {
    button.addEventListener('click', function () {
      button.closest('form').querySelectorAll('input[name="employee_id"]').forEach(function (input) {
        input.checked = true;
      });
    });
  });
  var dirty = false;
  document.querySelectorAll('[data-unsaved-warning]').forEach(function (form) {
    form.addEventListener('change', function () { dirty = true; });
    form.addEventListener('input', function () { dirty = true; });
  });
  document.querySelectorAll('form').forEach(function (form) {
    form.addEventListener('submit', function () { dirty = false; });
  });
  window.addEventListener('beforeunload', function (event) {
    if (dirty) { event.preventDefault(); event.returnValue = ''; }
  });
}());

// Keep explicitly selected brigade members across roster pages. IDs are
// revalidated by the repository when the preview is created/applied.
(function () {
  'use strict';
  var form = document.querySelector('form.ts-bulk-form');
  if (!form) return;
  var keys = ['workspace_id', 'version_id', 'revision', 'brigade_id'];
  var storageKey = 'cp-bulk-' + keys.map(function (key) {
    return form.elements[key] ? form.elements[key].value : '';
  }).join('-');
  var chosen = {};
  try { chosen = JSON.parse(sessionStorage.getItem(storageKey) || '{}'); } catch (_) { chosen = {}; }
  var inputs = Array.from(form.querySelectorAll('input[type="checkbox"][name="employee_id"]'));
  inputs.forEach(function (input) { input.checked = !!chosen[input.value]; });
  function remember() {
    inputs.forEach(function (input) {
      if (input.checked) chosen[input.value] = true; else delete chosen[input.value];
    });
    try { sessionStorage.setItem(storageKey, JSON.stringify(chosen)); } catch (_) {}
  }
  form.addEventListener('change', remember);
  form.addEventListener('submit', function () {
    remember();
    var visible = inputs.map(function (input) { return input.value; });
    Object.keys(chosen).filter(function (id) {
      return /^[0-9]{1,9}$/.test(id) && visible.indexOf(id) === -1;
    }).slice(0, 5000).forEach(function (id) {
      var input = document.createElement('input');
      input.type = 'hidden'; input.name = 'employee_id'; input.value = id;
      form.appendChild(input);
    });
  });
})();
