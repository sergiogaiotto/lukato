/* palette.js — paleta de comandos (⌘K / Ctrl+K).
 *
 * Navegar num console de doze telas com o mouse e lento. A paleta transforma a
 * navegacao em digitacao: abre com ⌘K, filtra rotas e modulos por subsequencia
 * (digitar "adcom" encontra "AdWatch · Comerciais") e navega com as setas.
 *
 * As opcoes saem do proprio DOM — os links da sidebar ja renderizados — mais um
 * punhado de acoes fixas. Nao ha requisicao nem indice a manter: o que esta no
 * menu esta na paleta, sempre.
 *
 * A marcacao e criada aqui, e nao em um template, porque a paleta so existe com
 * JavaScript: renderizar um dialogo morto no HTML de quem nao tem JS seria
 * entregar um botao que nao faz nada.
 */

(function () {
  "use strict";

  var MAX_RESULTS = 12;

  var overlay = null;
  var input = null;
  var list = null;
  var items = [];
  var matches = [];
  var cursor = 0;

  /** Rotas conhecidas: as da sidebar renderizada + as telas sem item de menu. */
  function collect() {
    var found = [];
    var seen = {};

    var links = document.querySelectorAll(".lk-sidebar .lk-nav__link");
    Array.prototype.forEach.call(links, function (link) {
      var href = link.getAttribute("href");
      var label = (link.textContent || "").trim();
      if (href && label && !seen[href]) {
        seen[href] = true;
        found.push({ label: label, href: href, hint: href });
      }
    });

    [
      { label: "AdWatch · Comerciais", href: "/adwatch/commercials" },
      { label: "AdWatch · Detecções", href: "/adwatch/detections" },
      { label: "Contrato da API (OpenAPI)", href: "/api/docs" },
    ].forEach(function (entry) {
      if (!seen[entry.href]) {
        seen[entry.href] = true;
        found.push({ label: entry.label, href: entry.href, hint: entry.href });
      }
    });

    return found;
  }

  /** Normaliza para comparar sem acento e sem caixa. */
  function fold(text) {
    return String(text)
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "");
  }

  /** Casamento por subsequencia: "adcom" casa "AdWatch · Comerciais". */
  function score(query, candidate) {
    if (!query) {
      return 1;
    }
    var needle = fold(query);
    var haystack = fold(candidate);
    if (haystack.indexOf(needle) !== -1) {
      return 100 - haystack.indexOf(needle);
    }
    var position = 0;
    for (var i = 0; i < needle.length; i += 1) {
      position = haystack.indexOf(needle[i], position);
      if (position === -1) {
        return 0;
      }
      position += 1;
    }
    return 1;
  }

  function build() {
    overlay = document.createElement("div");
    overlay.className = "lk-palette";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Paleta de comandos");

    var box = document.createElement("div");
    box.className = "lk-palette__box";

    input = document.createElement("input");
    input.className = "lk-palette__input";
    input.type = "search";
    input.autocomplete = "off";
    input.placeholder = "Ir para… (setas para navegar, Enter para abrir)";
    input.setAttribute("aria-label", "Buscar telas e comandos");

    list = document.createElement("div");
    list.className = "lk-palette__list";
    list.setAttribute("role", "listbox");

    box.appendChild(input);
    box.appendChild(list);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    overlay.addEventListener("mousedown", function (event) {
      if (event.target === overlay) {
        close();
      }
    });
    input.addEventListener("input", function () {
      render(input.value);
    });
    input.addEventListener("keydown", onKeydown);
  }

  function render(query) {
    matches = items
      .map(function (entry) {
        return { entry: entry, rank: score(query, entry.label + " " + entry.href) };
      })
      .filter(function (row) {
        return row.rank > 0;
      })
      .sort(function (a, b) {
        return b.rank - a.rank;
      })
      .slice(0, MAX_RESULTS)
      .map(function (row) {
        return row.entry;
      });

    cursor = 0;
    list.textContent = "";

    if (!matches.length) {
      var empty = document.createElement("p");
      empty.className = "lk-palette__empty";
      empty.textContent = "Nada encontrado.";
      list.appendChild(empty);
      return;
    }

    matches.forEach(function (entry, index) {
      var option = document.createElement("button");
      option.type = "button";
      option.className = "lk-palette__item";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", index === 0 ? "true" : "false");

      var label = document.createElement("span");
      label.textContent = entry.label;
      var hint = document.createElement("span");
      hint.className = "lk-palette__hint";
      hint.textContent = entry.hint || "";

      option.appendChild(label);
      option.appendChild(hint);
      option.addEventListener("click", function () {
        go(entry);
      });
      list.appendChild(option);
    });
  }

  function highlight() {
    var options = list.querySelectorAll(".lk-palette__item");
    Array.prototype.forEach.call(options, function (option, index) {
      option.setAttribute("aria-selected", index === cursor ? "true" : "false");
      if (index === cursor && option.scrollIntoView) {
        option.scrollIntoView({ block: "nearest" });
      }
    });
  }

  function onKeydown(event) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      cursor = matches.length ? (cursor + 1) % matches.length : 0;
      highlight();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      cursor = matches.length ? (cursor - 1 + matches.length) % matches.length : 0;
      highlight();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (matches[cursor]) {
        go(matches[cursor]);
      }
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  }

  function go(entry) {
    close();
    window.location.assign(entry.href);
  }

  function open(initial) {
    if (!overlay) {
      build();
    }
    items = collect();
    overlay.classList.add("is-open");
    input.value = initial || "";
    render(input.value);
    input.focus();
    input.select();
  }

  function close() {
    if (overlay) {
      overlay.classList.remove("is-open");
    }
  }

  function setup() {
    document.addEventListener("keydown", function (event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        open("");
      }
    });

    /* O campo da topbar vira o gatilho da paleta quando ha JavaScript; sem ele,
       o mesmo campo continua submetendo a busca do catalogo de modulos. */
    var search = document.querySelector("[data-palette-input]");
    if (search) {
      search.addEventListener("focus", function () {
        search.blur();
        open("");
      });
    }

    document.addEventListener("lukato:dismiss", close);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup);
  } else {
    setup();
  }

  window.lukato = Object.assign(window.lukato || {}, {
    palette: { open: open, close: close },
  });
})();
