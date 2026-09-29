/*
 * Калькулятор стоимости ремонта: площадь × ставка вида ремонта + доп. работы.
 * Кнопка «Отправить расчёт в заявку» переносит итог в комментарий формы и прокручивает к ней.
 */
(function () {
  "use strict";
  var form = document.getElementById("calc-form");
  if (!form) return;

  var range = document.getElementById("c-area-range");
  var area = document.getElementById("c-area");
  var sumEl = document.getElementById("c-sum");
  var weeksEl = document.getElementById("c-weeks");
  var fmt = new Intl.NumberFormat("ru-RU");

  function clampArea(v) {
    var n = Math.round(Number(v));
    if (!isFinite(n)) n = 55;
    return Math.min(200, Math.max(20, n));
  }

  function state() {
    var m2 = clampArea(area.value);
    var kind = form.querySelector('input[name="kind"]:checked');
    var rate = Number(kind.value);
    var total = m2 * rate;
    var extras = [];
    form.querySelectorAll('input[name="extra"]:checked').forEach(function (cb) {
      var price = Number(cb.value) * (cb.hasAttribute("data-per-m2") ? m2 : 1);
      total += price;
      extras.push(cb.parentElement.textContent.trim());
    });
    // срок: примерно неделя на каждые 12 м² для косметики, дольше для сложных видов
    var k = rate >= 14000 ? 2.6 : rate >= 9800 ? 1.9 : 1;
    var weeks = Math.max(2, Math.round((m2 / 12) * k));
    return { m2: m2, kindName: kind.parentElement.textContent.trim(), total: total, extras: extras, weeks: weeks };
  }

  function render() {
    var s = state();
    sumEl.textContent = fmt.format(Math.round(s.total / 500) * 500);
    weeksEl.textContent = s.weeks + "–" + (s.weeks + 1) + " недель";
  }

  range.addEventListener("input", function () { area.value = range.value; render(); });
  area.addEventListener("input", function () {
    if (area.value !== "") range.value = clampArea(area.value);
    render();
  });
  area.addEventListener("change", function () { area.value = clampArea(area.value); range.value = area.value; render(); });
  form.addEventListener("change", render);
  form.addEventListener("submit", function (e) { e.preventDefault(); });

  document.getElementById("c-apply").addEventListener("click", function () {
    var s = state();
    var comment = document.getElementById("f-comment");
    comment.value = "Расчёт с сайта: " + s.kindName + ", " + s.m2 + " м²" +
      (s.extras.length ? "; дополнительно: " + s.extras.join(", ") : "") +
      ". Примерно " + sumEl.textContent + " ₽.";
    var target = document.getElementById("request");
    var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ behavior: reduce ? "auto" : "smooth" });
    document.getElementById("f-name").focus({ preventScroll: true });
  });

  render();
})();
