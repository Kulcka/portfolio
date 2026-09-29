/*
 * Общий скрипт демо-лендингов: мобильное меню, плавная навигация с подсветкой
 * текущего раздела, появление блоков при прокрутке, маска телефона и
 * демо-обработчик формы заявки (никуда не отправляет — показывает сообщение).
 * Без зависимостей, работает в современных браузерах; без JS страница остаётся рабочей.
 */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- Мобильное меню ---------- */
  var toggle = document.querySelector("[data-menu-toggle]");
  var menu = document.getElementById(toggle ? toggle.getAttribute("aria-controls") : "");

  function setMenu(open) {
    if (!toggle || !menu) return;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Закрыть меню" : "Открыть меню");
    menu.classList.toggle("is-open", open);
    document.body.classList.toggle("menu-open", open);
  }

  if (toggle && menu) {
    toggle.addEventListener("click", function () {
      setMenu(toggle.getAttribute("aria-expanded") !== "true");
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
        setMenu(false);
        toggle.focus();
      }
    });
    window.matchMedia("(min-width: 900px)").addEventListener("change", function (mq) {
      if (mq.matches) setMenu(false);
    });
  }

  /* ---------- Плавная навигация по якорям ---------- */
  document.addEventListener("click", function (e) {
    var link = e.target.closest('a[href^="#"]');
    if (!link) return;
    var id = link.getAttribute("href").slice(1);
    var target = id ? document.getElementById(id) : null;
    if (!target) return;
    e.preventDefault();
    setMenu(false);
    target.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    if (history.replaceState) history.replaceState(null, "", "#" + id);
    // переносим фокус для клавиатуры и экранных дикторов
    if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: true });
  });

  /* ---------- Подсветка текущего раздела в меню ---------- */
  var navLinks = Array.prototype.slice.call(document.querySelectorAll("[data-nav] a[href^='#']"));
  if ("IntersectionObserver" in window && navLinks.length) {
    var byId = {};
    navLinks.forEach(function (a) { byId[a.getAttribute("href").slice(1)] = a; });
    var spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        navLinks.forEach(function (a) { a.removeAttribute("aria-current"); });
        var active = byId[entry.target.id];
        if (active) active.setAttribute("aria-current", "true");
      });
    }, { rootMargin: "-45% 0px -50% 0px" });
    Object.keys(byId).forEach(function (id) {
      var section = document.getElementById(id);
      if (section) spy.observe(section);
    });
  }

  /* ---------- Появление блоков при прокрутке ---------- */
  var reveal = document.querySelectorAll("[data-reveal]");
  if (!reduceMotion && "IntersectionObserver" in window) {
    document.documentElement.classList.add("js-reveal");
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px" });
    reveal.forEach(function (el) { io.observe(el); });
  }

  /* ---------- Маска телефона +7 (000) 000-00-00 ---------- */
  function formatPhone(value) {
    var digits = value.replace(/\D/g, "");
    if (digits.charAt(0) === "8" || digits.charAt(0) === "7") digits = digits.slice(1);
    digits = digits.slice(0, 10);
    var out = "+7";
    if (digits.length) out += " (" + digits.slice(0, 3);
    if (digits.length >= 3) out += ")";
    if (digits.length > 3) out += " " + digits.slice(3, 6);
    if (digits.length > 6) out += "-" + digits.slice(6, 8);
    if (digits.length > 8) out += "-" + digits.slice(8, 10);
    return out;
  }

  document.querySelectorAll('input[type="tel"]').forEach(function (input) {
    input.addEventListener("input", function () {
      input.value = input.value ? formatPhone(input.value) : "";
    });
    input.addEventListener("focus", function () {
      if (!input.value) input.value = "+7 (";
    });
    input.addEventListener("blur", function () {
      if (input.value.replace(/\D/g, "").length <= 1) input.value = "";
    });
  });

  /* ---------- Демо-обработчик формы заявки ---------- */
  var messages = {
    valueMissing: "Заполните это поле",
    typeMismatch: "Проверьте формат",
    patternMismatch: "Номер в формате +7 (000) 000-00-00",
    tooShort: "Слишком коротко",
    rangeUnderflow: "Значение меньше допустимого",
    rangeOverflow: "Значение больше допустимого"
  };

  function fieldError(field) {
    var v = field.validity;
    if (v.valid) return "";
    if (field.type === "checkbox" && v.valueMissing) return "Нужно согласие, чтобы отправить заявку";
    for (var key in messages) {
      if (v[key]) return messages[key];
    }
    return "Проверьте поле";
  }

  function showError(field) {
    var box = field.closest(".field") || field.parentElement;
    var hint = box.querySelector(".field-error");
    var text = fieldError(field);
    if (!hint) return !text;
    hint.textContent = text;
    field.setAttribute("aria-invalid", text ? "true" : "false");
    return !text;
  }

  document.querySelectorAll("form[data-demo-form]").forEach(function (form) {
    var status = form.querySelector("[data-form-status]");
    form.setAttribute("novalidate", "");

    form.querySelectorAll("input, select, textarea").forEach(function (field) {
      field.addEventListener("blur", function () {
        if (field.value || field.type === "checkbox") showError(field);
      });
      field.addEventListener("input", function () {
        if (field.getAttribute("aria-invalid") === "true") showError(field);
      });
    });

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      // ловушка для ботов: скрытое поле должно остаться пустым
      var trap = form.querySelector("[data-honeypot]");
      if (trap && trap.value) return;

      var fields = Array.prototype.slice.call(form.querySelectorAll("input:not([data-honeypot]), select, textarea"));
      var firstBad = null;
      fields.forEach(function (field) {
        if (!showError(field) && !firstBad) firstBad = field;
      });
      if (firstBad) {
        firstBad.focus();
        if (status) {
          status.className = "form-status is-error";
          status.textContent = "Проверьте отмеченные поля.";
        }
        return;
      }

      var btn = form.querySelector('[type="submit"]');
      if (btn) btn.disabled = true;
      if (status) {
        status.className = "form-status";
        status.textContent = "Отправляем…";
      }
      // имитация запроса к серверу; в рабочей версии здесь fetch() на почту, в Telegram или Google Таблицу
      window.setTimeout(function () {
        var name = (form.elements.name && form.elements.name.value.trim()) || "";
        if (status) {
          status.className = "form-status is-ok";
          status.textContent = (name ? name + ", спасибо! " : "Спасибо! ") +
            "Это демо: заявка никуда не отправлена. В рабочей версии она придёт владельцу на почту, в Telegram или в Google Таблицу.";
        }
        form.reset();
        fields.forEach(function (field) { field.removeAttribute("aria-invalid"); });
        if (btn) btn.disabled = false;
      }, 700);
    });
  });

  /* ---------- Текущий год в подвале ---------- */
  document.querySelectorAll("[data-year]").forEach(function (el) {
    el.textContent = String(new Date().getFullYear());
  });
})();
