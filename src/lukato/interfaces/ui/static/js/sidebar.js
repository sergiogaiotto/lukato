/* sidebar.js — recolher, expandir e lembrar o estado do menu lateral.
 *
 * O estado vive em `localStorage["lukato.sidebar"]` e e aplicado ao atributo
 * `data-sidebar` do elemento raiz. Quem escreve o valor no primeiro paint e o
 * script inline do `<head>` (`BOOT_SCRIPT` em `router.py`); este arquivo cuida
 * da interacao: cliques nos botoes, atalho `[` e o comportamento de gaveta em
 * telas estreitas.
 *
 * A largura em si e responsabilidade do CSS: `--lk-sidebar-w` troca de valor
 * conforme `data-sidebar`, e a grade de `layout.css` se ajusta sozinha.
 */

(function () {
  "use strict";

  var STORAGE_KEY = "lukato.sidebar";
  var EXPANDED = "expanded";
  var COLLAPSED = "collapsed";
  var SHORTCUT = "[";
  var OVERLAY_BREAKPOINT = 900;

  var root = document.documentElement;

  function state() {
    return root.getAttribute("data-sidebar") === COLLAPSED ? COLLAPSED : EXPANDED;
  }

  function apply(next) {
    root.setAttribute("data-sidebar", next);
    if (window.lukato && window.lukato.writeStorage) {
      window.lukato.writeStorage(STORAGE_KEY, next);
    }
    var buttons = document.querySelectorAll("[data-sidebar-toggle]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.setAttribute("aria-expanded", next === EXPANDED ? "true" : "false");
    });
  }

  function toggle() {
    apply(state() === COLLAPSED ? EXPANDED : COLLAPSED);
  }

  /** Em telas estreitas a sidebar e uma gaveta: fechar significa recolher. */
  function isOverlay() {
    return window.matchMedia("(max-width: " + OVERLAY_BREAKPOINT + "px)").matches;
  }

  function dismiss() {
    if (isOverlay() && state() === EXPANDED) {
      apply(COLLAPSED);
    }
  }

  /** Ignora o atalho enquanto a pessoa digita em um campo. */
  function typing(event) {
    var node = event.target;
    if (!node || !node.tagName) {
      return false;
    }
    var tag = node.tagName.toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || node.isContentEditable;
  }

  function setup() {
    apply(state());

    var buttons = document.querySelectorAll("[data-sidebar-toggle]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function (event) {
        event.preventDefault();
        toggle();
      });
    });

    document.addEventListener("keydown", function (event) {
      if (event.key !== SHORTCUT || event.ctrlKey || event.metaKey || event.altKey) {
        return;
      }
      if (typing(event)) {
        return;
      }
      event.preventDefault();
      toggle();
    });

    document.addEventListener("lukato:dismiss", dismiss);

    /* Em telas estreitas a navegacao fecha a gaveta ao seguir um link. */
    var links = document.querySelectorAll(".lk-sidebar .lk-nav__link");
    Array.prototype.forEach.call(links, function (link) {
      link.addEventListener("click", function () {
        if (isOverlay()) {
          apply(COLLAPSED);
        }
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup);
  } else {
    setup();
  }
})();
