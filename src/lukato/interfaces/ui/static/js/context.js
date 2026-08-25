/* context.js — o painel de contexto da direita (SPEC-0009 secao 7).
 *
 * A funcionalidade **nao depende deste arquivo**. Sem JavaScript, cada linha
 * selecionavel e um `<a href="?sel=<id>">` e o servidor renderiza o painel. Com
 * JavaScript, este script intercepta o clique, troca apenas o miolo
 * (`#lk-context-body`) por `GET /ui/context/{entity}/{id}` e ajusta a URL com
 * `history.replaceState` — a pagina inteira nao recarrega, e o link continua
 * compartilhavel.
 *
 * Em telas ate 1280px o painel e uma gaveta: selecionar um item tambem a abre.
 */

(function () {
  "use strict";

  var STORAGE_ASIDE = "lukato.aside";
  var DRAWER_BREAKPOINT = 1280;

  var root = document.documentElement;

  function panel() {
    return document.getElementById("lk-aside");
  }

  function body() {
    return document.getElementById("lk-context-body");
  }

  function isDrawer() {
    return window.matchMedia("(max-width: " + DRAWER_BREAKPOINT + "px)").matches;
  }

  function setAside(next) {
    root.setAttribute("data-aside", next);
    if (window.lukato && window.lukato.writeStorage) {
      window.lukato.writeStorage(STORAGE_ASIDE, next);
    }
    var buttons = document.querySelectorAll("[data-aside-toggle]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.setAttribute("aria-expanded", next === "open" ? "true" : "false");
    });
  }

  function toggleAside() {
    setAside(root.getAttribute("data-aside") === "closed" ? "open" : "closed");
  }

  /** Marca visualmente a linha escolhida e desmarca as demais. */
  function highlight(entity, id) {
    var rows = document.querySelectorAll("[data-context-id]");
    Array.prototype.forEach.call(rows, function (node) {
      if (node.tagName.toLowerCase() !== "tr") {
        return;
      }
      var same =
        node.getAttribute("data-context-id") === id &&
        node.getAttribute("data-context-entity") === entity;
      node.classList.toggle("is-selected", same);
      if (same) {
        node.setAttribute("aria-selected", "true");
      } else {
        node.removeAttribute("aria-selected");
      }
    });
  }

  /** Reescreve `?sel=` preservando todos os outros parametros da URL. */
  function rememberSelection(id) {
    try {
      var url = new URL(window.location.href);
      if (id) {
        url.searchParams.set("sel", id);
      } else {
        url.searchParams.delete("sel");
      }
      window.history.replaceState({}, "", url.toString());
    } catch (error) {
      /* navegador sem History API: a URL apenas nao acompanha a selecao */
    }
  }

  async function load(entity, id) {
    var host = body();
    var aside = panel();
    if (!host || !entity || !id) {
      return;
    }
    var base = (aside && aside.getAttribute("data-context-base")) || "/ui/context";
    host.setAttribute("aria-busy", "true");
    try {
      var html = await window.lukato.fetchHTML(
        base + "/" + encodeURIComponent(entity) + "/" + encodeURIComponent(id)
      );
      host.innerHTML = html;
      host.setAttribute("data-selected-id", id);
      if (aside) {
        aside.setAttribute("data-context-entity", entity);
      }
      highlight(entity, id);
      rememberSelection(id);
      if (isDrawer()) {
        setAside("open");
      }
    } catch (error) {
      window.lukato.toast("Não foi possível carregar os detalhes do item.", "danger");
    } finally {
      host.removeAttribute("aria-busy");
    }
  }

  function setup() {
    var aside = panel();
    if (!aside) {
      return;
    }

    document.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-context-id]");
      if (!trigger) {
        return;
      }
      var entity =
        trigger.getAttribute("data-context-entity") ||
        aside.getAttribute("data-context-entity") ||
        "";
      var id = trigger.getAttribute("data-context-id");
      if (!entity || !id) {
        return;
      }
      /* Deixa passar o clique que abre em outra aba ou janela. */
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
        return;
      }
      event.preventDefault();
      load(entity, id);
    });

    var toggles = document.querySelectorAll("[data-aside-toggle]");
    Array.prototype.forEach.call(toggles, function (button) {
      button.addEventListener("click", function (event) {
        event.preventDefault();
        toggleAside();
      });
    });

    document.addEventListener("lukato:dismiss", function () {
      if (isDrawer()) {
        setAside("closed");
      }
    });

    var selected = body() ? body().getAttribute("data-selected-id") : "";
    if (selected) {
      highlight(aside.getAttribute("data-context-entity") || "", selected);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup);
  } else {
    setup();
  }
})();
