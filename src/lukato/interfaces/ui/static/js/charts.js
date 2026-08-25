/* charts.js — sparklines e barras em SVG puro, sem biblioteca alguma.
 *
 * Uso no template:
 *
 *   <div class="lk-chart-card"
 *        data-chart="area"
 *        data-series="0.4,1.2,0.8,2.1,1.7"
 *        data-labels="00h,01h,02h,03h,04h"
 *        data-format="money"></div>
 *
 * Tipos: `line`, `area` (linha com preenchimento) e `bar`.
 *
 * Por que SVG escrito a mao em vez de uma biblioteca: o console precisa
 * renderizar identico em rede fechada (SPEC-0009 secao 1), e um grafico de custo
 * por hora e uma polilinha. Cores e tipografia saem das variaveis CSS, entao os
 * graficos acompanham o tema claro/escuro sem uma linha a mais.
 */

(function () {
  "use strict";

  /* O namespace do SVG e obtido do proprio parser em vez de escrito como
     literal: o console e offline-first e nenhum arquivo servido de `static/`
     pode conter uma URL — nem mesmo uma que jamais e buscada, como esta. */
  var NS = (function () {
    var probe = document.createElement("div");
    probe.innerHTML = "<svg></svg>";
    return probe.firstChild.namespaceURI;
  })();

  var WIDTH = 600;
  var HEIGHT = 140;
  var PADDING = { top: 10, right: 8, bottom: 18, left: 8 };

  function node(name, attributes) {
    var element = document.createElementNS(NS, name);
    Object.keys(attributes || {}).forEach(function (key) {
      element.setAttribute(key, String(attributes[key]));
    });
    return element;
  }

  function parseSeries(raw) {
    return String(raw || "")
      .split(",")
      .map(function (value) {
        var parsed = parseFloat(String(value).trim());
        return isFinite(parsed) ? parsed : 0;
      });
  }

  function parseLabels(raw) {
    if (!raw) {
      return [];
    }
    return String(raw)
      .split(",")
      .map(function (value) {
        return value.trim();
      });
  }

  function formatValue(value, kind) {
    if (!window.lukato) {
      return String(value);
    }
    if (kind === "money") {
      return window.lukato.formatMoney(value);
    }
    if (kind === "tokens") {
      return window.lukato.formatTokens(value);
    }
    return window.lukato.formatNumber(value, 0);
  }

  function scaleY(value, max, height) {
    if (max <= 0) {
      return height + PADDING.top;
    }
    return PADDING.top + height - (value / max) * height;
  }

  function drawLine(svg, values, max, width, height, filled) {
    var step = values.length > 1 ? width / (values.length - 1) : 0;
    var points = values.map(function (value, index) {
      return [PADDING.left + index * step, scaleY(value, max, height)];
    });

    var path = points
      .map(function (point, index) {
        return (index === 0 ? "M" : "L") + point[0].toFixed(1) + " " + point[1].toFixed(1);
      })
      .join(" ");

    if (filled && points.length) {
      var floor = PADDING.top + height;
      var area =
        path +
        " L" +
        points[points.length - 1][0].toFixed(1) +
        " " +
        floor +
        " L" +
        points[0][0].toFixed(1) +
        " " +
        floor +
        " Z";
      svg.appendChild(node("path", { class: "lk-chart__area", d: area }));
    }
    svg.appendChild(node("path", { class: "lk-chart__line", d: path }));
  }

  function drawBars(svg, values, max, width, height) {
    var slot = values.length ? width / values.length : width;
    var barWidth = Math.max(2, slot * 0.62);
    values.forEach(function (value, index) {
      var top = scaleY(value, max, height);
      var barHeight = Math.max(1, PADDING.top + height - top);
      svg.appendChild(
        node("rect", {
          class: "lk-chart__bar",
          x: (PADDING.left + index * slot + (slot - barWidth) / 2).toFixed(1),
          y: top.toFixed(1),
          width: barWidth.toFixed(1),
          height: barHeight.toFixed(1),
          rx: 1.5,
        })
      );
    });
  }

  function render(host) {
    var values = parseSeries(host.getAttribute("data-series"));
    if (!values.length) {
      return;
    }
    var labels = parseLabels(host.getAttribute("data-labels"));
    var kind = host.getAttribute("data-chart") || "line";
    var format = host.getAttribute("data-format") || "number";

    var width = WIDTH - PADDING.left - PADDING.right;
    var height = HEIGHT - PADDING.top - PADDING.bottom;
    var max = values.reduce(function (accumulator, value) {
      return Math.max(accumulator, value);
    }, 0);

    var svg = node("svg", {
      class: "lk-chart",
      viewBox: "0 0 " + WIDTH + " " + HEIGHT,
      preserveAspectRatio: "none",
      role: "img",
    });

    var peak = formatValue(max, format);
    var title = node("title", {});
    title.textContent =
      host.getAttribute("data-title") || "Série de " + values.length + " pontos, pico " + peak;
    svg.appendChild(title);

    svg.appendChild(
      node("line", {
        class: "lk-chart__axis",
        x1: PADDING.left,
        y1: PADDING.top + height,
        x2: PADDING.left + width,
        y2: PADDING.top + height,
      })
    );

    if (kind === "bar") {
      drawBars(svg, values, max, width, height);
    } else {
      drawLine(svg, values, max, width, height, kind === "area");
    }

    if (labels.length) {
      var first = node("text", {
        class: "lk-chart__label",
        x: PADDING.left,
        y: HEIGHT - 4,
      });
      first.textContent = labels[0];
      var last = node("text", {
        class: "lk-chart__label",
        x: PADDING.left + width,
        y: HEIGHT - 4,
        "text-anchor": "end",
      });
      last.textContent = labels[labels.length - 1];
      svg.appendChild(first);
      svg.appendChild(last);
    }

    host.textContent = "";
    host.appendChild(svg);

    var caption = host.getAttribute("data-caption");
    if (caption) {
      var note = document.createElement("p");
      note.className = "lk-hint";
      note.textContent = caption + " · pico " + peak;
      host.appendChild(note);
    }
  }

  function setup() {
    var hosts = document.querySelectorAll("[data-chart]");
    Array.prototype.forEach.call(hosts, render);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup);
  } else {
    setup();
  }

  window.lukato = Object.assign(window.lukato || {}, { renderChart: render });
})();
