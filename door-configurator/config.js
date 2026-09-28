/*
 * Настройки конфигуратора входной двери.
 *
 * Всё, что видит покупатель, задаётся здесь: группы опций, варианты, цены, картинки-слои,
 * порядок слоёв и правила совместимости. Код (configurator.js) менять не нужно.
 *
 * Картинки-слои
 *   Все слои — файлы одного размера (canvas.width × canvas.height) с прозрачным фоном;
 *   конфигуратор накладывает их друг на друга. Чем больше `layer` у группы, тем выше её слой.
 *   В пути можно подставить выбор другой группы: "layers/coating/{size}/wenge.svg" —
 *   вместо {size} встанет id выбранного типа двери (single или oneandhalf).
 *   image: null — у варианта нет своей картинки (например, «без стекла»).
 *
 * Миниатюры вариантов
 *   thumb — картинка 2:3 (например, 60 × 90), swatch — CSS-цвет или градиент, если картинки нет.
 *
 * Цены — в рублях: basePrice + надбавки выбранных вариантов (price).
 */
(function (root) {
  'use strict';

  var config = {
    title: 'Входная дверь',
    currency: '₽',
    basePrice: 42900,
    basePriceLabel: 'Базовая комплектация: стальное полотно 2 листа, 2 замка, утеплитель',

    // Размер холста всех слоёв, px
    canvas: { width: 600, height: 1000 },

    // Префикс путей ко всем картинкам. '' — рядом с index.html; для Tilda и т.п. —
    // полный адрес папки с картинками, например 'https://example.ru/door/'.
    assetsBase: '',

    // Писать выбор в адрес страницы (#size=single&...), чтобы ссылкой можно было поделиться.
    syncHash: true,

    // Слои без выбора: всегда на картинке.
    staticLayers: [
      { id: 'scene', name: 'Интерьер', image: 'layers/scene.svg', layer: 0 }
    ],

    groups: [
      {
        id: 'size',
        name: 'Тип двери',
        layer: 5,
        default: 'single',
        options: [
          { id: 'single', name: 'Одностворчатая', note: '860 × 2050 мм', price: 0,
            image: 'layers/size/single.svg', thumb: 'thumbs/size/single.svg' },
          { id: 'oneandhalf', name: 'Полуторная', note: '1250 × 2050 мм', price: 18500,
            image: 'layers/size/oneandhalf.svg', thumb: 'thumbs/size/oneandhalf.svg' }
        ]
      },
      {
        id: 'coating',
        name: 'Покрытие полотна',
        layer: 30,
        default: 'graphite',
        options: [
          { id: 'graphite', name: 'Порошок графит', price: 0, swatch: '#43474c',
            image: 'layers/coating/{size}/graphite.svg', thumb: 'thumbs/coating/graphite.svg' },
          { id: 'copper', name: 'Порошок антик медь', price: 2900, swatch: '#94603a',
            image: 'layers/coating/{size}/copper.svg', thumb: 'thumbs/coating/copper.svg' },
          { id: 'wenge', name: 'МДФ венге', price: 6400, swatch: '#3e2b21',
            image: 'layers/coating/{size}/wenge.svg', thumb: 'thumbs/coating/wenge.svg' },
          { id: 'oak', name: 'МДФ белый дуб', price: 6900, swatch: '#dccfb6',
            image: 'layers/coating/{size}/oak.svg', thumb: 'thumbs/coating/oak.svg' },
          { id: 'concrete', name: 'МДФ «под бетон»', price: 7400, swatch: '#a19e99',
            image: 'layers/coating/{size}/concrete.svg', thumb: 'thumbs/coating/concrete.svg' },
          { id: 'white', name: 'МДФ эмаль белая', price: 8800, swatch: '#ecebe6',
            image: 'layers/coating/{size}/white.svg', thumb: 'thumbs/coating/white.svg' }
        ]
      },
      {
        id: 'milling',
        name: 'Фрезеровка панели',
        layer: 40,
        default: 'smooth',
        options: [
          { id: 'smooth', name: 'Гладкая', price: 0,
            image: null, thumb: 'thumbs/milling/smooth.svg' },
          { id: 'lines', name: 'Горизонтальные линии', price: 2600,
            image: 'layers/milling/{size}/lines.svg', thumb: 'thumbs/milling/lines.svg' },
          { id: 'modern', name: 'Двойная рамка', price: 3200,
            image: 'layers/milling/{size}/modern.svg', thumb: 'thumbs/milling/modern.svg' },
          { id: 'classic', name: 'Классика, две филёнки', price: 4400,
            image: 'layers/milling/{size}/classic.svg', thumb: 'thumbs/milling/classic.svg' },
          { id: 'wave', name: 'Волна', price: 4800,
            image: 'layers/milling/{size}/wave.svg', thumb: 'thumbs/milling/wave.svg' }
        ]
      },
      {
        id: 'glass',
        name: 'Стеклопакет',
        layer: 50,
        default: 'none',
        options: [
          { id: 'none', name: 'Без стекла', price: 0,
            image: null, thumb: 'thumbs/glass/none.svg' },
          { id: 'vertical', name: 'Узкая вертикальная', note: 'тонированный', price: 6900,
            image: 'layers/glass/{size}/vertical.svg', thumb: 'thumbs/glass/vertical.svg' },
          { id: 'squares', name: 'Квадраты', note: '5 окошек', price: 5400,
            image: 'layers/glass/{size}/squares.svg', thumb: 'thumbs/glass/squares.svg' },
          { id: 'window', name: 'Окно-триплекс', note: 'матовое', price: 8200,
            image: 'layers/glass/{size}/window.svg', thumb: 'thumbs/glass/window.svg' }
        ]
      },
      {
        id: 'handle',
        name: 'Ручка',
        layer: 70,
        default: 'chrome',
        options: [
          { id: 'chrome', name: 'Нажимная, хром', price: 0,
            image: 'layers/handle/{size}/chrome.svg', thumb: 'thumbs/handle/chrome.svg' },
          { id: 'black', name: 'Нажимная, чёрная', price: 1200,
            image: 'layers/handle/{size}/black.svg', thumb: 'thumbs/handle/black.svg' },
          { id: 'gold', name: 'Нажимная, золото', price: 2400,
            image: 'layers/handle/{size}/gold.svg', thumb: 'thumbs/handle/gold.svg' },
          { id: 'bar', name: 'Скоба 800 мм, чёрная', price: 4900,
            image: 'layers/handle/{size}/bar.svg', thumb: 'thumbs/handle/bar.svg' }
        ]
      },
      {
        id: 'peephole',
        name: 'Глазок',
        layer: 60,
        default: 'none',
        options: [
          { id: 'none', name: 'Без глазка', price: 0,
            image: null, thumb: 'thumbs/peephole/none.svg' },
          { id: 'standard', name: 'Глазок', note: 'обзор 200°', price: 900,
            image: 'layers/peephole/{size}/standard.svg', thumb: 'thumbs/peephole/standard.svg' },
          { id: 'video', name: 'Видеоглазок', note: 'запись на карту', price: 6500,
            image: 'layers/peephole/{size}/video.svg', thumb: 'thumbs/peephole/video.svg' }
        ]
      },
      {
        id: 'frame',
        name: 'Короб и наличник',
        layer: 20,
        default: 'black',
        options: [
          { id: 'black', name: 'Чёрный муар', price: 0, swatch: '#232426',
            image: 'layers/frame/{size}/black.svg', thumb: 'thumbs/frame/black.svg' },
          { id: 'graphite', name: 'Графит', price: 0, swatch: '#51555b',
            image: 'layers/frame/{size}/graphite.svg', thumb: 'thumbs/frame/graphite.svg' },
          { id: 'bronze', name: 'Бронза', price: 1800, swatch: '#866b47',
            image: 'layers/frame/{size}/bronze.svg', thumb: 'thumbs/frame/bronze.svg' }
        ]
      },
      {
        id: 'extras',
        name: 'Фрамуга и боковые вставки',
        hint: 'Стеклянные секции в цвет короба',
        layer: 10,
        default: 'none',
        options: [
          { id: 'none', name: 'Без вставок', price: 0,
            image: null, thumb: 'thumbs/extras/none.svg' },
          { id: 'transom', name: 'Фрамуга сверху', price: 14500,
            image: 'layers/extras/{size}/{frame}/transom.svg', thumb: 'thumbs/extras/transom.svg' },
          { id: 'sides', name: 'Боковые вставки', price: 21800,
            image: 'layers/extras/{size}/{frame}/sides.svg', thumb: 'thumbs/extras/sides.svg' },
          { id: 'both', name: 'Фрамуга и вставки', price: 33900,
            image: 'layers/extras/{size}/{frame}/both.svg', thumb: 'thumbs/extras/both.svg' }
        ]
      }
    ],

    // Правила совместимости: если выбрано то, что в `when`, варианты из `disable` недоступны.
    // Если покупатель выбирает вариант, с которым уже выбранное несовместимо, несовместимое
    // автоматически меняется на вариант по умолчанию, а покупатель видит подсказку `reason`.
    rules: [
      {
        when: { milling: ['classic'] },
        disable: { glass: ['vertical', 'squares', 'window'] },
        reason: 'Классическая филёнка делается только глухой, без стекла'
      },
      {
        when: { coating: ['graphite', 'copper'] },
        disable: { milling: ['wave'] },
        reason: 'Волна фрезеруется только по МДФ-панели'
      }
    ],

    lead: {
      // Адрес, куда отправлять заявку (POST, JSON). Пусто — демо-режим: заявка показывается на экране.
      endpoint: '',
      title: 'Оставить заявку',
      text: 'Менеджер перезвонит, уточнит размеры проёма и назовёт точную цену с установкой.',
      successText: 'Спасибо! Заявка отправлена, менеджер перезвонит в рабочее время.'
    }
  };

  if (typeof module === 'object' && module.exports) {
    module.exports = config;
  } else {
    root.DOOR_CONFIG = config;
  }
})(typeof window !== 'undefined' ? window : globalThis);
