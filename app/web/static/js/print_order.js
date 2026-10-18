"use strict";
document.querySelectorAll("[data-print-order]").forEach(function (button) {
  button.addEventListener("click", function () { window.print(); });
});
