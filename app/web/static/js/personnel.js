"use strict";
(function () {
  var share=document.getElementById("share-contractor-form");
  if (share) share.addEventListener("submit",function (e) {
    var id=share.querySelector('[name="share_contractor_id"]').value;
    if (!id) { e.preventDefault(); return; }
    share.action="/personnel/contractors/"+encodeURIComponent(id)+"/share";
  });
  var employer=document.querySelector('form [name="contractor_id"]');
  var brigade=document.querySelector('form [name="brigade_id"]');
  if (employer && brigade) {
    function update() {
      Array.from(brigade.options).forEach(function (o) {
        var allowed=!o.value || !employer.value || o.dataset.contractor===employer.value;
        o.hidden=!allowed; o.disabled=!allowed;
        if (!allowed && o.selected) brigade.value="";
      });
    }
    employer.addEventListener("change",update); update();
  }
}());
