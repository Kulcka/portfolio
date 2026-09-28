'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const core = require('../configurator.js');
const realConfig = require('../config.js');

const NBSP = '\u00a0';

/** Небольшой конфиг для проверки правил, не зависящий от демо-цен. */
function miniConfig(overrides) {
  return Object.assign(
    {
      title: 'Тест',
      basePrice: 1000,
      canvas: { width: 10, height: 20 },
      staticLayers: [{ id: 'bg', image: 'bg.png', layer: 0 }],
      groups: [
        {
          id: 'a',
          name: 'Группа A',
          layer: 30,
          default: 'x',
          options: [
            { id: 'x', name: 'X', price: 0, image: 'a/{b}/x.png' },
            { id: 'y', name: 'Y', price: 100, image: 'a/{b}/y.png' },
          ],
        },
        {
          id: 'b',
          name: 'Группа B',
          layer: 10,
          default: 'p',
          options: [
            { id: 'p', name: 'P', price: 0, image: null },
            { id: 'q', name: 'Q', price: 250, image: 'b/q.png' },
          ],
        },
        {
          id: 'c',
          name: 'Группа C',
          layer: 20,
          default: 'm',
          options: [
            { id: 'm', name: 'M', image: 'c/m.png' },
            { id: 'n', name: 'N', price: -50, image: 'c/n.png' },
          ],
        },
      ],
      rules: [],
    },
    overrides
  );
}

test('по умолчанию выбран default каждой группы', () => {
  const state = core.defaultState(realConfig);
  assert.deepEqual(state, {
    size: 'single',
    coating: 'graphite',
    milling: 'smooth',
    glass: 'none',
    handle: 'chrome',
    peephole: 'none',
    frame: 'black',
    extras: 'none',
  });
});

test('цена по умолчанию равна базовой', () => {
  const price = core.calcPrice(realConfig, core.defaultState(realConfig));
  assert.equal(price.base, 42900);
  assert.equal(price.total, 42900);
  assert.equal(price.items.length, realConfig.groups.length);
});

test('цена: база + надбавки всех выбранных вариантов', () => {
  const state = {
    size: 'oneandhalf', // 18 500
    coating: 'wenge', // 6 400
    milling: 'lines', // 2 600
    glass: 'vertical', // 6 900
    handle: 'gold', // 2 400
    peephole: 'video', // 6 500
    frame: 'bronze', // 1 800
    extras: 'both', // 33 900
  };
  const price = core.calcPrice(realConfig, state);
  assert.equal(price.total, 42900 + 18500 + 6400 + 2600 + 6900 + 2400 + 6500 + 1800 + 33900);
  const glass = price.items.find((i) => i.groupId === 'glass');
  assert.deepEqual(glass, {
    groupId: 'glass',
    groupName: 'Стеклопакет',
    optionId: 'vertical',
    optionName: 'Узкая вертикальная',
    note: 'тонированный',
    price: 6900,
  });
});

test('цена: вариант без price считается нулём, отрицательная надбавка вычитается', () => {
  const cfg = miniConfig();
  assert.equal(core.calcPrice(cfg, { a: 'y', b: 'q', c: 'm' }).total, 1000 + 100 + 250);
  assert.equal(core.calcPrice(cfg, { a: 'x', b: 'p', c: 'n' }).total, 950);
});

test('формат цены — с неразрывными пробелами', () => {
  assert.equal(core.formatPrice(121900, '₽'), `121${NBSP}900${NBSP}₽`);
  assert.equal(core.formatPrice(1234567), `1${NBSP}234${NBSP}567${NBSP}₽`);
  assert.equal(core.formatPrice(900, '₽'), `900${NBSP}₽`);
  assert.equal(core.formatDelta(0, '₽'), 'в базе');
  assert.equal(core.formatDelta(2900, '₽'), `+2${NBSP}900${NBSP}₽`);
  assert.equal(core.formatDelta(-50, '₽'), `−50${NBSP}₽`);
});

test('совместимость: классическая филёнка отключает все стёкла', () => {
  const state = Object.assign(core.defaultState(realConfig), { milling: 'classic' });
  for (const glass of ['vertical', 'squares', 'window']) {
    assert.match(core.disabledReason(realConfig, state, 'glass', glass), /без стекла/);
  }
  assert.equal(core.disabledReason(realConfig, state, 'glass', 'none'), null);
  state.milling = 'lines';
  assert.equal(core.disabledReason(realConfig, state, 'glass', 'vertical'), null);
});

test('совместимость: волна недоступна на порошковой окраске', () => {
  const state = core.defaultState(realConfig);
  assert.ok(core.disabledReason(realConfig, state, 'milling', 'wave'));
  state.coating = 'copper';
  assert.ok(core.disabledReason(realConfig, state, 'milling', 'wave'));
  state.coating = 'oak';
  assert.equal(core.disabledReason(realConfig, state, 'milling', 'wave'), null);
});

test('выбор несовместимого варианта сбрасывает конфликтующий на вариант по умолчанию', () => {
  let state = core.defaultState(realConfig);
  state = core.selectOption(realConfig, state, 'glass', 'vertical').state;
  const res = core.selectOption(realConfig, state, 'milling', 'classic');
  assert.equal(res.blocked, null);
  assert.equal(res.state.milling, 'classic');
  assert.equal(res.state.glass, 'none');
  assert.equal(res.changes.length, 1);
  assert.deepEqual(
    { groupId: res.changes[0].groupId, from: res.changes[0].from, to: res.changes[0].to },
    { groupId: 'glass', from: 'vertical', to: 'none' }
  );
  assert.match(res.changes[0].reason, /филёнка/);
  // исходный объект не мутирован
  assert.equal(state.glass, 'vertical');
});

test('смена покрытия на порошок снимает волну', () => {
  let state = core.defaultState(realConfig);
  state = core.selectOption(realConfig, state, 'coating', 'oak').state;
  state = core.selectOption(realConfig, state, 'milling', 'wave').state;
  assert.equal(state.milling, 'wave');
  const res = core.selectOption(realConfig, state, 'coating', 'graphite');
  assert.equal(res.state.milling, 'smooth');
  assert.equal(res.changes[0].groupId, 'milling');
});

test('недоступный вариант выбрать нельзя: состояние не меняется', () => {
  const state = core.defaultState(realConfig); // графит
  const res = core.selectOption(realConfig, state, 'milling', 'wave');
  assert.match(res.blocked, /МДФ/);
  assert.deepEqual(res.state, state);
  assert.deepEqual(res.changes, []);
});

test('выбор несуществующей группы или варианта — ошибка', () => {
  const state = core.defaultState(realConfig);
  assert.throws(() => core.selectOption(realConfig, state, 'color', 'red'), /Нет группы/);
  assert.throws(() => core.selectOption(realConfig, state, 'glass', 'stained'), /нет варианта/);
});

test('правила: условие when из нескольких групп работает как «И»', () => {
  const cfg = miniConfig({
    rules: [{ when: { a: ['y'], b: ['q'] }, disable: { c: ['n'] }, reason: 'нельзя' }],
  });
  assert.equal(core.disabledReason(cfg, { a: 'y', b: 'p', c: 'm' }, 'c', 'n'), null);
  assert.equal(core.disabledReason(cfg, { a: 'x', b: 'q', c: 'm' }, 'c', 'n'), null);
  assert.equal(core.disabledReason(cfg, { a: 'y', b: 'q', c: 'm' }, 'c', 'n'), 'нельзя');
});

test('правила: снятие конфликтов идёт цепочкой', () => {
  const cfg = miniConfig({
    rules: [
      { when: { a: ['y'] }, disable: { b: ['q'] }, reason: 'A=Y без Q' },
      { when: { b: ['p'] }, disable: { c: ['n'] }, reason: 'B=P без N' },
    ],
  });
  const res = core.selectOption(cfg, { a: 'x', b: 'q', c: 'n' }, 'a', 'y');
  assert.deepEqual(res.state, { a: 'y', b: 'p', c: 'm' });
  assert.deepEqual(
    res.changes.map((c) => c.groupId),
    ['b', 'c']
  );
});

test('правила: если default сам недоступен, берётся первый доступный вариант', () => {
  const cfg = miniConfig({
    groups: miniConfig().groups.map((g) =>
      g.id === 'c'
        ? Object.assign({}, g, {
            options: g.options.concat([{ id: 'k', name: 'K', image: 'c/k.png' }]),
          })
        : g
    ),
    rules: [{ when: { a: ['y'] }, disable: { c: ['m', 'n'] }, reason: 'только K' }],
  });
  const res = core.selectOption(cfg, { a: 'x', b: 'p', c: 'n' }, 'a', 'y');
  assert.equal(res.state.c, 'k');
});

test('ссылка: выбор → hash → выбор без потерь для всех сочетаний демо-конфига', () => {
  const groups = realConfig.groups;
  let checked = 0;
  function walk(i, state) {
    if (i === groups.length) {
      const normalized = core.normalizeState(realConfig, state);
      const hash = '#' + core.serializeState(realConfig, normalized);
      assert.deepEqual(core.parseState(realConfig, hash), normalized, hash);
      checked++;
      return;
    }
    for (const o of groups[i].options) walk(i + 1, Object.assign({}, state, { [groups[i].id]: o.id }));
  }
  walk(0, {});
  assert.equal(checked, groups.reduce((n, g) => n * g.options.length, 1));
});

test('ссылка: формат читаемый, порядок групп как в конфиге', () => {
  const state = Object.assign(core.defaultState(realConfig), { coating: 'oak', extras: 'both' });
  assert.equal(
    core.serializeState(realConfig, state),
    'size=single&coating=oak&milling=smooth&glass=none&handle=chrome&peephole=none&frame=black&extras=both'
  );
});

test('ссылка: особые символы в id кодируются и раскодируются', () => {
  const cfg = miniConfig();
  cfg.groups[0].options.push({ id: 'с пробелом&=', name: 'Особый', image: null });
  const state = { a: 'с пробелом&=', b: 'q', c: 'n' };
  const text = core.serializeState(cfg, state);
  assert.ok(!text.includes(' '));
  assert.deepEqual(core.parseState(cfg, text), state);
});

test('ссылка: мусор, неизвестные группы и битая кодировка не ломают разбор', () => {
  const state = core.parseState(realConfig, '#size=huge&coating=oak&foo=bar&=x&glass&handle=%E0%A4%A&frame=bronze');
  assert.deepEqual(state, Object.assign(core.defaultState(realConfig), { coating: 'oak', frame: 'bronze' }));
  assert.deepEqual(core.parseState(realConfig, ''), core.defaultState(realConfig));
  assert.deepEqual(core.parseState(realConfig, null), core.defaultState(realConfig));
  assert.equal(core.parseState(realConfig, '?coating=white').coating, 'white');
});

test('ссылка с несовместимым выбором открывается в совместимом виде', () => {
  const state = core.parseState(realConfig, '#milling=classic&glass=vertical&coating=oak');
  assert.equal(state.milling, 'classic');
  assert.equal(state.glass, 'none');
  const state2 = core.parseState(realConfig, '#milling=wave&coating=graphite');
  assert.equal(state2.milling, 'smooth');
});

test('слои: порядок по layer, шаблоны путей подставлены, пустые слои — null', () => {
  const state = Object.assign(core.defaultState(realConfig), { size: 'oneandhalf', frame: 'bronze', extras: 'sides' });
  const layers = core.getLayers(realConfig, state);
  assert.deepEqual(
    layers.map((l) => l.id),
    ['static-scene', 'size', 'extras', 'frame', 'coating', 'milling', 'glass', 'peephole', 'handle']
  );
  const byId = Object.fromEntries(layers.map((l) => [l.id, l]));
  assert.equal(byId.extras.src, 'layers/extras/oneandhalf/bronze/sides.svg');
  assert.equal(byId.coating.src, 'layers/coating/oneandhalf/graphite.svg');
  assert.equal(byId.milling.src, null);
  assert.equal(byId.glass.src, null);
  for (let i = 1; i < layers.length; i++) assert.ok(layers[i - 1].layer <= layers[i].layer);
});

test('слои: assetsBase добавляется к относительным путям, абсолютные не трогаются', () => {
  const cfg = miniConfig({ assetsBase: 'https://cdn.example.ru/door/' });
  cfg.groups[2].options[0].image = 'https://other.example.ru/m.png';
  const layers = core.getLayers(cfg, { a: 'y', b: 'q', c: 'm' });
  const byId = Object.fromEntries(layers.map((l) => [l.id, l]));
  assert.equal(byId.a.src, 'https://cdn.example.ru/door/a/q/y.png');
  assert.equal(byId.a.path, 'a/q/y.png');
  assert.equal(byId.c.src, 'https://other.example.ru/m.png');
  assert.equal(core.assetUrl(cfg, '/abs/path.png'), '/abs/path.png');
  assert.equal(core.assetUrl(cfg, null), null);
});

test('шаблоны путей: ключи и подстановка', () => {
  assert.deepEqual(core.templateKeys('layers/{size}/{frame}/{size}.svg'), ['size', 'frame']);
  assert.deepEqual(core.templateKeys('plain.svg'), []);
  assert.equal(core.resolveTemplate('l/{size}/{nope}.svg', { size: 'single' }), 'l/single/{nope}.svg');
  assert.equal(core.resolveTemplate(null, {}), null);
});

test('список картинок раскрывает шаблоны по всем вариантам', () => {
  const paths = core.listImagePaths(realConfig);
  assert.equal(new Set(paths).size, paths.length, 'без повторов');
  assert.ok(paths.includes('layers/scene.svg'));
  assert.ok(paths.includes('layers/extras/oneandhalf/bronze/both.svg'));
  assert.ok(paths.includes('layers/extras/single/black/transom.svg'));
  assert.ok(paths.includes('thumbs/coating/wenge.svg'));
  assert.ok(!paths.some((p) => p.includes('{')), 'все шаблоны раскрыты');
  const extras = paths.filter((p) => p.startsWith('layers/extras/'));
  assert.equal(extras.length, 3 * 2 * 3);
});

test('валидация: демо-конфиг без ошибок', () => {
  assert.deepEqual(core.validateConfig(realConfig), []);
});

test('валидация: находит типичные ошибки заказчика', () => {
  const cases = [
    [null, /Конфиг не найден/],
    [miniConfig({ basePrice: '1000' }), /basePrice/],
    [miniConfig({ canvas: undefined }), /canvas/],
    [miniConfig({ groups: [] }), /хотя бы одна группа/],
  ];
  const mutate = (fn) => {
    const cfg = JSON.parse(JSON.stringify(miniConfig()));
    fn(cfg);
    return cfg;
  };
  cases.push([mutate((c) => delete c.groups[1].default), /«b»: нет default/]);
  cases.push([mutate((c) => (c.groups[1].default = 'z')), /default «z» нет среди вариантов/]);
  cases.push([mutate((c) => (c.groups[2].id = 'a')), /id повторяется/]);
  cases.push([mutate((c) => c.groups[1].options.push({ id: 'p', name: 'Дубль' })), /«p»: id повторяется/]);
  cases.push([mutate((c) => (c.groups[1].options[1].price = '250 ₽')), /price должен быть числом/]);
  cases.push([mutate((c) => (c.groups[0].layer = 'top')), /layer/]);
  cases.push([mutate((c) => (c.groups[0].options[0].image = 'a/{color}/x.png')), /\{color\} — нет такой группы/]);
  cases.push([mutate((c) => (c.groups[0].options[0].image = 'a/{a}/x.png')), /свою же группу/]);
  cases.push([mutate((c) => (c.groups[0].options = [])), /нет вариантов/]);
  cases.push([mutate((c) => (c.staticLayers[0].image = '')), /Постоянный слой «bg»: нет image/]);
  cases.push([mutate((c) => (c.rules = [{ when: { a: ['y'] }, disable: { d: ['z'] } }])), /нет группы «d»/]);
  cases.push([mutate((c) => (c.rules = [{ when: { a: ['w'] }, disable: { b: ['q'] } }])), /нет варианта «w»/]);
  cases.push([mutate((c) => (c.rules = [{ when: { a: ['y'] }, disable: { a: ['x'] } }])), /и в when, и в disable/]);
  cases.push([
    mutate((c) => (c.rules = [{ when: { a: ['x'] }, disable: { b: ['p'] }, reason: 'конфликт' }])),
    /по умолчанию несовместим/,
  ]);
  for (const [cfg, pattern] of cases) {
    const errors = core.validateConfig(cfg);
    assert.ok(errors.some((e) => pattern.test(e)), `ожидалась ошибка ${pattern}, получено: ${JSON.stringify(errors)}`);
  }
});

test('заявка: проверка полей', () => {
  assert.deepEqual(core.validateLead({ name: 'Анна', phone: '+7 (900) 123-45-67' }), { valid: true, errors: {} });
  assert.equal(core.validateLead({ name: 'Анна', phone: '89001234567', comment: 'Проём 880' }).valid, true);
  const empty = core.validateLead({});
  assert.equal(empty.valid, false);
  assert.ok(empty.errors.name && empty.errors.phone);
  assert.ok(core.validateLead({ name: 'Анна', phone: '12345' }).errors.phone);
  assert.ok(core.validateLead({ name: 'Анна', phone: '+7 900 abc 45 67' }).errors.phone);
  assert.ok(core.validateLead({ name: 'Анна', phone: '+7 900 123 45 67', comment: 'x'.repeat(1001) }).errors.comment);
  assert.ok(core.validateLead({ name: ' А ', phone: '+79001234567' }).errors.name);
});

test('заявка: состав двери, итог и ссылка', () => {
  const state = Object.assign(core.defaultState(realConfig), { coating: 'oak', handle: 'bar' });
  const lead = core.buildLead(
    realConfig,
    state,
    { name: '  Иван ', phone: '+7 900 123-45-67', comment: 'Проём 900 × 2060' },
    { now: '2026-09-28T10:00:00.000Z', url: 'https://example.ru/door#x' }
  );
  assert.equal(lead.source, 'door-configurator');
  assert.equal(lead.createdAt, '2026-09-28T10:00:00.000Z');
  assert.deepEqual(lead.customer, { name: 'Иван', phone: '+7 900 123-45-67', comment: 'Проём 900 × 2060' });
  assert.equal(lead.door.total, 42900 + 6900 + 4900);
  assert.equal(lead.door.options.length, realConfig.groups.length);
  assert.deepEqual(lead.door.options[1], { group: 'Покрытие полотна', option: 'МДФ белый дуб', price: 6900 });
  assert.deepEqual(lead.door.selection, state);
  assert.equal(lead.link, 'https://example.ru/door#x');
  assert.doesNotThrow(() => JSON.parse(JSON.stringify(lead)));
});
