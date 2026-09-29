/* Переключатель срока абонемента: цены берутся из data-price-1 / data-price-6 */
(function () {
  "use strict";
  var group = document.querySelector("[data-period]");
  if (!group) return;
  group.addEventListener("change", function (e) {
    var months = e.target.value;
    document.querySelectorAll("[data-price-" + months + "]").forEach(function (el) {
      el.textContent = el.getAttribute("data-price-" + months);
    });
  });
})();
