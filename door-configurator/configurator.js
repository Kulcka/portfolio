/*
 * Конфигуратор входной двери: картинка из слоёв + расчёт цены.
 *
 * Файл состоит из двух частей:
 *  1. DoorConfiguratorCore — чистые функции без DOM (цена, совместимость, ссылка, проверка
 *     конфига, заявка). Их же проверяют тесты: node --test.
 *  2. Интерфейс — строится внутри элемента с атрибутом data-door-configurator по настройкам
 *     из window.DOOR_CONFIG (файл config.js).
 *
 * Без сборки и внешних библиотек; работает в современных браузерах.
 */

/* ======================================================================= */
/* 1. Логика                                                               */
/* ======================================================================= */
(function (root, factory) {
  'use strict';
  var core = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = core;
  } else {
    root.DoorConfiguratorCore = core;
  }
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';

  var TEMPLATE_KEY = /\{([A-Za-z0-9_-]+)\}/g;

  function hasOwn(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  function assign(target) {
    for (var i = 1; i < arguments.length; i++) {
      var src = arguments[i];
      if (!src) continue;
      for (var k in src) if (hasOwn(src, k)) target[k] = src[k];
    }
    return target;
  }

  function isFiniteNumber(v) {
    return typeof v === 'number' && isFinite(v);
  }

  function findGroup(config, groupId) {
    var groups = config.groups || [];
    for (var i = 0; i < groups.length; i++) if (groups[i].id === groupId) return groups[i];
    return null;
  }

  function findOption(group, optionId) {
    if (!group) return null;
    for (var i = 0; i < group.options.length; i++) if (group.options[i].id === optionId) return group.options[i];
    return null;
  }

  /** Выбор по умолчанию: { groupId: optionId }. */
  function defaultState(config) {
    var state = {};
    config.groups.forEach(function (g) {
      state[g.id] = g.default;
    });
    return state;
  }

  /** Причина, по которой вариант сейчас недоступен, или null. */
  function disabledReason(config, state, groupId, optionId) {
    var rules = config.rules || [];
    for (var i = 0; i < rules.length; i++) {
      var rule = rules[i];
      var list = rule.disable && rule.disable[groupId];
      if (!list || list.indexOf(optionId) < 0) continue;
      var when = rule.when || {};
      var matched = Object.keys(when).every(function (key) {
        return key !== groupId && when[key].indexOf(state[key]) >= 0;
      });
      if (matched) return rule.reason || 'Недоступно с выбранными опциями';
    }
    return null;
  }

  /**
   * Приводит выбор к совместимому: несовместимые варианты заменяются на вариант по умолчанию
   * (или первый доступный). lockedGroupId — группа, которую только что выбрал покупатель: её не трогаем.
   * Возвращает { state, changes: [{ groupId, from, to, reason }] }.
   */
  function resolveConflicts(config, state, lockedGroupId) {
    var next = assign({}, state);
    var changes = [];
    for (var pass = 0; pass <= config.groups.length; pass++) {
      var changed = false;
      config.groups.forEach(function (g) {
        if (g.id === lockedGroupId) return;
        var reason = disabledReason(config, next, g.id, next[g.id]);
        if (!reason) return;
        var candidates = [g.default].concat(
          g.options.map(function (o) {
            return o.id;
          })
        );
        var replacement = null;
        for (var i = 0; i < candidates.length; i++) {
          if (candidates[i] !== next[g.id] && !disabledReason(config, next, g.id, candidates[i])) {
            replacement = candidates[i];
            break;
          }
        }
        if (replacement === null) return;
        changes.push({ groupId: g.id, from: next[g.id], to: replacement, reason: reason });
        next[g.id] = replacement;
        changed = true;
      });
      if (!changed) break;
    }
    return { state: next, changes: changes };
  }

  /** Заполняет пропуски значениями по умолчанию, отбрасывает неизвестное, снимает конфликты. */
  function normalizeState(config, raw) {
    var state = {};
    raw = raw || {};
    config.groups.forEach(function (g) {
      var value = hasOwn(raw, g.id) ? raw[g.id] : undefined;
      state[g.id] = findOption(g, value) ? value : g.default;
    });
    return resolveConflicts(config, state, null).state;
  }

  /**
   * Выбор варианта покупателем. Возвращает { state, changes, blocked }:
   * blocked — причина, если вариант недоступен (тогда state не меняется).
   */
  function selectOption(config, state, groupId, optionId) {
    var group = findGroup(config, groupId);
    if (!group) throw new Error('Нет группы «' + groupId + '»');
    if (!findOption(group, optionId)) throw new Error('В группе «' + groupId + '» нет варианта «' + optionId + '»');
    var reason = disabledReason(config, state, groupId, optionId);
    if (reason) return { state: assign({}, state), changes: [], blocked: reason };
    var next = assign({}, state);
    next[groupId] = optionId;
    var resolved = resolveConflicts(config, next, groupId);
    return { state: resolved.state, changes: resolved.changes, blocked: null };
  }

  /** Цена: { base, items: [{ groupId, groupName, optionId, optionName, note, price }], total }. */
  function calcPrice(config, state) {
    var base = config.basePrice || 0;
    var total = base;
    var items = config.groups.map(function (g) {
      var option = findOption(g, state[g.id]) || findOption(g, g.default);
      var price = option.price || 0;
      total += price;
      return {
        groupId: g.id,
        groupName: g.name,
        optionId: option.id,
        optionName: option.name,
        note: option.note || '',
        price: price,
      };
    });
    return { base: base, items: items, total: total };
  }

  function groupDigits(n) {
    return String(Math.round(Math.abs(n))).replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0');
  }

  /** 52400 → «52 400 ₽» (неразрывные пробелы). */
  function formatPrice(value, currency) {
    return (value < 0 ? '−' : '') + groupDigits(value) + '\u00a0' + (currency || '₽');
  }

  /** Надбавка варианта: «+2 900 ₽», «в базе» для нуля. */
  function formatDelta(value, currency) {
    if (!value) return 'в базе';
    return (value > 0 ? '+' : '−') + groupDigits(value) + '\u00a0' + (currency || '₽');
  }

  function templateKeys(template) {
    var keys = [];
    if (typeof template !== 'string') return keys;
    template.replace(TEMPLATE_KEY, function (m, key) {
      if (keys.indexOf(key) < 0) keys.push(key);
      return m;
    });
    return keys;
  }

  /** "layers/coating/{size}/oak.svg" + { size: 'single' } → "layers/coating/single/oak.svg". */
  function resolveTemplate(template, state) {
    if (typeof template !== 'string') return null;
    return template.replace(TEMPLATE_KEY, function (m, key) {
      return hasOwn(state, key) ? state[key] : m;
    });
  }

  /** Путь картинки с учётом config.assetsBase (абсолютные адреса не трогаем). */
  function assetUrl(config, p) {
    if (p === null || p === undefined) return null;
    if (/^(?:[a-z][a-z0-9+.-]*:|\/)/i.test(p)) return p;
    return (config.assetsBase || '') + p;
  }

  /**
   * Слои для текущего выбора, снизу вверх:
   * [{ id, name, layer, path, src }], path/src = null — у варианта нет картинки.
   */
  function getLayers(config, state) {
    var list = [];
    (config.staticLayers || []).forEach(function (l) {
      var p = resolveTemplate(l.image, state);
      list.push({ id: 'static-' + l.id, name: l.name || l.id, layer: l.layer || 0, path: p, src: assetUrl(config, p) });
    });
    config.groups.forEach(function (g) {
      var option = findOption(g, state[g.id]) || findOption(g, g.default);
      var p = option && option.image ? resolveTemplate(option.image, state) : null;
      list.push({ id: g.id, name: g.name, layer: g.layer || 0, path: p, src: assetUrl(config, p) });
    });
    return list
      .map(function (item, index) {
        return { item: item, index: index };
      })
      .sort(function (a, b) {
        return a.item.layer - b.item.layer || a.index - b.index;
      })
      .map(function (x) {
        return x.item;
      });
  }

  /** Выбор → строка для адреса: "size=single&coating=graphite&…". */
  function serializeState(config, state) {
    return config.groups
      .map(function (g) {
        return encodeURIComponent(g.id) + '=' + encodeURIComponent(state[g.id]);
      })
      .join('&');
  }

  /** Строка из адреса ("#size=…&…" или "size=…") → выбор; мусор и несовместимое отбрасываются. */
  function parseState(config, text) {
    var raw = {};
    String(text || '')
      .replace(/^[#?]/, '')
      .split('&')
      .forEach(function (pair) {
        if (!pair) return;
        var eq = pair.indexOf('=');
        if (eq < 1) return;
        try {
          raw[decodeURIComponent(pair.slice(0, eq))] = decodeURIComponent(pair.slice(eq + 1).replace(/\+/g, ' '));
        } catch (e) {
          /* битая кодировка — пропускаем пару */
        }
      });
    return normalizeState(config, raw);
  }

  /** Все сочетания значений групп из шаблона → список путей. */
  function expandTemplate(config, template) {
    var keys = templateKeys(template).filter(function (k) {
      return !!findGroup(config, k);
    });
    var results = [template];
    keys.forEach(function (key) {
      var ids = findGroup(config, key).options.map(function (o) {
        return o.id;
      });
      var next = [];
      results.forEach(function (partial) {
        ids.forEach(function (id) {
          next.push(partial.split('{' + key + '}').join(id));
        });
      });
      results = next;
    });
    return results;
  }

  /** Все картинки, которые может показать конфигуратор (слои и миниатюры), без повторов. */
  function listImagePaths(config) {
    var seen = {};
    var out = [];
    function add(template) {
      if (typeof template !== 'string' || !template) return;
      expandTemplate(config, template).forEach(function (p) {
        if (!seen[p]) {
          seen[p] = true;
          out.push(p);
        }
      });
    }
    (config.staticLayers || []).forEach(function (l) {
      add(l.image);
    });
    (config.groups || []).forEach(function (g) {
      (g.options || []).forEach(function (o) {
        add(o.image);
        add(o.thumb);
      });
    });
    return out;
  }

  /** Проверка конфига. Возвращает список ошибок по-русски (пустой — всё в порядке). */
  function validateConfig(config) {
    var errors = [];
    function err(msg) {
      errors.push(msg);
    }
    if (!config || typeof config !== 'object') return ['Конфиг не найден: подключите config.js перед configurator.js'];
    if (!isFiniteNumber(config.basePrice)) err('basePrice должен быть числом');
    if (!config.canvas || !(config.canvas.width > 0) || !(config.canvas.height > 0)) {
      err('canvas: укажите width и height холста слоёв');
    }
    if (!Array.isArray(config.groups) || !config.groups.length) {
      err('groups: нужна хотя бы одна группа опций');
      return errors;
    }

    var groupIds = {};
    config.groups.forEach(function (g, gi) {
      var where = 'Группа ' + (g && g.id ? '«' + g.id + '»' : '№' + (gi + 1));
      if (!g || typeof g.id !== 'string' || !g.id) {
        err(where + ': нет id');
        return;
      }
      if (groupIds[g.id]) err(where + ': id повторяется');
      groupIds[g.id] = true;
      if (typeof g.name !== 'string' || !g.name) err(where + ': нет name');
      if (!isFiniteNumber(g.layer)) err(where + ': layer (порядок слоя) должен быть числом');
      if (!Array.isArray(g.options) || !g.options.length) {
        err(where + ': нет вариантов (options)');
        return;
      }
      var optionIds = {};
      g.options.forEach(function (o, oi) {
        var ow = where + ', вариант ' + (o && o.id ? '«' + o.id + '»' : '№' + (oi + 1));
        if (!o || typeof o.id !== 'string' || !o.id) {
          err(ow + ': нет id');
          return;
        }
        if (optionIds[o.id]) err(ow + ': id повторяется');
        optionIds[o.id] = true;
        if (typeof o.name !== 'string' || !o.name) err(ow + ': нет name');
        if (hasOwn(o, 'price') && !isFiniteNumber(o.price)) err(ow + ': price должен быть числом');
        if (o.image !== null && o.image !== undefined && typeof o.image !== 'string') {
          err(ow + ': image — путь к картинке или null');
        }
        if (o.thumb !== undefined && typeof o.thumb !== 'string') err(ow + ': thumb — путь к картинке');
        templateKeys(o.image).forEach(function (key) {
          if (key === g.id) err(ow + ': путь картинки ссылается на свою же группу {' + key + '}');
        });
      });
      if (g.default === undefined || g.default === null || g.default === '') {
        err(where + ': нет default (варианта по умолчанию)');
      } else if (!optionIds[g.default]) {
        err(where + ': default «' + g.default + '» нет среди вариантов');
      }
    });

    function checkKeys(template, where) {
      templateKeys(template).forEach(function (key) {
        if (!groupIds[key]) err(where + ': в пути {' + key + '} — нет такой группы');
      });
    }
    config.groups.forEach(function (g) {
      (g.options || []).forEach(function (o) {
        if (!o) return;
        checkKeys(o.image, 'Группа «' + g.id + '», вариант «' + o.id + '»');
        checkKeys(o.thumb, 'Группа «' + g.id + '», вариант «' + o.id + '», миниатюра');
      });
    });

    (config.staticLayers || []).forEach(function (l, i) {
      var where = 'Постоянный слой ' + (l && l.id ? '«' + l.id + '»' : '№' + (i + 1));
      if (!l || typeof l.image !== 'string' || !l.image) {
        err(where + ': нет image');
        return;
      }
      if (hasOwn(l, 'layer') && !isFiniteNumber(l.layer)) err(where + ': layer должен быть числом');
      checkKeys(l.image, where);
    });

    function checkRef(map, where) {
      if (!map || typeof map !== 'object') {
        err(where + ': нужен объект { группа: [варианты] }');
        return;
      }
      Object.keys(map).forEach(function (gid) {
        var group = findGroup(config, gid);
        if (!group) {
          err(where + ': нет группы «' + gid + '»');
          return;
        }
        if (!Array.isArray(map[gid]) || !map[gid].length) {
          err(where + ', «' + gid + '»: нужен непустой список вариантов');
          return;
        }
        map[gid].forEach(function (oid) {
          if (!findOption(group, oid)) err(where + ': в группе «' + gid + '» нет варианта «' + oid + '»');
        });
      });
    }
    (config.rules || []).forEach(function (rule, i) {
      var where = 'Правило №' + (i + 1);
      checkRef(rule.when, where + ' (when)');
      checkRef(rule.disable, where + ' (disable)');
      if (rule.when && rule.disable) {
        Object.keys(rule.disable).forEach(function (gid) {
          if (hasOwn(rule.when, gid)) err(where + ': группа «' + gid + '» не может быть и в when, и в disable');
        });
      }
    });

    if (!errors.length) {
      var def = defaultState(config);
      config.groups.forEach(function (g) {
        var reason = disabledReason(config, def, g.id, g.default);
        if (reason) err('Группа «' + g.id + '»: вариант по умолчанию несовместим с другими (' + reason + ')');
      });
    }
    return errors;
  }

  /** Проверка формы заявки. { valid, errors: { name?, phone?, comment? } } */
  function validateLead(form) {
    form = form || {};
    var errors = {};
    var name = String(form.name || '').trim();
    var phone = String(form.phone || '').trim();
    var comment = String(form.comment || '').trim();
    if (name.length < 2) errors.name = 'Укажите имя';
    else if (name.length > 80) errors.name = 'Имя слишком длинное';
    var digits = phone.replace(/\D/g, '');
    if (!digits) errors.phone = 'Укажите телефон';
    else if (!/^[\d\s()+-]+$/.test(phone) || digits.length < 10 || digits.length > 15) {
      errors.phone = 'Проверьте номер: например, +7 900 123-45-67';
    }
    if (comment.length > 1000) errors.comment = 'Комментарий — не больше 1000 знаков';
    return { valid: Object.keys(errors).length === 0, errors: errors };
  }

  /** Заявка, которую сайт отправляет на почту / в Telegram / CRM. */
  function buildLead(config, state, form, meta) {
    meta = meta || {};
    var price = calcPrice(config, state);
    return {
      source: 'door-configurator',
      createdAt: meta.now || new Date().toISOString(),
      customer: {
        name: String(form.name || '').trim(),
        phone: String(form.phone || '').trim(),
        comment: String(form.comment || '').trim(),
      },
      door: {
        title: config.title || '',
        options: price.items.map(function (i) {
          return { group: i.groupName, option: i.optionName, price: i.price };
        }),
        selection: assign({}, state),
        basePrice: price.base,
        total: price.total,
        currency: config.currency || '₽',
      },
      link: meta.url || '',
    };
  }

  return {
    findGroup: findGroup,
    findOption: findOption,
    defaultState: defaultState,
    disabledReason: disabledReason,
    resolveConflicts: resolveConflicts,
    normalizeState: normalizeState,
    selectOption: selectOption,
    calcPrice: calcPrice,
    formatPrice: formatPrice,
    formatDelta: formatDelta,
    templateKeys: templateKeys,
    resolveTemplate: resolveTemplate,
    assetUrl: assetUrl,
    getLayers: getLayers,
    serializeState: serializeState,
    parseState: parseState,
    listImagePaths: listImagePaths,
    validateConfig: validateConfig,
    validateLead: validateLead,
    buildLead: buildLead,
  };
});

/* ======================================================================= */
/* 2. Интерфейс                                                            */
/* ======================================================================= */
(function () {
  'use strict';
  if (typeof window === 'undefined' || typeof document === 'undefined') return;

  var core = window.DoorConfiguratorCore;
  var instanceCounter = 0;
  var FADE_MS = 350;

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === 'className') node.className = value;
        else if (key === 'text') node.textContent = value;
        else node.setAttribute(key, value === true ? '' : value);
      });
    }
    (children || []).forEach(function (child) {
      if (child) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function needsCors(src) {
    if (!/^https?:/i.test(src)) return false;
    try {
      return new URL(src, location.href).origin !== location.origin;
    } catch (e) {
      return false;
    }
  }

  function loadImage(src) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      if (needsCors(src)) img.crossOrigin = 'anonymous';
      img.onload = function () {
        resolve(img);
      };
      img.onerror = function () {
        reject(new Error('не загрузилась картинка ' + src));
      };
      img.src = src;
    });
  }

  function mount(root, config) {
    var errors = core.validateConfig(config);
    if (errors.length) {
      root.innerHTML = '';
      root.appendChild(
        el('div', { className: 'dc-config-error', role: 'alert' }, [
          el('strong', { text: 'Конфигуратор не запущен: ошибки в config.js' }),
          el(
            'ul',
            null,
            errors.map(function (e) {
              return el('li', { text: e });
            })
          ),
        ])
      );
      if (window.console) console.error('[door-configurator]', errors);
      return null;
    }

    var uid = 'dc' + ++instanceCounter;
    var currency = config.currency || '₽';
    var state = core.parseState(config, config.syncHash ? location.hash : '');
    var reportedErrors = {};

    root.innerHTML = '';
    root.classList.add('dc');
    root.style.setProperty('--dc-ratio', config.canvas.width + ' / ' + config.canvas.height);
    root.style.setProperty('--dc-k', String(config.canvas.width / config.canvas.height));

    // ---------- превью ----------
    var stage = el('div', { className: 'dc__stage', role: 'img' });
    var slots = {};
    core.getLayers(config, state).forEach(function (layer) {
      var slotEl = el('div', { className: 'dc__layer', 'data-layer': layer.id });
      slotEl.style.zIndex = String(layer.layer);
      stage.appendChild(slotEl);
      slots[layer.id] = { el: slotEl, src: undefined };
    });

    var totalValue = el('strong', { className: 'dc__total-value' });
    var totalBlock = el('div', { className: 'dc__total' }, [
      el('span', { className: 'dc__total-label', text: 'Стоимость' }),
      totalValue,
    ]);

    function button(text, action, extra) {
      return el('button', { type: 'button', className: 'dc-btn' + (extra ? ' ' + extra : ''), 'data-action': action, text: text });
    }
    var actions = el('div', { className: 'dc__actions' }, [
      button((config.lead && config.lead.title) || 'Оставить заявку', 'lead', 'dc-btn--primary dc__lead-main'),
      button('Скачать картинку', 'download'),
      button('Поделиться', 'share'),
      button('Сбросить', 'reset', 'dc-btn--ghost'),
    ]);

    var preview = el('div', { className: 'dc__preview' }, [
      el('div', { className: 'dc__stage-wrap' }, [stage]),
      totalBlock,
      actions,
    ]);

    // ---------- опции ----------
    var optionsForm = el('form', { className: 'dc__options', 'aria-label': 'Параметры двери' });
    optionsForm.addEventListener('submit', function (e) {
      e.preventDefault();
    });
    var groupViews = {};

    config.groups.forEach(function (g) {
      var current = el('span', { className: 'dc-group__current' });
      var legend = el('legend', { className: 'dc-group__title' }, [el('span', { text: g.name }), current]);
      var list = el('div', { className: 'dc-group__options' });
      var fieldset = el('fieldset', { className: 'dc-group', 'data-group': g.id }, [
        legend,
        g.hint ? el('p', { className: 'dc-group__hint', text: g.hint }) : null,
        list,
      ]);
      var optionViews = {};
      g.options.forEach(function (o) {
        var inputId = uid + '-' + g.id + '-' + o.id;
        var hintId = inputId + '-hint';
        var input = el('input', {
          type: 'radio',
          className: 'dc-opt__input',
          id: inputId,
          name: uid + '-' + g.id,
          value: o.id,
          'data-group': g.id,
          'aria-describedby': hintId,
        });
        var thumb = el('span', { className: 'dc-opt__thumb', 'aria-hidden': 'true' });
        var thumbImg = null;
        if (o.thumb) {
          thumbImg = el('img', { alt: '', loading: 'lazy', decoding: 'async' });
          thumb.appendChild(thumbImg);
        }
        if (o.swatch) thumb.style.background = o.swatch;
        if (!o.thumb && !o.swatch) thumb.classList.add('dc-opt__thumb--empty');
        var hint = el('span', { className: 'dc-opt__hint', id: hintId });
        var card = el('span', { className: 'dc-opt__card' }, [
          thumb,
          el('span', { className: 'dc-opt__text' }, [
            el('span', { className: 'dc-opt__name', text: o.name }),
            o.note ? el('span', { className: 'dc-opt__note', text: o.note }) : null,
            el('span', { className: 'dc-opt__price', text: core.formatDelta(o.price || 0, currency) }),
            hint,
          ]),
        ]);
        list.appendChild(el('label', { className: 'dc-opt', for: inputId }, [input, card]));
        optionViews[o.id] = { input: input, hint: hint, thumbImg: thumbImg, thumbSrc: null };
      });
      optionsForm.appendChild(fieldset);
      groupViews[g.id] = { current: current, options: optionViews };
    });

    optionsForm.addEventListener('change', function (e) {
      var input = e.target;
      if (!input || !input.getAttribute('data-group')) return;
      choose(input.getAttribute('data-group'), input.value);
    });

    // ---------- «Ваша дверь» ----------
    var summaryList = el('ul', { className: 'dc-summary__list' });
    var summaryTotal = el('strong');
    var summaryTitleId = uid + '-summary';
    var summary = el('section', { className: 'dc__summary', 'aria-labelledby': summaryTitleId }, [
      el('h2', { className: 'dc__summary-title', id: summaryTitleId, text: 'Ваша дверь' }),
      summaryList,
      el('div', { className: 'dc-summary__total' }, [el('span', { text: 'Итого' }), summaryTotal]),
      el('p', { className: 'dc-summary__note', text: 'Цена без доставки и установки. Точную стоимость менеджер назовёт после замера.' }),
    ]);

    var panel = el('div', { className: 'dc__panel' }, [optionsForm, summary]);

    // ---------- нижняя панель (телефон) ----------
    var barTotal = el('strong', { className: 'dc__bar-total' });
    var bar = el('div', { className: 'dc__bar' }, [
      el('div', { className: 'dc__bar-price' }, [el('span', { text: 'Итого' }), barTotal]),
      button((config.lead && config.lead.title) || 'Оставить заявку', 'lead', 'dc-btn--primary'),
    ]);

    var toast = el('div', { className: 'dc__toast', role: 'status', 'aria-live': 'polite' });

    // ---------- заявка ----------
    var leadCfg = config.lead || {};
    var dialogTitleId = uid + '-lead-title';
    function field(name, label, control) {
      var errId = uid + '-err-' + name;
      control.setAttribute('aria-describedby', errId);
      control.id = uid + '-f-' + name;
      return el('div', { className: 'dc-field' }, [
        el('label', { className: 'dc-field__label', for: control.id, text: label }),
        control,
        el('span', { className: 'dc-field__error', id: errId }),
      ]);
    }
    var nameInput = el('input', { name: 'name', type: 'text', autocomplete: 'name', maxlength: '80', required: true });
    var phoneInput = el('input', {
      name: 'phone',
      type: 'tel',
      autocomplete: 'tel',
      inputmode: 'tel',
      maxlength: '24',
      placeholder: '+7 900 123-45-67',
      required: true,
    });
    var commentInput = el('textarea', {
      name: 'comment',
      rows: '3',
      maxlength: '1000',
      placeholder: 'Например: размер проёма, адрес, удобное время для звонка',
    });
    var leadDoor = el('p', { className: 'dc-form__door' });
    var submitBtn = el('button', { type: 'submit', className: 'dc-btn dc-btn--primary', text: 'Отправить заявку' });
    var leadForm = el('form', { className: 'dc-form', novalidate: true }, [
      el('h2', { className: 'dc-dialog__title', id: dialogTitleId, text: leadCfg.title || 'Оставить заявку' }),
      leadCfg.text ? el('p', { className: 'dc-dialog__text', text: leadCfg.text }) : null,
      leadDoor,
      field('name', 'Имя', nameInput),
      field('phone', 'Телефон', phoneInput),
      field('comment', 'Комментарий (необязательно)', commentInput),
      el('div', { className: 'dc-dialog__actions' }, [
        submitBtn,
        el('button', { type: 'button', className: 'dc-btn dc-btn--ghost', 'data-close': '', text: 'Отмена' }),
      ]),
    ]);
    var resultTitle = el('h2', { className: 'dc-dialog__title' });
    var resultText = el('p', { className: 'dc-dialog__text' });
    var resultJson = el('pre', { className: 'dc-result__json', tabindex: '0' });
    var copyJsonBtn = el('button', { type: 'button', className: 'dc-btn', text: 'Скопировать JSON' });
    var result = el('div', { className: 'dc-result', hidden: true }, [
      resultTitle,
      resultText,
      resultJson,
      el('div', { className: 'dc-dialog__actions' }, [
        el('button', { type: 'button', className: 'dc-btn dc-btn--primary', 'data-close': '', text: 'Готово' }),
        copyJsonBtn,
      ]),
    ]);
    var dialog = el('dialog', { className: 'dc-dialog', 'aria-labelledby': dialogTitleId }, [
      el('button', { type: 'button', className: 'dc-dialog__close', 'data-close': '', 'aria-label': 'Закрыть', text: '×' }),
      leadForm,
      result,
    ]);

    root.appendChild(preview);
    root.appendChild(panel);
    root.appendChild(bar);
    root.appendChild(toast);
    root.appendChild(dialog);

    // ---------- поведение ----------
    var toastTimer = null;
    function showToast(message) {
      toast.textContent = message;
      toast.classList.add('is-visible');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(function () {
        toast.classList.remove('is-visible');
      }, 5000);
    }

    function optionName(groupId, optionId) {
      var o = core.findOption(core.findGroup(config, groupId), optionId);
      return o ? o.name : optionId;
    }

    function removeNode(node) {
      if (node.parentNode) node.parentNode.removeChild(node);
    }

    // Смена картинки слоя: новая проявляется поверх старой, старая убирается после проявления.
    // При быстрых кликах устаревшие загрузки отбрасываются — в слое остаётся только последняя.
    function setLayer(slot, src) {
      if (slot.src === src) return;
      slot.src = src;
      var token = (slot.token = (slot.token || 0) + 1);
      function stale() {
        return slot.token !== token;
      }
      if (!src) {
        Array.prototype.forEach.call(slot.el.querySelectorAll('img'), function (old) {
          old.classList.remove('is-visible');
          setTimeout(function () {
            if (!stale()) removeNode(old);
          }, FADE_MS + 50);
        });
        return;
      }
      var img = new Image();
      img.className = 'dc__layer-img';
      img.alt = '';
      img.decoding = 'async';
      if (needsCors(src)) img.crossOrigin = 'anonymous';
      img.onload = function () {
        if (stale()) {
          removeNode(img);
          return;
        }
        requestAnimationFrame(function () {
          if (stale()) {
            removeNode(img);
            return;
          }
          img.classList.add('is-visible');
          setTimeout(function () {
            if (stale()) return;
            Array.prototype.forEach.call(slot.el.querySelectorAll('img'), function (other) {
              if (other !== img) removeNode(other);
            });
          }, FADE_MS + 50);
        });
      };
      img.onerror = function () {
        removeNode(img);
        if (!reportedErrors[src]) {
          reportedErrors[src] = true;
          showToast('Не загрузилась картинка слоя: ' + src);
          if (window.console) console.warn('[door-configurator] нет файла слоя', src);
        }
      };
      slot.el.appendChild(img);
      img.src = src;
    }

    function render() {
      // опции
      config.groups.forEach(function (g) {
        var view = groupViews[g.id];
        view.current.textContent = optionName(g.id, state[g.id]);
        g.options.forEach(function (o) {
          var ov = view.options[o.id];
          var selected = state[g.id] === o.id;
          var reason = selected ? null : core.disabledReason(config, state, g.id, o.id);
          ov.input.checked = selected;
          ov.input.disabled = !!reason;
          ov.hint.textContent = reason || '';
          if (ov.thumbImg) {
            var thumbSrc = core.assetUrl(config, core.resolveTemplate(o.thumb, state));
            if (ov.thumbSrc !== thumbSrc) {
              ov.thumbSrc = thumbSrc;
              ov.thumbImg.src = thumbSrc;
            }
          }
        });
      });

      // слои
      core.getLayers(config, state).forEach(function (layer) {
        setLayer(slots[layer.id], layer.src);
      });

      // цена и состав
      var price = core.calcPrice(config, state);
      var totalText = core.formatPrice(price.total, currency);
      totalValue.textContent = totalText;
      summaryTotal.textContent = totalText;
      barTotal.textContent = totalText;
      summaryList.innerHTML = '';
      summaryList.appendChild(
        el('li', { className: 'dc-summary__row' }, [
          el('span', { className: 'dc-summary__name' }, [
            el('span', { className: 'dc-summary__group', text: 'Основа' }),
            el('span', { text: config.basePriceLabel || 'Базовая комплектация' }),
          ]),
          el('span', { className: 'dc-summary__price', text: core.formatPrice(price.base, currency) }),
        ])
      );
      price.items.forEach(function (item) {
        summaryList.appendChild(
          el('li', { className: 'dc-summary__row' }, [
            el('span', { className: 'dc-summary__name' }, [
              el('span', { className: 'dc-summary__group', text: item.groupName }),
              el('span', { text: item.optionName + (item.note ? ', ' + item.note : '') }),
            ]),
            el('span', { className: 'dc-summary__price', text: core.formatDelta(item.price, currency) }),
          ])
        );
      });

      stage.setAttribute(
        'aria-label',
        (config.title || 'Дверь') +
          ': ' +
          price.items
            .map(function (i) {
              return i.groupName.toLowerCase() + ' — ' + i.optionName;
            })
            .join('; ')
      );
      leadDoor.textContent = 'Ваша дверь: ' + totalText + ' — состав и ссылка на картинку придут вместе с заявкой.';

      if (config.syncHash) {
        var hash = '#' + core.serializeState(config, state);
        if (location.hash !== hash) {
          try {
            history.replaceState(history.state, '', hash);
          } catch (e) {
            /* старые браузеры / песочница — ссылка просто не обновится */
          }
        }
      }
    }

    function choose(groupId, optionId) {
      var res = core.selectOption(config, state, groupId, optionId);
      if (res.blocked) {
        showToast(res.blocked);
        render();
        return;
      }
      state = res.state;
      render();
      if (res.changes.length) {
        showToast(
          res.changes
            .map(function (c) {
              var g = core.findGroup(config, c.groupId);
              return g.name + ': «' + optionName(c.groupId, c.from) + '» заменено на «' + optionName(c.groupId, c.to) + '». ' + c.reason + '.';
            })
            .join(' ')
        );
      }
    }

    function shareUrl() {
      return location.href.split('#')[0] + '#' + core.serializeState(config, state);
    }

    function share() {
      var url = shareUrl();
      function fallback() {
        window.prompt('Скопируйте ссылку на вашу дверь:', url);
      }
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(url).then(function () {
          showToast('Ссылка скопирована: по ней откроется эта же дверь');
        }, fallback);
      } else {
        fallback();
      }
    }

    function download(btn) {
      var layers = core.getLayers(config, state).filter(function (l) {
        return l.src;
      });
      btn.disabled = true;
      function done() {
        btn.disabled = false;
      }
      function fail(e) {
        done();
        if (location.protocol === 'file:') {
          showToast('Браузер не даёт сохранять картинку со страницы, открытой с диска. Откройте демо по ссылке или через локальный сервер (см. README).');
        } else {
          showToast('Не удалось собрать картинку: ' + (e && e.message ? e.message : 'ошибка браузера'));
        }
      }
      Promise.all(
        layers.map(function (l) {
          return loadImage(l.src);
        })
      ).then(function (images) {
        var scale = 2;
        var canvas = document.createElement('canvas');
        canvas.width = config.canvas.width * scale;
        canvas.height = config.canvas.height * scale;
        var ctx = canvas.getContext('2d');
        images.forEach(function (img) {
          ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        });
        try {
          canvas.toBlob(function (blob) {
            if (!blob) {
              fail(new Error('пустой результат'));
              return;
            }
            var a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'vhodnaya-dver.png';
            document.body.appendChild(a);
            a.click();
            setTimeout(function () {
              URL.revokeObjectURL(a.href);
              a.parentNode.removeChild(a);
            }, 1000);
            done();
          }, 'image/png');
        } catch (e) {
          fail(e);
        }
      }, fail);
    }

    function reset() {
      state = core.defaultState(config);
      render();
      showToast('Выбор сброшен');
    }

    // заявка
    function openLead() {
      leadForm.hidden = false;
      result.hidden = true;
      clearErrors();
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
      nameInput.focus();
    }
    function closeLead() {
      if (typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
    }
    function clearErrors() {
      [nameInput, phoneInput, commentInput].forEach(function (input) {
        input.removeAttribute('aria-invalid');
        document.getElementById(input.getAttribute('aria-describedby')).textContent = '';
      });
    }
    function showResult(title, text, json) {
      resultTitle.textContent = title;
      resultText.textContent = text;
      resultJson.textContent = json || '';
      resultJson.hidden = !json;
      copyJsonBtn.hidden = !json;
      leadForm.hidden = true;
      result.hidden = false;
      resultTitle.setAttribute('tabindex', '-1');
      resultTitle.focus();
    }

    leadForm.addEventListener('submit', function (e) {
      e.preventDefault();
      clearErrors();
      var form = { name: nameInput.value, phone: phoneInput.value, comment: commentInput.value };
      var check = core.validateLead(form);
      if (!check.valid) {
        var first = null;
        [
          ['name', nameInput],
          ['phone', phoneInput],
          ['comment', commentInput],
        ].forEach(function (pair) {
          var msg = check.errors[pair[0]];
          if (!msg) return;
          pair[1].setAttribute('aria-invalid', 'true');
          document.getElementById(pair[1].getAttribute('aria-describedby')).textContent = msg;
          if (!first) first = pair[1];
        });
        if (first) first.focus();
        return;
      }
      var lead = core.buildLead(config, state, form, { url: shareUrl() });
      var json = JSON.stringify(lead, null, 2);
      if (!leadCfg.endpoint) {
        showResult(
          'Заявка сформирована',
          'Это демо: заявка никуда не отправляется. На сайте эти данные уходят на почту, в Telegram или CRM — ниже ровно то, что будет отправлено.',
          json
        );
        leadForm.reset();
        return;
      }
      submitBtn.disabled = true;
      submitBtn.textContent = 'Отправляем…';
      fetch(leadCfg.endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: json })
        .then(function (resp) {
          if (!resp.ok) throw new Error('сервер ответил ' + resp.status);
          showResult('Заявка отправлена', leadCfg.successText || 'Спасибо! Мы свяжемся с вами.', '');
          leadForm.reset();
        })
        .catch(function (err) {
          showToast('Заявка не отправилась (' + err.message + '). Попробуйте ещё раз или позвоните нам.');
        })
        .then(function () {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Отправить заявку';
        });
    });

    copyJsonBtn.addEventListener('click', function () {
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(resultJson.textContent).then(function () {
          showToast('JSON заявки скопирован');
        });
      }
    });

    dialog.addEventListener('click', function (e) {
      if (e.target === dialog || (e.target.closest && e.target.closest('[data-close]'))) closeLead();
    });

    root.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('[data-action]') : null;
      if (!btn || !root.contains(btn)) return;
      var action = btn.getAttribute('data-action');
      if (action === 'lead') openLead();
      else if (action === 'download') download(btn);
      else if (action === 'share') share();
      else if (action === 'reset') reset();
    });

    if (config.syncHash) {
      window.addEventListener('hashchange', function () {
        state = core.parseState(config, location.hash);
        render();
      });
    }

    render();

    // предзагрузка всех картинок, чтобы смена слоёв шла без задержки
    var preloaded = [];
    setTimeout(function () {
      core.listImagePaths(config).forEach(function (p) {
        var img = new Image();
        var src = core.assetUrl(config, p);
        if (needsCors(src)) img.crossOrigin = 'anonymous';
        img.src = src;
        preloaded.push(img);
      });
    }, 400);

    return {
      getState: function () {
        return Object.assign({}, state);
      },
      select: choose,
      reset: reset,
    };
  }

  function autoMount() {
    var nodes = document.querySelectorAll('[data-door-configurator]');
    Array.prototype.forEach.call(nodes, function (node) {
      if (node.getAttribute('data-mounted')) return;
      node.setAttribute('data-mounted', '1');
      mount(node, window.DOOR_CONFIG);
    });
  }

  window.DoorConfigurator = { mount: mount, core: core };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', autoMount);
  else autoMount();
})();
