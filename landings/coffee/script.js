/* Бронь столика: нельзя выбрать прошедшую дату, по умолчанию — сегодня */
(function () {
  "use strict";
  var date = document.getElementById("f-date");
  if (!date) return;
  var now = new Date();
  var pad = function (n) { return String(n).padStart(2, "0"); };
  var today = now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate());
  date.min = today;
  date.defaultValue = today; // сохраняется и после сброса формы
})();
