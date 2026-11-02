// paper2audio reader: PDF view with selectable text, zoom, and highlight/underline/note
// annotations. Python serves page images, text positions and annotation storage; this
// file does everything in the browser. Exposed as window.P2A.
(() => {
  "use strict";

  const ICONS = window.P2A_ICONS || {};
  const COLORS = [
    ["Yellow", "#ffd400"], ["Red", "#ff6666"], ["Green", "#5fb236"], ["Blue", "#2ea8e5"],
    ["Purple", "#a28ae5"], ["Magenta", "#e56eee"], ["Orange", "#f19837"], ["Gray", "#aaaaaa"],
  ];
  const LEVELS = [50, 67, 80, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400];
  const MIN_ZOOM = 25, MAX_ZOOM = 400, MAX_RENDER = 8192, PREVIEW_WIDTH = 600;
  const TYPE_ICON = { highlight: "highlighter", underline: "underline", note: "sticky-note" };

  // Settings that apply to every tab.
  const ui = {
    tool: null,             // null (select text), "highlight", "underline", "note"
    color: COLORS[0][1],
    sidebarTab: "annotations",
    filter: "",
  };

  // Each open paper is a tab with its own scroll area, so switching tabs keeps the scroll
  // position, zoom and loaded pages. `state` is the active tab; on the Library tab it's
  // BLANK, which has no document, so the reader's controls and shortcuts do nothing.
  function makeTab(id, title) {
    const scroll = el("div", "p2a-scroll");
    const pagesEl = el("div", "p2a-pages");
    scroll.append(pagesEl);
    return {
      id, title, scroll, pagesEl,
      doc: null,            // {token, pages: [[w, h], ...]}, for PDFs
      zoom: 100,
      annotations: [],
      selected: null,       // id of the selected annotation
      outline: null,
      folded: new Set(),    // indexes of folded outline entries
      saveTimer: null,
    };
  }
  const tabs = [];
  let BLANK, state;

  const $ = (id) => document.getElementById(id);
  const icon = (name, cls = "") => `<span class="p2a-icon ${cls}">${ICONS[name] || ""}</span>`;
  const el = (tag, cls, html) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  };
  const pages = () => [...state.pagesEl.querySelectorAll(".p2a-page")];
  const pageEl = (i) => state.pagesEl.querySelector(`.p2a-page[data-index="${i}"]`);
  const notify = (message) => window.emitEvent && window.emitEvent("p2a_notify", message);
  const emitArgs = (name, args) => window.emitEvent && window.emitEvent(name, args);
  BLANK = state = makeTab("library", "Library");

  // --- tabs ---------------------------------------------------------------------------------

  // Called by Python: a new tab with a PDF ("pdf"), a structured paper ("blocks"), a text
  // file ("text") or an error message ("message").
  function openTab({ id, title, kind, doc, url, message, activate: show = true }) {
    let tab = tabs.find((t) => t.id === id);
    if (!tab) {
      tab = makeTab(id, title);
      tabs.push(tab);
      $("p2a-view").insertBefore(tab.scroll, $("p2a-popup"));
    }
    tab.title = title;
    tab.scroll.style.display = "none";
    if (kind === "pdf") open(tab, doc);
    else if (kind === "blocks") showBlocks(tab, url);
    else if (kind === "text") showText(tab, url);
    else showMessage(tab, "file-text", message || "");
    if (show) activate(id, false);
    else renderTabs();
  }

  // Show a tab ("library" for the Library). emit: tell Python (not needed when it asked).
  function activate(id, emit = true) {
    hidePopup();
    hideMenu();
    $("p2a-colors").classList.remove("p2a-open");
    window.getSelection()?.removeAllRanges();
    const tab = tabs.find((t) => t.id === id);
    state = tab || BLANK;
    tabs.forEach((t) => { t.scroll.style.display = t === state ? "" : "none"; });
    document.documentElement.classList.toggle("p2a-mode-library", !tab);
    renderTabs();
    if (tab) {
      setControlsEnabled(!!state.doc);
      $("p2a-zoom").textContent = state.doc ? `${Math.round(state.zoom)}%` : "";
      $("p2a-page-count").textContent = state.doc ? `of ${state.doc.pages.length}` : "";
      renderSidebar();
      sharpenSoon(30);
    } else {
      renderLibrary();
    }
    if (emit) emitArgs("p2a_tab", tab ? tab.id : "library");
  }

  function closeTab(id) {
    const index = tabs.findIndex((t) => t.id === id);
    if (index < 0) return;
    const [tab] = tabs.splice(index, 1);
    clearTimeout(tab.saveTimer);
    if (tab.saveTimer) saveNow(tab);  // don't lose annotations made just before closing
    tab.scroll.remove();
    emitArgs("p2a_tab_close", id);
    if (state === tab) {
      const next = tabs[index] || tabs[index - 1];
      activate(next ? next.id : "library");
    } else {
      renderTabs();
    }
  }

  function cycleTab(direction) {
    const ids = ["library", ...tabs.map((t) => t.id)];
    const index = ids.indexOf(state === BLANK ? "library" : state.id);
    activate(ids[(index + direction + ids.length) % ids.length]);
  }

  function renderTabs() {
    const list = $("p2a-tablist");
    list.innerHTML = "";
    const libraryTab = el("div", `p2a-tab p2a-tab-library${state === BLANK ? " p2a-on" : ""}`, icon("library"));
    libraryTab.title = "Library";
    libraryTab.addEventListener("click", () => activate("library"));
    list.append(libraryTab);
    for (const tab of tabs) {
      const item = el("div", `p2a-tab${tab === state ? " p2a-on" : ""}`, icon(tab.doc ? "file-text" : "book-open"));
      const label = el("span", "p2a-tab-title");
      label.textContent = tab.title;
      item.title = tab.title;
      const close = el("button", "p2a-tab-close", icon("x"));
      close.title = `Close tab (${IS_MAC ? "⌘" : "Ctrl+"}W)`;
      close.addEventListener("click", (e) => { e.stopPropagation(); closeTab(tab.id); });
      item.append(label, close);
      item.addEventListener("click", () => activate(tab.id));
      item.addEventListener("auxclick", (e) => { if (e.button === 1) { e.preventDefault(); closeTab(tab.id); } });
      list.append(item);
    }
    list.querySelector(".p2a-tab.p2a-on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  // --- opening documents ------------------------------------------------------------------

  function open(tab, doc) {
    tab.doc = doc;
    tab.annotations = [];
    tab.selected = null;
    tab.outline = null;
    tab.folded = new Set();
    const container = tab.pagesEl;
    container.innerHTML = "";
    doc.pages.forEach(([w, h], i) => {
      const page = el("div", "p2a-page");
      page.dataset.index = i;
      page.style.aspectRatio = `${w} / ${h}`;
      const img = el("img");
      img.loading = "lazy";
      img.alt = `Page ${i + 1}`;
      img.dataset.base = `/pdf/${doc.token}/${i}.png`;
      img.draggable = false;
      page.append(img, el("div", "p2a-marks"), el("div", "p2a-text"), el("div", "p2a-notes"));
      container.append(page);
    });
    fetch(`/annotations/${doc.token}`).then((r) => r.json()).then((list) => {
      if (tab.doc !== doc) return;
      tab.annotations = list;
      renderAllMarks(tab);
      if (state === tab) renderSidebar();
    });
    fetch(`/pdf/${doc.token}/outline`).then((r) => r.json()).then((items) => {
      if (tab.doc !== doc) return;
      tab.outline = items;
      if (state === tab && ui.sidebarTab === "outline") renderSidebar();
    });
  }

  function showMessage(tab, iconName, text) {
    tab.doc = null;
    tab.annotations = [];
    const box = el("div", "p2a-message", icon(iconName));
    box.append(el("div", "", ""));
    box.lastChild.textContent = text;
    tab.pagesEl.innerHTML = "";
    tab.pagesEl.append(box);
  }

  function showText(tab, url) {
    showMessage(tab, "file-text", "Loading…");
    fetch(url).then((r) => r.text()).then((text) => {
      const doc = el("div", "p2a-textdoc");
      doc.textContent = text;
      tab.pagesEl.innerHTML = "";
      tab.pagesEl.append(doc);
    });
  }

  // Structured papers (HTML, JATS XML, EPUB, LaTeX): the labeled blocks they're read from,
  // shown as a formatted document. Text only: downloaded pages never run as web pages here.
  function showBlocks(tab, url) {
    showMessage(tab, "file-text", "Loading…");
    fetch(url).then((r) => {
      if (!r.ok) return r.json().then((e) => { throw new Error(e.detail || r.statusText); });
      return r.json();
    }).then((blocks) => {
      const doc = el("article", "p2a-textdoc p2a-blocks");
      let inAppendix = false;
      for (const [kind, text, appendix] of blocks) {
        if (appendix && !inAppendix) {
          inAppendix = true;
          doc.append(el("div", "p2a-block-divider", "Appendix"));
        }
        const tag = { title: "h1", heading: "h2", equation: "div", table: "pre" }[kind] || "p";
        const block = el(tag, `p2a-block p2a-block-${kind}`);
        // Math is wrapped in U+2063 ... U+2064 by the server: set it in italics.
        text.split(/(⁣[^⁤]*⁤)/).forEach((part) => {
          if (part.startsWith("⁣")) {
            const math = el("span", "p2a-math");
            // "x_1", "d_model", "K^T" -> sub- and superscripts
            part.slice(1, -1).split(/([_^](?:\w+|\([^)]*\)))/).forEach((piece) => {
              if (/^[_^]./.test(piece)) {
                const script = el(piece[0] === "_" ? "sub" : "sup");
                script.textContent = piece.slice(1).replace(/^\((.*)\)$/, "$1");
                math.append(script);
              } else if (piece) {
                math.append(document.createTextNode(piece));
              }
            });
            block.append(math);
          } else if (part) {
            block.append(document.createTextNode(part));
          }
        });
        if (kind === "abstract") block.prepend(el("strong", "", "Abstract. "));
        if (kind === "footnote") block.prepend(el("span", "p2a-block-label", "Footnote: "));
        if (kind === "keywords" && !/^\s*key\s?words/i.test(text)) block.prepend(el("span", "p2a-block-label", "Keywords: "));
        doc.append(block);
      }
      if (!blocks.length) doc.append(el("p", "p2a-empty", "No readable text found in this file."));
      tab.pagesEl.innerHTML = "";
      tab.pagesEl.append(doc);
    }).catch((e) => showMessage(tab, "file-text", `Can’t show this file: ${e.message}`));
  }

  function setControlsEnabled(enabled) {
    document.querySelectorAll("[data-needs-pdf]").forEach((b) => { b.disabled = !enabled; });
    $("p2a-page-input").disabled = !enabled;
    if (!enabled) setTool(null);
  }

  // --- rendering pages and text -----------------------------------------------------------

  // Pages within about a screen of the visible area get full resolution (exactly their size
  // on screen, so text is sharp) and a text layer; pages further away get a small preview.
  function sharpen() {
    const scroller = state.scroll;
    if (!state.doc) return;
    const dpr = window.devicePixelRatio || 1;
    const view = scroller.getBoundingClientRect();
    pages().forEach((page) => {
      const r = page.getBoundingClientRect();
      const near = r.bottom > view.top - view.height && r.top < view.bottom + view.height;
      const full = Math.min(MAX_RENDER, Math.round(r.width * dpr));
      const width = near ? full : Math.min(full, PREVIEW_WIDTH);
      const img = page.querySelector("img");
      if (width > 0 && img.dataset.width != width) {
        img.dataset.width = width;
        img.src = `${img.dataset.base}?w=${width}`;
      }
      if (near) loadText(page);
    });
    updatePage();
  }
  let sharpenTimer;
  const sharpenSoon = (delay = 200) => { clearTimeout(sharpenTimer); sharpenTimer = setTimeout(sharpen, delay); };

  function loadText(page) {
    if (page.dataset.text) return;
    page.dataset.text = "loading";
    const tab = state, doc = state.doc;
    fetch(`/pdf/${doc.token}/${page.dataset.index}/text`).then((r) => r.json()).then((items) => {
      // Text is measured to fit it to the page, which needs the tab to be shown.
      if (tab.doc !== doc || state !== tab) { page.dataset.text = ""; return; }
      const layer = page.querySelector(".p2a-text");
      const spans = [];
      let previous = null;
      for (const [x, y, w, h, fontSize, text] of items) {
        if (previous) {
          // Line breaks and spaces between runs, so copied text reads naturally.
          const newLine = y > previous.y + previous.h * 0.5;
          layer.append(newLine ? document.createElement("br") : document.createTextNode(" "));
        }
        const span = el("span");
        span.textContent = text;
        span.style.left = `${x * 100}%`;
        span.style.top = `${y * 100}%`;
        span.style.fontSize = `${fontSize * 100}cqw`;
        span.dataset.w = w;
        layer.append(span);
        spans.push(span);
        previous = { y, h };
      }
      // Stretch each run to the width of the printed text. Sizes are in cqw, so the
      // ratio stays right at every zoom level. Measure everything first, then write.
      const pageWidth = page.getBoundingClientRect().width;
      const natural = spans.map((s) => s.getBoundingClientRect().width);
      spans.forEach((s, i) => {
        if (natural[i] > 0) s.style.transform = `scaleX(${(s.dataset.w * pageWidth) / natural[i]})`;
      });
      page.dataset.text = "loaded";
    }).catch(() => { page.dataset.text = ""; });
  }

  // --- navigation and zoom ----------------------------------------------------------------

  function currentPage() {
    const scroller = state.scroll;
    const line = scroller.getBoundingClientRect().top + scroller.clientHeight / 3;
    let current = 0;
    pages().forEach((p, i) => { if (p.getBoundingClientRect().top <= line) current = i; });
    return current;
  }

  function updatePage() {
    const input = $("p2a-page-input");
    if (!state.doc) { input.value = ""; return; }
    const current = currentPage();
    if (document.activeElement !== input) input.value = current + 1;
    document.querySelectorAll(".p2a-thumb").forEach((t, i) => t.classList.toggle("p2a-current", i === current));
  }

  function goToPage(index, fraction = 0) {
    const page = pageEl(Math.max(0, Math.min(index, (state.doc?.pages.length || 1) - 1)));
    if (!page) return;
    const scroller = state.scroll;
    scroller.scrollTop = page.offsetTop + fraction * page.offsetHeight - (fraction ? scroller.clientHeight / 3 : 12);
    sharpenSoon(50);
  }

  // Zoom is a percentage of the fitted width. (x, y): point to keep in place, in window
  // coordinates; by default the centre of the view.
  function setZoom(value, x, y) {
    if (!state.doc) return;
    const scroller = state.scroll, container = state.pagesEl;
    value = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, value));
    const box = scroller.getBoundingClientRect();
    if (x === undefined) { x = box.left + box.width / 2; y = box.top + box.height / 2; }
    const before = container.getBoundingClientRect();
    const fx = (x - before.left) / before.width, fy = (y - before.top) / before.height;
    state.zoom = value;
    container.style.width = `${value}%`;
    const after = container.getBoundingClientRect();
    scroller.scrollLeft += after.left + fx * after.width - x;
    scroller.scrollTop += after.top + fy * after.height - y;
    $("p2a-zoom").textContent = `${Math.round(value)}%`;
    hidePopup();
    sharpenSoon();
  }
  function zoomStep(direction) {
    const next = direction > 0
      ? LEVELS.find((l) => l > state.zoom + 0.5)
      : [...LEVELS].reverse().find((l) => l < state.zoom - 0.5);
    setZoom(next ?? (direction > 0 ? MAX_ZOOM : MIN_ZOOM));
  }

  // --- tools and colors -------------------------------------------------------------------

  function setTool(tool) {
    ui.tool = ui.tool === tool ? null : tool;
    document.querySelectorAll("[data-action^='tool-']").forEach((b) => {
      b.classList.toggle("p2a-on", b.dataset.action === `tool-${ui.tool}`);
    });
    $("p2a-view").classList.toggle("p2a-tool-note", ui.tool === "note");
    hidePopup();
  }

  function setColor(color) {
    ui.color = color;
    $("p2a-color-swatch").style.background = color;
    $("p2a-colors").classList.remove("p2a-open");
  }

  function toggleColorMenu(button) {
    const menu = $("p2a-colors");
    if (menu.classList.contains("p2a-open")) { menu.classList.remove("p2a-open"); return; }
    menu.innerHTML = "";
    for (const [name, value] of COLORS) {
      const item = el("div", "p2a-menu-item", `<span class="p2a-dot" style="background:${value}"></span>`);
      item.append(document.createTextNode(name));
      item.addEventListener("click", () => setColor(value));
      menu.append(item);
    }
    const b = button.getBoundingClientRect();
    menu.style.left = `${b.left}px`;
    menu.style.top = `${b.bottom + 4}px`;
    menu.classList.add("p2a-open");
  }

  // --- creating annotations ---------------------------------------------------------------

  // The parts of a text selection, per page: rectangles (as fractions of the page) and text.
  function selectionByPage(range) {
    const byPage = new Map();
    state.pagesEl.querySelectorAll(".p2a-text span").forEach((span) => {
      const node = span.firstChild;
      if (!node || !range.intersectsNode(span)) return;
      const sub = document.createRange();
      sub.setStart(node, node === range.startContainer ? range.startOffset : 0);
      sub.setEnd(node, node === range.endContainer ? range.endOffset : node.length);
      const text = sub.toString();
      const box = sub.getBoundingClientRect();
      if (!text.trim() || box.width < 1) return;
      const page = span.closest(".p2a-page"), p = page.getBoundingClientRect();
      const index = +page.dataset.index;
      if (!byPage.has(index)) byPage.set(index, { rects: [], parts: [] });
      const entry = byPage.get(index);
      entry.rects.push([(box.left - p.left) / p.width, (box.top - p.top) / p.height, box.width / p.width, box.height / p.height]);
      entry.parts.push(text);
    });
    return [...byPage.entries()].map(([page, { rects, parts }]) => ({ page, rects: mergeRects(rects), text: joinParts(parts) }));
  }

  // Merge rectangles of runs on the same line into one per line.
  function mergeRects(rects) {
    const merged = [];
    for (const r of [...rects].sort((a, b) => a[1] - b[1] || a[0] - b[0])) {
      const last = merged[merged.length - 1];
      const sameLine = last && Math.abs(last[1] - r[1]) < Math.min(last[3], r[3]) * 0.5;
      if (sameLine && r[0] - (last[0] + last[2]) < r[3] * 2) {
        const left = Math.min(last[0], r[0]), top = Math.min(last[1], r[1]);
        const right = Math.max(last[0] + last[2], r[0] + r[2]), bottom = Math.max(last[1] + last[3], r[1] + r[3]);
        merged[merged.length - 1] = [left, top, right - left, bottom - top];
      } else {
        merged.push(r);
      }
    }
    return merged;
  }

  function joinParts(parts) {
    let text = "";
    for (const part of parts.map((p) => p.trim()).filter(Boolean)) {
      if (text.endsWith("-") && /^[a-z]/.test(part)) text = text.slice(0, -1) + part;  // "transduc-" "tion"
      else text += (text ? " " : "") + part;
    }
    return text;
  }

  function createFromSelection(type, color) {
    const selection = window.getSelection();
    if (!selection.rangeCount || selection.isCollapsed) return;
    const parts = selectionByPage(selection.getRangeAt(0));
    selection.removeAllRanges();
    hidePopup();
    let last = null;
    for (const part of parts) {
      last = add({ type, color, page: part.page, rects: part.rects, text: part.text });
    }
    if (last) select(last.id, { scrollCard: true });
  }

  function add(fields) {
    const annotation = {
      id: (crypto.randomUUID && crypto.randomUUID()) || String(Date.now() + Math.random()),
      comment: "", created: new Date().toISOString(), text: "", ...fields,
    };
    state.annotations.push(annotation);
    renderMarks(annotation.page);
    renderSidebar();
    save();
    return annotation;
  }

  function remove(id) {
    const annotation = state.annotations.find((a) => a.id === id);
    if (!annotation) return;
    state.annotations = state.annotations.filter((a) => a.id !== id);
    if (state.selected === id) state.selected = null;
    renderMarks(annotation.page);
    renderSidebar();
    save();
  }

  function recolor(id, color) {
    const annotation = state.annotations.find((a) => a.id === id);
    if (!annotation) return;
    annotation.color = color;
    renderMarks(annotation.page);
    renderSidebar();
    save();
  }

  function save(tab = state) {
    if (!tab.doc) return;
    clearTimeout(tab.saveTimer);
    tab.saveTimer = setTimeout(() => saveNow(tab), 400);
  }
  function saveNow(tab) {
    tab.saveTimer = null;
    fetch(`/annotations/${tab.doc.token}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(tab.annotations),
    }).then((r) => { if (!r.ok) throw new Error(r.statusText); })
      .catch((e) => notify(`Couldn't save annotations: ${e.message}`));
  }

  // --- drawing annotations on the pages ---------------------------------------------------

  function renderAllMarks(tab = state) {
    if (tab.doc) tab.doc.pages.forEach((_, i) => renderMarks(i, tab));
  }

  function renderMarks(index, tab = state) {
    const page = tab.pagesEl.querySelector(`.p2a-page[data-index="${index}"]`);
    if (!page) return;
    const marks = page.querySelector(".p2a-marks"), notes = page.querySelector(".p2a-notes");
    marks.innerHTML = "";
    notes.innerHTML = "";
    for (const a of tab.annotations.filter((a) => a.page === index)) {
      const selected = a.id === tab.selected ? " p2a-selected" : "";
      if (a.type === "note") {
        const note = el("div", `p2a-note${selected}`, icon("sticky-note"));
        note.style.left = `${a.position[0] * 100}%`;
        note.style.top = `${a.position[1] * 100}%`;
        note.style.background = a.color;
        note.title = a.comment || "Note";
        note.addEventListener("mousedown", (e) => e.stopPropagation());
        note.addEventListener("click", (e) => { e.stopPropagation(); select(a.id, { scrollCard: true }); });
        notes.append(note);
        continue;
      }
      for (const [x, y, w, h] of a.rects) {
        const mark = el("div", (a.type === "underline" ? "p2a-mark-ul" : "p2a-mark-hl") + selected);
        mark.style.left = `${x * 100}%`;
        mark.style.top = `${y * 100}%`;
        mark.style.width = `${w * 100}%`;
        mark.style.height = `${h * 100}%`;
        if (a.type === "underline") mark.style.borderColor = a.color;
        else mark.style.background = a.color;
        marks.append(mark);
      }
    }
  }

  function annotationAt(index, x, y) {
    // Notes are clickable elements; this finds highlights and underlines under a point.
    return state.annotations.find((a) => a.page === index && a.rects &&
      a.rects.some(([rx, ry, rw, rh]) => x >= rx && x <= rx + rw && y >= ry && y <= ry + rh));
  }

  function select(id, { scrollCard = false, scrollPage = false } = {}) {
    const previous = state.annotations.find((a) => a.id === state.selected);
    state.selected = id;
    if (previous) renderMarks(previous.page);
    const annotation = state.annotations.find((a) => a.id === id);
    if (annotation) {
      renderMarks(annotation.page);
      if (scrollPage) {
        const y = annotation.position ? annotation.position[1] : annotation.rects[0][1];
        goToPage(annotation.page, y);
      }
    }
    if (scrollCard && ui.sidebarTab !== "annotations") ui.sidebarTab = "annotations";
    if (annotation && scrollCard && $("p2a-sidebar").classList.contains("p2a-hidden")) toggleSidebar();
    renderSidebar();
    if (annotation && scrollCard) {
      const card = document.querySelector(`.p2a-card[data-id="${CSS.escape(id)}"]`);
      if (card) card.scrollIntoView({ block: "nearest" });
    }
  }

  // --- selection popup --------------------------------------------------------------------

  function showPopup(range) {
    const popup = $("p2a-popup"), view = $("p2a-view").getBoundingClientRect();
    popup.innerHTML = "";
    for (const [name, value] of COLORS) {
      const dot = el("button", "p2a-dot");
      dot.style.background = value;
      dot.title = `Highlight (${name})`;
      dot.addEventListener("click", () => createFromSelection("highlight", value));
      popup.append(dot);
    }
    popup.append(el("div", "p2a-sep"));
    const underline = el("button", "p2a-btn", icon("underline"));
    underline.title = "Underline";
    underline.addEventListener("click", () => createFromSelection("underline", ui.color));
    const copy = el("button", "p2a-btn", icon("copy"));
    copy.title = "Copy";
    copy.addEventListener("click", () => {
      const sel = window.getSelection();
      const text = sel.rangeCount ? joinParts(selectionByPage(sel.getRangeAt(0)).map((p) => p.text)) : "";
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).catch(() => document.execCommand("copy"));
      hidePopup();
    });
    popup.append(underline, copy);
    popup.classList.add("p2a-open");
    const r = range.getBoundingClientRect(), p = popup.getBoundingClientRect();
    const left = Math.max(8, Math.min(r.left + r.width / 2 - p.width / 2 - view.left, view.width - p.width - 8));
    let top = r.bottom - view.top + 8;
    if (top + p.height > view.height - 8) top = r.top - view.top - p.height - 8;
    popup.style.left = `${left}px`;
    popup.style.top = `${Math.max(8, top)}px`;
  }
  function hidePopup() { $("p2a-popup")?.classList.remove("p2a-open"); }

  // --- left sidebar -----------------------------------------------------------------------

  function toggleSidebar() {
    $("p2a-sidebar").classList.toggle("p2a-hidden");
    document.querySelector("[data-action='sidebar']").classList.toggle("p2a-on", !$("p2a-sidebar").classList.contains("p2a-hidden"));
    sharpenSoon(50);
  }

  function setSidebarTab(tab) {
    ui.sidebarTab = tab;
    renderSidebar();
  }

  function renderSidebar() {
    document.querySelectorAll("[data-action^='tab-']").forEach((b) => {
      b.classList.toggle("p2a-on", b.dataset.action === `tab-${ui.sidebarTab}`);
    });
    $("p2a-sidebar-search").style.display = ui.sidebarTab === "annotations" ? "" : "none";
    const body = $("p2a-sidebar-body");
    const focused = document.activeElement;
    if (focused && body.contains(focused) && focused.isContentEditable) return;  // don't interrupt typing
    body.innerHTML = "";
    if (!state.doc) {
      body.append(el("div", "p2a-empty", "Annotations, pages and the outline are shown for PDFs."));
      return;
    }
    if (ui.sidebarTab === "thumbnails") renderThumbnails(body);
    else if (ui.sidebarTab === "outline") renderOutline(body);
    else renderCards(body);
  }

  function sortKey(a) {
    const y = a.position ? a.position[1] : a.rects[0]?.[1] ?? 0;
    const x = a.position ? a.position[0] : a.rects[0]?.[0] ?? 0;
    return a.page * 10 + y * 2 + x * 0.01;
  }

  function renderCards(body) {
    const filter = ui.filter.trim().toLowerCase();
    const list = state.annotations
      .filter((a) => !filter || `${a.text} ${a.comment}`.toLowerCase().includes(filter))
      .sort((a, b) => sortKey(a) - sortKey(b));
    if (!list.length) {
      body.append(el("div", "p2a-empty", filter ? "No matching annotations."
        : "Select text to highlight or underline it, or pick the note tool and click on a page."));
      return;
    }
    for (const a of list) {
      const card = el("div", `p2a-card${a.id === state.selected ? " p2a-selected" : ""}`);
      card.dataset.id = a.id;
      const head = el("div", "p2a-card-head", `<span style="color:${a.color}">${icon(TYPE_ICON[a.type])}</span>`);
      head.append(el("span", "p2a-grow", `Page ${a.page + 1}`));
      const del = el("button", "p2a-btn", icon("trash-2"));
      del.title = "Delete";
      del.addEventListener("click", (e) => { e.stopPropagation(); remove(a.id); });
      head.append(del);
      card.append(head);
      if (a.type !== "note" && a.text) {
        const quote = el("div", "p2a-card-text");
        quote.textContent = a.text;
        quote.style.borderColor = a.color;
        card.append(quote);
      }
      const comment = el("div", "p2a-card-comment");
      comment.contentEditable = "true";
      comment.dataset.placeholder = "Add comment…";
      comment.textContent = a.comment;
      comment.addEventListener("focus", () => { if (state.selected !== a.id) { state.selected = a.id; renderAllMarks(); card.classList.add("p2a-selected"); } });
      comment.addEventListener("input", () => { a.comment = comment.innerText.replace(/\n$/, ""); save(); });
      comment.addEventListener("blur", () => renderSidebar());
      card.append(comment);
      const colors = el("div", "p2a-card-colors");
      for (const [name, value] of COLORS) {
        const dot = el("button", `p2a-dot${value === a.color ? " p2a-on" : ""}`);
        dot.style.background = value;
        dot.title = name;
        dot.addEventListener("click", (e) => { e.stopPropagation(); recolor(a.id, value); });
        colors.append(dot);
      }
      card.append(colors);
      card.addEventListener("click", (e) => {
        if (e.target === comment) return;
        select(a.id, { scrollPage: true });
      });
      body.append(card);
    }
  }

  function renderThumbnails(body) {
    const dpr = window.devicePixelRatio || 1;
    state.doc.pages.forEach(([w, h], i) => {
      const thumb = el("div", "p2a-thumb");
      const img = el("img");
      img.loading = "lazy";
      img.style.setProperty("--ratio", `${w} / ${h}`);
      img.src = `/pdf/${state.doc.token}/${i}.png?w=${Math.round(120 * dpr)}`;
      thumb.append(img, el("span", "", String(i + 1)));
      thumb.addEventListener("click", () => goToPage(i));
      body.append(thumb);
    });
    updatePage();
  }

  // The outline is a flat list of [level, title, page]; an entry's children are the
  // entries after it with a deeper level. Entries with children can be folded.
  function hasChildren(i) {
    return i + 1 < state.outline.length && state.outline[i + 1][0] > state.outline[i][0];
  }

  function renderOutline(body) {
    if (state.outline === null) { body.append(el("div", "p2a-empty", "Loading…")); return; }
    if (!state.outline.length) { body.append(el("div", "p2a-empty", "This PDF has no outline.")); return; }
    const foldable = state.outline.map((_, i) => i).filter(hasChildren);
    if (foldable.length) {
      const bar = el("div", "p2a-outline-bar");
      const expand = el("button", "p2a-btn", icon("chevrons-up-down"));
      expand.title = "Expand all";
      expand.addEventListener("click", () => { state.folded.clear(); renderSidebar(); });
      const collapse = el("button", "p2a-btn", icon("chevrons-down-up"));
      collapse.title = "Collapse all";
      collapse.addEventListener("click", () => { state.folded = new Set(foldable); renderSidebar(); });
      bar.append(expand, collapse);
      body.append(bar);
    }
    let hiddenBelow = Infinity;  // while inside a folded entry: its level
    state.outline.forEach(([level, title, page], i) => {
      if (level > hiddenBelow) return;
      hiddenBelow = Infinity;
      const folded = state.folded.has(i);
      if (folded) hiddenBelow = level;

      const item = el("div", "p2a-outline-item");
      item.style.paddingLeft = `${2 + level * 14}px`;
      if (hasChildren(i)) {
        const twisty = el("button", `p2a-outline-twisty${folded ? "" : " p2a-open"}`, icon("chevron-right"));
        twisty.title = folded ? "Expand" : "Collapse";
        twisty.addEventListener("click", (e) => {
          e.stopPropagation();
          // Alt-click folds or unfolds this entry and everything under it.
          const ids = [i];
          if (e.altKey) {
            for (let k = i + 1; k < state.outline.length && state.outline[k][0] > level; k++) {
              if (hasChildren(k)) ids.push(k);
            }
          }
          ids.forEach((k) => (folded ? state.folded.delete(k) : state.folded.add(k)));
          renderSidebar();
        });
        item.append(twisty);
      } else {
        item.append(el("span", "p2a-outline-spacer"));
      }
      const label = el("span", "p2a-grow");
      label.textContent = title;
      if (level === 0) label.classList.add("p2a-outline-top");
      item.title = title;  // full title on hover; long ones are cut off with "…"
      item.append(label);
      if (page !== null) {
        item.append(el("span", "p2a-outline-page", String(page + 1)));
        item.addEventListener("click", () => goToPage(page));
      }
      body.append(item);
    });
  }

  // --- audio player -------------------------------------------------------------------------
  // In the Audio section: plays the active paper's audio. Switching to a paper without audio
  // keeps whatever is playing; the player is greyed out only when nothing is loaded.

  const audioEl = () => $("p2a-audio");
  const SKIP = 10;  // seconds
  let audioUrl = null, seeking = false;
  const clock = (t) => {
    if (!isFinite(t) || t < 0) t = 0;
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = Math.floor(t % 60);
    return `${h ? `${h}:${String(m).padStart(2, "0")}` : m}:${String(sec).padStart(2, "0")}`;
  };

  function setAudio(url, name) {
    const audio = audioEl();
    if (url === audioUrl) return;
    if (!url && audioUrl && !audio.paused) return;  // keep playing another paper's audio
    audioUrl = url;
    if (url) {
      audio.src = url;
      audio.load();
    } else {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    const label = $("p2a-player-name");
    label.textContent = url ? name : "No audio yet";
    label.title = url ? name : "Convert the paper to get audio";
    $("p2a-player").classList.toggle("p2a-disabled", !url);
    $("p2a-player").querySelectorAll("button, input").forEach((c) => { c.disabled = !url; });
    updatePlayer();
  }

  function updatePlayer() {
    const audio = audioEl();
    const duration = isFinite(audio.duration) ? audio.duration : 0;
    $("p2a-audio-time").textContent = clock(audio.currentTime);
    $("p2a-audio-duration").textContent = clock(duration);
    const seek = $("p2a-audio-seek");
    if (!seeking) seek.value = duration ? Math.round((audio.currentTime / duration) * 1000) : 0;
    seek.style.setProperty("--p2a-fill", `${seek.value / 10}%`);
    const play = document.querySelector("[data-action='audio-play']");
    const playing = !audio.paused && !audio.ended;
    play.innerHTML = icon(playing ? "pause" : "play");
    play.title = playing ? "Pause" : "Play";
  }

  function audioAction(action) {
    const audio = audioEl();
    if (!audioUrl) return;
    if (action === "audio-play") (audio.paused || audio.ended ? audio.play() : audio.pause());
    else if (action === "audio-back") audio.currentTime = Math.max(0, audio.currentTime - SKIP);
    else if (action === "audio-forward") audio.currentTime = Math.min(audio.duration || Infinity, audio.currentTime + SKIP);
    else if (action === "audio-reveal") emitArgs("p2a_reveal", audioUrl.split("/").pop());
    updatePlayer();
  }

  // The pane is drawn after this script runs, and media events don't bubble: listen on the
  // document in the capture phase.
  ["timeupdate", "play", "pause", "ended", "loadedmetadata", "durationchange", "emptied"].forEach((name) => {
    document.addEventListener(name, (e) => { if (e.target.id === "p2a-audio") updatePlayer(); }, true);
  });
  document.addEventListener("input", (e) => {
    if (e.target.id !== "p2a-audio-seek") return;
    seeking = true;
    $("p2a-audio-time").textContent = clock((e.target.value / 1000) * (audioEl().duration || 0));
    e.target.style.setProperty("--p2a-fill", `${e.target.value / 10}%`);
  });
  document.addEventListener("change", (e) => {
    if (e.target.id !== "p2a-audio-seek") return;
    seeking = false;
    const audio = audioEl();
    if (audio.duration) audio.currentTime = (e.target.value / 1000) * audio.duration;
  });

  // --- library ----------------------------------------------------------------------------
  // Python sends the papers (local ones merged with Zotero's) and collections; changes are
  // sent back as "p2a_lib" events, after which Python sends the library again.

  const lib = {
    papers: [], byId: new Map(), collections: [], zotero: null, home: "",
    view: null,              // "all", "unfiled", "recent", "c:<id>", "zl:<library>", "zc:<collection>"
    sort: { key: "title", dir: 1 },
    selected: new Set(),
    anchor: null,            // for shift-click ranges
    filter: "",
    collapsed: new Set(),    // tree nodes that are folded
    editing: null,           // {id, parent, isNew, addIds}: a collection name being typed
  };
  const libEmit = (action, args = {}) => emitArgs("p2a_lib", { action, ...args });
  const newId = () => (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36));
  const IS_MAC_UI = /Mac/.test(navigator.platform);

  function setLibrary(data) {
    lib.papers = data.papers;
    lib.byId = new Map(data.papers.map((p) => [p.id, p]));
    lib.collections = data.collections;
    lib.zotero = data.zotero;
    lib.home = data.home;
    lib.selected = new Set([...lib.selected].filter((id) => lib.byId.has(id)));
    if (lib.view === null) lib.view = data.papers.some((p) => p.added) ? "all" : "recent";
    if (!viewExists(lib.view)) lib.view = "all";
    if (state === BLANK) renderLibrary();
    if ($("p2a-picker").classList.contains("p2a-open")) renderPicker();
  }

  function viewExists(view) {
    const [kind, id] = [view.split(":")[0], view.slice(view.indexOf(":") + 1)];
    if (kind === "c") return lib.collections.some((c) => c.id === id);
    if (kind === "zl") return !!lib.zotero?.libraries.some((l) => String(l.id) === id);
    if (kind === "zc") return !!lib.zotero?.collections.some((c) => String(c.id) === id);
    return ["all", "unfiled", "recent"].includes(view);
  }

  function viewPapers(view = lib.view) {
    const [kind, id] = [view.split(":")[0], view.slice(view.indexOf(":") + 1)];
    if (view === "all") return lib.papers.filter((p) => p.added);
    if (view === "unfiled") return lib.papers.filter((p) => p.added && !p.collections.length);
    if (view === "recent") return lib.papers.filter((p) => p.opened);
    if (kind === "c") return lib.papers.filter((p) => p.collections.includes(id));
    if (kind === "zl") return lib.papers.filter((p) => String(p.zotero) === id);
    if (kind === "zc") return lib.papers.filter((p) => p.zcollections.some((c) => String(c) === id));
    return [];
  }

  function creators(authors) {
    if (!authors.length) return "";
    if (authors.length === 1) return authors[0];
    if (authors.length === 2) return `${authors[0]} and ${authors[1]}`;
    return `${authors[0]} et al.`;
  }
  const zoteroName = (library) => (library?.name === "My Library" ? "Zotero library" : library?.name);
  const tilde = (path) => (lib.home && path.startsWith(lib.home + "/") ? "~" + path.slice(lib.home.length) : path);
  const baseName = (path) => path.split(/[\\/]/).pop();
  const folderOf = (path) => tilde(path.slice(0, path.length - baseName(path).length - 1));
  const shortDate = (seconds) => (seconds
    ? new Date(seconds * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "");

  // Search: every word must be found in the title, authors, year or file name.
  function matchScore(p, words) {
    const title = p.title.toLowerCase(), authors = p.authors.join(" ").toLowerCase();
    const file = baseName(p.path).toLowerCase();
    let total = 0;
    for (const word of words) {
      const at = title.indexOf(word);
      if (at === 0) total += 30;
      else if (at > 0) total += /[\w]/.test(title[at - 1]) ? 10 : 20;
      else if (authors.startsWith(word) || authors.includes(" " + word)) total += 15;
      else if (authors.includes(word)) total += 8;
      else if (p.year && p.year.startsWith(word)) total += 12;
      else if (file.includes(word)) total += 5;
      else return -1;
    }
    return total;
  }
  const searchWords = (text) => text.toLowerCase().split(/\s+/).filter(Boolean);

  function sortedItems() {
    const words = searchWords(lib.filter);
    let list = viewPapers();
    if (words.length) list = list.filter((p) => matchScore(p, words) >= 0);
    const { key, dir } = lib.sort;
    const value = (p) => key === "creator" ? (p.authors[0] || "").toLowerCase()
      : key === "year" ? p.year || ""
      : key === "date" ? (lib.view === "recent" ? p.opened : p.added) || 0
      : p.title.toLowerCase();
    return list.sort((a, b) => {
      const x = value(a), y = value(b);
      return (x < y ? -1 : x > y ? 1 : a.title.localeCompare(b.title)) * dir;
    });
  }

  function renderLibrary() {
    renderTree();
    renderItems();
    const sync = document.querySelector("[data-action='lib-sync']");
    if (sync) sync.style.display = lib.zotero ? "" : "none";
  }

  // --- collections tree

  function treeItem({ view, label, iconName, depth, count, children, collection, droppable }) {
    const item = el("div", `p2a-tree-item${lib.view === view ? " p2a-on" : ""}`);
    item.dataset.view = view;
    item.style.paddingLeft = `${4 + depth * 16}px`;
    if (children) {
      const folded = lib.collapsed.has(view);
      const twisty = el("button", `p2a-outline-twisty${folded ? "" : " p2a-open"}`, icon("chevron-right"));
      twisty.addEventListener("click", (e) => {
        e.stopPropagation();
        folded ? lib.collapsed.delete(view) : lib.collapsed.add(view);
        renderTree();
      });
      item.append(twisty);
    } else {
      item.append(el("span", "p2a-outline-spacer"));
    }
    item.insertAdjacentHTML("beforeend", icon(iconName));
    if (lib.editing && !lib.editing.isNew && collection && lib.editing.id === collection.id) {
      item.append(nameInput(collection.name));
    } else {
      const text = el("span", "p2a-grow");
      text.textContent = label;
      item.append(text);
      if (count) item.append(el("span", "p2a-tree-count", String(count)));
    }
    item.title = label;
    item.addEventListener("click", () => { if (lib.view !== view) { lib.view = view; lib.selected.clear(); renderLibrary(); } });
    if (collection) {
      item.draggable = true;
      item.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("application/x-p2a-collection", collection.id);
        e.dataTransfer.effectAllowed = "move";
      });
      item.addEventListener("dblclick", () => startRename(collection.id));
    }
    if (droppable) {
      item.addEventListener("dragover", (e) => {
        const types = [...e.dataTransfer.types];
        if (!types.includes("application/x-p2a-papers") && !types.includes("application/x-p2a-collection")) return;
        e.preventDefault();
        item.classList.add("p2a-drop");
      });
      item.addEventListener("dragleave", () => item.classList.remove("p2a-drop"));
      item.addEventListener("drop", (e) => {
        e.preventDefault();
        item.classList.remove("p2a-drop");
        const target = collection ? collection.id : null;
        const moved = e.dataTransfer.getData("application/x-p2a-collection");
        if (moved) {
          if (moved !== target) libEmit("move_collection", { collection: moved, parent: target });
          return;
        }
        const ids = JSON.parse(e.dataTransfer.getData("application/x-p2a-papers") || "[]");
        if (ids.length) libEmit("add", { ids, collection: target });
      });
    }
    item.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      treeMenu(e, view, collection);
    });
    return item;
  }

  function nameInput(value) {
    const input = el("input", "p2a-tree-input");
    input.type = "text";
    input.value = value;
    let done = false;
    const finish = (commit) => {
      if (done) return;
      done = true;
      const editing = lib.editing;
      lib.editing = null;
      const name = input.value.trim();
      if (commit && name && editing) {
        if (editing.isNew) {
          libEmit("new_collection", { id: editing.id, name, parent: editing.parent });
          if (editing.addIds?.length) libEmit("add", { ids: editing.addIds, collection: editing.id });
          lib.view = `c:${editing.id}`;
        } else if (name !== value) {
          libEmit("rename_collection", { collection: editing.id, name });
        }
      }
      renderTree();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
      e.stopPropagation();
    });
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("click", (e) => e.stopPropagation());
    setTimeout(() => { input.focus(); input.select(); }, 0);
    return input;
  }

  function startNewCollection(parent = null, addIds = []) {
    if (parent) lib.collapsed.delete(`c:${parent}`);
    lib.collapsed.delete("all");
    lib.editing = { id: newId(), parent, isNew: true, addIds };
    renderTree();
  }
  function startRename(id) {
    lib.editing = { id, isNew: false };
    renderTree();
  }

  function renderTree() {
    const tree = $("p2a-lib-tree");
    const scrollTop = tree.scrollTop;
    tree.innerHTML = "";
    const childrenOf = (parent) => lib.collections.filter((c) => c.parent === parent)
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }));
    const count = (view) => viewPapers(view).length;

    tree.append(el("div", "p2a-tree-heading", "paper2audio"));
    const top = childrenOf(null);
    tree.append(treeItem({ view: "all", label: "My Library", iconName: "library", depth: 0, count: count("all"),
      children: top.length > 0 || lib.editing?.isNew, droppable: true }));
    const addCollections = (parent, depth) => {
      if (parent !== null && lib.collapsed.has(`c:${parent}`)) return;
      for (const c of childrenOf(parent)) {
        const kids = childrenOf(c.id).length > 0 || (lib.editing?.isNew && lib.editing.parent === c.id);
        tree.append(treeItem({ view: `c:${c.id}`, label: c.name, iconName: "folder", depth, count: count(`c:${c.id}`),
          children: kids, collection: c, droppable: true }));
        addCollections(c.id, depth + 1);
      }
      if (lib.editing?.isNew && lib.editing.parent === parent) {
        const row = el("div", "p2a-tree-item");
        row.style.paddingLeft = `${4 + depth * 16}px`;
        row.append(el("span", "p2a-outline-spacer"));
        row.insertAdjacentHTML("beforeend", icon("folder"));
        row.append(nameInput(""));
        tree.append(row);
      }
    };
    if (!lib.collapsed.has("all")) addCollections(null, 1);
    tree.append(treeItem({ view: "unfiled", label: "Unfiled", iconName: "inbox", depth: 1, count: count("unfiled") }));
    tree.append(treeItem({ view: "recent", label: "Recently opened", iconName: "history", depth: 0, count: count("recent") }));

    if (lib.zotero) {
      tree.append(el("div", "p2a-tree-heading", "Zotero"));
      for (const library of lib.zotero.libraries) {
        const cols = lib.zotero.collections.filter((c) => c.library === library.id);
        const view = `zl:${library.id}`;
        tree.append(treeItem({ view, label: zoteroName(library),
          iconName: "library", depth: 0, count: count(view), children: cols.length > 0 }));
        const addZotero = (parent, depth) => {
          for (const c of cols.filter((c) => c.parent === parent)) {
            const kids = cols.some((k) => k.parent === c.id);
            tree.append(treeItem({ view: `zc:${c.id}`, label: c.name, iconName: "folder", depth,
              count: count(`zc:${c.id}`), children: kids }));
            if (!lib.collapsed.has(`zc:${c.id}`)) addZotero(c.id, depth + 1);
          }
        };
        if (!lib.collapsed.has(view)) addZotero(null, 1);
      }
      if (lib.zotero.status) {
        const note = el("div", "p2a-tree-note");
        note.textContent = lib.zotero.status;
        tree.append(note);
      }
    }
    tree.scrollTop = scrollTop;
  }

  function treeMenu(e, view, collection) {
    const items = [];
    if (view === "all") items.push({ label: "New collection", run: () => startNewCollection() });
    if (collection) {
      items.push(
        { label: "New subcollection", run: () => startNewCollection(collection.id) },
        { label: "Rename", run: () => startRename(collection.id) },
        { separator: true },
        { label: "Delete collection…", run: () => libEmit("delete_collection", { collection: collection.id }) },
      );
    }
    if (view.startsWith("zl:")) items.push({ label: "Reload the Zotero library", run: () => libEmit("sync") });
    if (items.length) showMenu(e.clientX, e.clientY, items);
  }

  // --- the list of papers

  const COLUMNS = [["title", "Title"], ["creator", "Creator"], ["year", "Year"], ["date", "Added"]];

  function renderItems() {
    const box = $("p2a-lib-items");
    const scrollTop = box.querySelector(".p2a-items-body")?.scrollTop || 0;
    box.innerHTML = "";
    const head = el("div", "p2a-items-head");
    for (const [key, label] of COLUMNS) {
      const cell = el("div", `p2a-col-${key}`);
      cell.textContent = key === "date" && lib.view === "recent" ? "Opened" : label;
      if (lib.sort.key === key) cell.append(el("span", "p2a-sort", lib.sort.dir > 0 ? "▲" : "▼"));
      cell.addEventListener("click", () => {
        lib.sort = { key, dir: lib.sort.key === key ? -lib.sort.dir : key === "date" ? -1 : 1 };
        renderItems();
      });
      head.append(cell);
    }
    const body = el("div", "p2a-items-body");
    const list = sortedItems();
    lib.visible = list.map((p) => p.id);
    const openIds = new Set(tabs.map((t) => t.id));
    for (const p of list) {
      const row = el("div", `p2a-item${lib.selected.has(p.id) ? " p2a-on" : ""}${p.missing ? " p2a-missing" : ""}`);
      row.dataset.id = p.id;
      row.draggable = true;
      const title = el("div", "p2a-col-title", icon(openIds.has(p.id) ? "book-open" : "file-text"));
      title.append(el("span", "p2a-grow", ""));
      title.lastChild.textContent = p.title;
      row.title = p.missing ? `File not found: ${tilde(p.path)}` : tilde(p.path);
      const creator = el("div", "p2a-col-creator"); creator.textContent = creators(p.authors);
      const year = el("div", "p2a-col-year"); year.textContent = p.year;
      const date = el("div", "p2a-col-date"); date.textContent = shortDate(lib.view === "recent" ? p.opened : p.added);
      row.append(title, creator, year, date);
      body.append(row);
    }
    if (!list.length) {
      body.append(el("div", "p2a-empty", lib.filter ? "No matching papers."
        : lib.view === "recent" ? "Papers you open are listed here."
        : lib.view.startsWith("z") ? "No papers with a PDF here."
        : "Drag papers here, or right-click them and choose a collection. Use the add button to add files."));
    }
    box.append(head, body);
    body.scrollTop = scrollTop;
    renderInfo();
  }

  function selectRow(id, e) {
    if (e && (e.metaKey || e.ctrlKey)) {
      lib.selected.has(id) ? lib.selected.delete(id) : lib.selected.add(id);
      lib.anchor = id;
    } else if (e && e.shiftKey && lib.anchor && lib.visible.includes(lib.anchor)) {
      const [a, b] = [lib.visible.indexOf(lib.anchor), lib.visible.indexOf(id)].sort((x, y) => x - y);
      lib.selected = new Set(lib.visible.slice(a, b + 1));
    } else {
      lib.selected = new Set([id]);
      lib.anchor = id;
    }
    refreshSelection();
  }
  function refreshSelection() {
    $("p2a-lib-items").querySelectorAll(".p2a-item").forEach((r) => r.classList.toggle("p2a-on", lib.selected.has(r.dataset.id)));
    renderInfo();
  }

  const selectedIds = () => lib.visible.filter((id) => lib.selected.has(id));
  const openPapers = (ids) => { if (ids.length) libEmit("open", { ids }); };

  function removeSelected() {
    const ids = selectedIds();
    if (!ids.length) return;
    if (lib.view.startsWith("c:")) libEmit("remove_from_collection", { ids, collection: lib.view.slice(2) });
    else if (lib.view === "all" || lib.view === "unfiled") libEmit("remove", { ids });
    else if (lib.view === "recent") libEmit("forget", { ids });
  }

  function itemMenu(e) {
    const ids = selectedIds();
    if (!ids.length) return;
    const papers = ids.map((id) => lib.byId.get(id));
    const items = [{ label: ids.length > 1 ? `Open ${ids.length} papers` : "Open", run: () => openPapers(ids) }];
    if (ids.length === 1 && !papers[0].missing) {
      items.push({ label: IS_MAC_UI ? "Show in Finder" : "Show in folder", run: () => libEmit("reveal", { ids }) });
    }
    items.push({ separator: true });
    if (papers.some((p) => !p.added)) items.push({ label: "Add to My Library", run: () => libEmit("add", { ids, collection: null }) });
    items.push({ header: "Add to collection" });
    const addTree = (parent, depth) => {
      lib.collections.filter((c) => c.parent === parent).sort((a, b) => a.name.localeCompare(b.name)).forEach((c) => {
        const inAll = papers.every((p) => p.collections.includes(c.id));
        items.push({ label: c.name, depth, checked: inAll, disabled: inAll, run: () => libEmit("add", { ids, collection: c.id }) });
        addTree(c.id, depth + 1);
      });
    };
    addTree(null, 1);
    items.push({ label: "New collection…", depth: 1, run: () => startNewCollection(null, ids) });
    const removal = lib.view.startsWith("c:") ? `Remove from “${lib.collections.find((c) => c.id === lib.view.slice(2))?.name}”`
      : lib.view === "all" || lib.view === "unfiled" ? "Remove from My Library"
      : lib.view === "recent" ? "Remove from Recently opened" : null;
    if (removal) items.push({ separator: true }, { label: removal, run: removeSelected });
    showMenu(e.clientX, e.clientY, items);
  }

  function renderInfo() {
    const box = $("p2a-lib-info");
    box.innerHTML = "";
    const ids = selectedIds();
    if (ids.length !== 1) {
      const n = ids.length || lib.visible.length;
      box.append(el("div", "p2a-empty", ids.length ? `${n} papers selected` : `${n} paper${n === 1 ? "" : "s"}`));
      if (ids.length > 1) {
        const open = el("button", "p2a-push p2a-primary", `${icon("book-open")} Open all`);
        open.addEventListener("click", () => openPapers(ids));
        const actions = el("div", "p2a-info-actions");
        actions.append(open);
        box.append(actions);
      }
      return;
    }
    const p = lib.byId.get(ids[0]);
    const title = el("div", "p2a-info-title");
    title.textContent = p.title;
    box.append(title);
    const rows = [
      ["Creators", p.authors.join(", ")],
      ["Year", p.year],
      ["File", baseName(p.path) + (p.missing ? " (not found)" : "")],
      ["Folder", folderOf(p.path)],
      ["Collections", p.collections.map((id) => lib.collections.find((c) => c.id === id)?.name).filter(Boolean).join(", ")],
      ["Zotero", p.zotero !== null && lib.zotero
        ? [zoteroName(lib.zotero.libraries.find((l) => l.id === p.zotero)), ...p.zcollections.map((id) => lib.zotero.collections.find((c) => c.id === id)?.name)].filter(Boolean).join(", ")
        : ""],
      ["Added", shortDate(p.added)],
      ["Last opened", shortDate(p.opened)],
    ];
    const grid = el("div", "p2a-info-grid");
    for (const [label, value] of rows) {
      if (!value) continue;
      grid.append(el("div", "p2a-label", label));
      const v = el("div", "p2a-value");
      v.textContent = value;
      grid.append(v);
    }
    box.append(grid);
    const actions = el("div", "p2a-info-actions");
    const open = el("button", "p2a-push p2a-primary", `${icon("book-open")} Open`);
    open.addEventListener("click", () => openPapers(ids));
    actions.append(open);
    if (!p.missing) {
      const reveal = el("button", "p2a-push", IS_MAC_UI ? "Show in Finder" : "Show in folder");
      reveal.addEventListener("click", () => libEmit("reveal", { ids }));
      actions.append(reveal);
    }
    if (!p.added) {
      const add = el("button", "p2a-push", `${icon("plus")} Add to library`);
      add.addEventListener("click", () => libEmit("add", { ids, collection: null }));
      actions.append(add);
    }
    box.append(actions);
  }

  function libraryKey(e) {
    if (e.key === "Escape") { hideMenu(); return; }
    const ids = lib.visible || [];
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!ids.length) return;
      const current = ids.indexOf(lib.anchor);
      const next = ids[Math.max(0, Math.min(ids.length - 1, current < 0 ? 0 : current + (e.key === "ArrowDown" ? 1 : -1)))];
      if (e.shiftKey && lib.selected.size) {  // extend the selection
        lib.selected.add(next);
        lib.anchor = next;
        refreshSelection();
      } else {
        selectRow(next);
      }
      $("p2a-lib-items").querySelector(`.p2a-item[data-id="${CSS.escape(next)}"]`)?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      openPapers(selectedIds());
    } else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      removeSelected();
    }
  }

  // Events on the list: delegated, as rows are rebuilt often.
  const itemsBox = () => $("p2a-lib-items");
  document.addEventListener("click", (e) => {
    const row = e.target.closest && e.target.closest("#p2a-lib-items .p2a-item");
    if (row) selectRow(row.dataset.id, e);
  });
  document.addEventListener("dblclick", (e) => {
    const row = e.target.closest && e.target.closest("#p2a-lib-items .p2a-item");
    if (row) openPapers([row.dataset.id]);
  });
  document.addEventListener("contextmenu", (e) => {
    if (!e.target.closest || !e.target.closest("#p2a-library")) return;
    e.preventDefault();
    const row = e.target.closest("#p2a-lib-items .p2a-item");
    if (!row) return;
    if (!lib.selected.has(row.dataset.id)) selectRow(row.dataset.id);
    itemMenu(e);
  });
  document.addEventListener("dragstart", (e) => {
    const row = e.target.closest && e.target.closest("#p2a-lib-items .p2a-item");
    if (!row || !itemsBox().contains(row)) return;
    if (!lib.selected.has(row.dataset.id)) selectRow(row.dataset.id);
    e.dataTransfer.setData("application/x-p2a-papers", JSON.stringify(selectedIds()));
    e.dataTransfer.effectAllowed = "copy";
  });

  // --- context menu

  function showMenu(x, y, items) {
    const menu = $("p2a-menu");
    menu.innerHTML = "";
    for (const item of items) {
      if (item.separator) { menu.append(el("div", "p2a-menu-sep")); continue; }
      if (item.header) {
        const header = el("div", "p2a-menu-header");
        header.textContent = item.header;
        menu.append(header);
        continue;
      }
      const row = el("div", `p2a-menu-item${item.disabled ? " p2a-disabled" : ""}`);
      if (item.depth) row.style.paddingLeft = `${8 + item.depth * 12}px`;
      if (item.checked !== undefined) row.append(el("span", "p2a-menu-check", item.checked ? "✓" : ""));
      row.append(el("span", "", ""));
      row.lastChild.textContent = item.label;
      if (!item.disabled) row.addEventListener("click", () => { hideMenu(); item.run(); });
      menu.append(row);
    }
    menu.classList.add("p2a-open");
    const r = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(4, Math.min(x, window.innerWidth - r.width - 4))}px`;
    menu.style.top = `${Math.max(4, Math.min(y, window.innerHeight - r.height - 4))}px`;
  }
  function hideMenu() { $("p2a-menu")?.classList.remove("p2a-open"); }

  // --- quick search (Cmd+K): find any paper and open it in a tab

  const picker = { results: [], index: 0 };
  const pickerOpen = () => $("p2a-picker").classList.contains("p2a-open");

  function openPicker() {
    hideMenu();
    hidePopup();
    const input = $("p2a-picker-input");
    input.value = "";
    $("p2a-picker").classList.add("p2a-open");
    renderPicker();
    setTimeout(() => input.focus(), 0);
  }
  function closePicker() {
    $("p2a-picker").classList.remove("p2a-open");
    $("p2a-picker-input").blur();
  }

  function renderPicker() {
    const words = searchWords($("p2a-picker-input").value);
    const openIds = new Set(tabs.map((t) => t.id));
    let results;
    if (!words.length) {  // recently opened first, then the library
      results = [...lib.papers].sort((a, b) => (b.opened || 0) - (a.opened || 0) || a.title.localeCompare(b.title));
    } else {
      results = lib.papers.map((p) => [matchScore(p, words), p])
        .filter(([score]) => score >= 0)
        .map(([score, p]) => [score + (p.opened ? 2 : 0) + (p.added ? 1 : 0), p])
        .sort((a, b) => b[0] - a[0] || (b[1].opened || 0) - (a[1].opened || 0))
        .map(([, p]) => p);
    }
    picker.results = results.slice(0, 60);
    picker.index = 0;
    const list = $("p2a-picker-list");
    list.innerHTML = "";
    picker.results.forEach((p, i) => {
      const row = el("div", `p2a-pick${i === 0 ? " p2a-on" : ""}${p.missing ? " p2a-missing" : ""}`, icon(openIds.has(p.id) ? "book-open" : "file-text"));
      const text = el("div", "p2a-grow");
      const title = el("div", "p2a-pick-title");
      title.textContent = p.title;
      const where = [creators(p.authors), p.year].filter(Boolean).join(", ");
      const place = p.zotero !== null && !p.added ? "Zotero" : p.added ? "My Library" : "Recently opened";
      const sub = el("div", "p2a-pick-sub");
      sub.textContent = [where, place, p.missing ? "file not found" : ""].filter(Boolean).join(" · ");
      text.append(title, sub);
      row.append(text);
      if (openIds.has(p.id)) row.append(el("span", "p2a-pick-badge", "open"));
      row.addEventListener("mousemove", () => { if (picker.index !== i) movePick(i - picker.index); });
      row.addEventListener("click", () => pick(i));
      list.append(row);
    });
    if (!picker.results.length) {
      list.append(el("div", "p2a-empty", lib.papers.length ? "No paper matches." : "Papers you open, add to the library or have in Zotero show up here."));
    }
  }

  function movePick(step) {
    const rows = $("p2a-picker-list").querySelectorAll(".p2a-pick");
    if (!rows.length) return;
    rows[picker.index]?.classList.remove("p2a-on");
    picker.index = Math.max(0, Math.min(rows.length - 1, picker.index + step));
    rows[picker.index].classList.add("p2a-on");
    rows[picker.index].scrollIntoView({ block: "nearest" });
  }
  function pick(i) {
    const p = picker.results[i];
    if (!p) return;
    closePicker();
    const tab = tabs.find((t) => t.id === p.id);
    if (tab) activate(tab.id);
    else libEmit("open", { ids: [p.id] });
  }

  document.addEventListener("keydown", (e) => {
    if (e.target.id !== "p2a-picker-input") return;
    const ctrl = e.ctrlKey && !e.metaKey;
    if (e.key === "ArrowDown" || (ctrl && e.key === "n")) { e.preventDefault(); movePick(1); }
    else if (e.key === "ArrowUp" || (ctrl && e.key === "p")) { e.preventDefault(); movePick(-1); }
    else if (e.key === "PageDown") { e.preventDefault(); movePick(8); }
    else if (e.key === "PageUp") { e.preventDefault(); movePick(-8); }
    else if (e.key === "Enter") { e.preventDefault(); pick(picker.index); }
    else if (e.key === "Escape") { e.preventDefault(); closePicker(); }
  });
  document.addEventListener("mousedown", (e) => {
    if (e.target.id === "p2a-picker") closePicker();  // a click next to the box
    if (!e.target.closest || !e.target.closest("#p2a-menu")) hideMenu();
  });
  window.addEventListener("blur", hideMenu);

  // --- events ------------------------------------------------------------------------------

  const inView = (target) => !!(target && target.closest && target.closest(".p2a-scroll"));
  const editing = () => {
    const a = document.activeElement;
    return a && (a.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName));
  };

  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-action]");
    if (!e.target.closest("#p2a-colors") && !e.target.closest("[data-action='colors']")) $("p2a-colors")?.classList.remove("p2a-open");
    if (!button || button.disabled) return;
    const action = button.dataset.action;
    if (action === "sidebar") toggleSidebar();
    else if (action === "pane") { $("p2a-pane").classList.toggle("p2a-hidden"); button.classList.toggle("p2a-on"); sharpenSoon(50); }
    else if (action === "zoom-in") zoomStep(1);
    else if (action === "zoom-out") zoomStep(-1);
    else if (action === "fit") setZoom(100);
    else if (action === "prev") goToPage(currentPage() - 1);
    else if (action === "next") goToPage(currentPage() + 1);
    else if (action.startsWith("tool-")) setTool(action.slice(5));
    else if (action === "colors") toggleColorMenu(button);
    else if (action.startsWith("tab-")) setSidebarTab(action.slice(4));
    else if (action === "quick-open") openPicker();
    else if (action.startsWith("audio-")) audioAction(action);
    else if (action === "lib-new-collection") startNewCollection(lib.view.startsWith("c:") ? lib.view.slice(2) : null);
    else if (action === "lib-add-files") libEmit("add_files", { collection: lib.view.startsWith("c:") ? lib.view.slice(2) : null });
    else if (action === "lib-sync") libEmit("sync");
    else if (action.startsWith("emit-")) window.emitEvent(`p2a_${action.slice(5)}`);
  });

  document.addEventListener("change", (e) => {
    if (e.target.id === "p2a-page-input") goToPage(parseInt(e.target.value, 10) - 1);
  });
  document.addEventListener("input", (e) => {
    if (e.target.id === "p2a-annotation-search") { ui.filter = e.target.value; renderSidebar(); }
    else if (e.target.id === "p2a-lib-search") { lib.filter = e.target.value; renderItems(); }
    else if (e.target.id === "p2a-picker-input") renderPicker();
  });

  // Selecting text: with the highlight or underline tool, mark it right away; otherwise
  // offer a popup. A plain click selects an annotation, or places a note with the note tool.
  // Dragging to select text is handled here rather than by the browser: with absolutely
  // positioned text, a drag that starts or ends in a gap (margins, between lines or
  // columns) would otherwise select up to the end of the page. Both ends snap to the
  // nearest text instead. Double and triple clicks (word, line) stay native.
  function caretAt(x, y) {
    let best = null, bestDistance = Infinity;
    for (const page of pages()) {
      const p = page.getBoundingClientRect();
      if (y < p.top - 40 || y > p.bottom + 40) continue;  // only the page(s) under the pointer
      for (const span of page.querySelectorAll(".p2a-text span")) {
        const r = span.getBoundingClientRect();
        const dy = y < r.top ? r.top - y : y > r.bottom ? y - r.bottom : 0;
        const dx = x < r.left ? r.left - x : x > r.right ? x - r.right : 0;
        const distance = dy * 4 + dx;  // prefer the same line over a nearby one
        if (distance < bestDistance) { bestDistance = distance; best = r; }
      }
    }
    if (!best) return null;
    const cx = Math.max(best.left + 0.5, Math.min(x, best.right - 0.5));
    const cy = best.top + best.height / 2;
    if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(cx, cy);
      return r && r.startContainer.nodeType === 3 ? [r.startContainer, r.startOffset] : null;
    }
    const pos = document.caretPositionFromPoint && document.caretPositionFromPoint(cx, cy);
    return pos && pos.offsetNode.nodeType === 3 ? [pos.offsetNode, pos.offset] : null;
  }

  let dragAnchor = null;
  document.addEventListener("mousedown", (e) => {
    if (!e.target.closest("#p2a-popup")) hidePopup();
    if (e.button !== 0 || e.detail > 1 || !e.target.closest(".p2a-text") || ui.tool === "note") return;
    const caret = caretAt(e.clientX, e.clientY);
    if (!caret) return;
    e.preventDefault();  // we place the selection ourselves
    document.activeElement?.blur?.();
    const selection = window.getSelection();
    if (e.shiftKey && selection.rangeCount) {
      dragAnchor = [selection.anchorNode, selection.anchorOffset];
    } else {
      dragAnchor = caret;
    }
    selection.setBaseAndExtent(dragAnchor[0], dragAnchor[1], caret[0], caret[1]);
  });
  document.addEventListener("mousemove", (e) => {
    if (!dragAnchor || !(e.buttons & 1)) return;
    const caret = caretAt(e.clientX, e.clientY);
    if (caret) window.getSelection().setBaseAndExtent(dragAnchor[0], dragAnchor[1], caret[0], caret[1]);
  });
  window.addEventListener("mouseup", () => { dragAnchor = null; }, true);
  window.addEventListener("blur", () => { dragAnchor = null; });
  document.addEventListener("mouseup", (e) => {
    if (!inView(e.target) || e.button !== 0) return;
    setTimeout(() => {
      const selection = window.getSelection();
      const page = e.target.closest(".p2a-page");
      if (selection.rangeCount && !selection.isCollapsed && selection.toString().trim()) {
        if (ui.tool === "highlight" || ui.tool === "underline") createFromSelection(ui.tool, ui.color);
        else showPopup(selection.getRangeAt(0));
        return;
      }
      if (!page || !state.doc) return;
      const r = page.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
      if (ui.tool === "note") {
        const note = add({ type: "note", color: ui.color, page: +page.dataset.index, position: [x, y] });
        select(note.id, { scrollCard: true });
        setTimeout(() => document.querySelector(`.p2a-card[data-id="${CSS.escape(note.id)}"] .p2a-card-comment`)?.focus(), 30);
        return;
      }
      const hit = annotationAt(+page.dataset.index, x, y);
      if (hit) select(hit.id, { scrollCard: true });
      else if (state.selected) select(null);
    }, 0);
  });

  // Keyboard shortcuts. "Mod" is Cmd on macOS and Ctrl elsewhere; zooming accepts both.
  const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform);
  const emit = (name) => window.emitEvent && window.emitEvent(name);
  function scrollBy(dy) { state.scroll.scrollBy({ top: dy }); }

  document.addEventListener("keydown", (e) => {
    const mod = IS_MAC ? e.metaKey : e.ctrlKey;
    // Zoom: Cmd or Ctrl with + - 0.
    if ((e.ctrlKey || e.metaKey) && !e.altKey) {
      let zoomAction = null;
      if (e.key === "+" || e.key === "=" || e.code === "NumpadAdd") zoomAction = 1;
      else if (e.key === "-" || e.key === "_" || e.code === "NumpadSubtract") zoomAction = -1;
      else if (e.key === "0" || e.code === "Numpad0") zoomAction = 0;
      if (zoomAction !== null) {
        e.preventDefault();  // never zoom the whole window instead
        if (zoomAction === 0) setZoom(100); else zoomStep(zoomAction);
        return;
      }
    }
    if (mod && e.shiftKey && !e.altKey && e.code === "KeyO") { e.preventDefault(); emit("p2a_open_remote"); return; }
    // Tabs: Ctrl+Tab and Cmd+Shift+[ ] switch, Cmd+1 is the Library, Cmd+2... the papers, Cmd+9 the last.
    if (e.ctrlKey && !e.metaKey && !e.altKey && e.key === "Tab") { e.preventDefault(); cycleTab(e.shiftKey ? -1 : 1); return; }
    if (mod && e.shiftKey && !e.altKey && (e.code === "BracketRight" || e.code === "BracketLeft")) {
      e.preventDefault();
      cycleTab(e.code === "BracketRight" ? 1 : -1);
      return;
    }
    if (mod && !e.shiftKey && !e.altKey && /^Digit[1-9]$/.test(e.code)) {
      e.preventDefault();
      const n = +e.code.slice(5);
      const tab = n === 9 ? tabs[tabs.length - 1] : tabs[n - 2];
      if (n === 1 || tab) activate(n === 1 ? "library" : tab.id);
      return;
    }
    if (mod && !e.shiftKey && !e.altKey && e.code === "KeyK") {
      e.preventDefault();
      pickerOpen() ? closePicker() : openPicker();
      return;
    }
    if (mod && !e.shiftKey) {
      const key = e.key.toLowerCase();
      if (!e.altKey && key === "o") { e.preventDefault(); emit("p2a_open"); return; }
      if (!e.altKey && key === "w") {
        e.preventDefault();
        if (pickerOpen()) closePicker();
        else if (state !== BLANK) closeTab(state.id);
        else emit("p2a_close");
        return;
      }
      if (!e.altKey && key === "a" && state === BLANK && !editing()) {
        e.preventDefault();
        lib.selected = new Set(lib.visible || []);
        refreshSelection();
        return;
      }
      if (!e.altKey && e.key === "Enter") { e.preventDefault(); emit("p2a_convert"); return; }
      if (!e.altKey && (e.key === "/" || e.code === "Slash")) { e.preventDefault(); emit("p2a_shortcuts"); return; }
      if (e.altKey && e.code === "KeyG" && state.doc) {
        e.preventDefault();
        const input = $("p2a-page-input");
        input.focus();
        input.select();
        return;
      }
      if (e.altKey && state.doc && ["Digit1", "Digit2", "Digit3"].includes(e.code)) {
        e.preventDefault();
        setTool({ Digit1: "highlight", Digit2: "underline", Digit3: "note" }[e.code]);
        return;
      }
    }
    if (e.target.id === "p2a-page-input" && e.key === "Enter") {
      goToPage(parseInt(e.target.value, 10) - 1);
      e.target.blur();
      return;
    }
    if (editing() || e.ctrlKey || e.altKey || (e.metaKey && !["ArrowUp", "ArrowDown"].includes(e.key))) return;
    if (e.key === "?") { e.preventDefault(); emit("p2a_shortcuts"); return; }
    if (state === BLANK) { libraryKey(e); return; }
    if (e.key === "Escape") {
      hidePopup();
      $("p2a-colors").classList.remove("p2a-open");
      if (ui.tool) setTool(ui.tool);  // toggles off
      if (state.selected) select(null);
      return;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && state.selected) {
      e.preventDefault();
      remove(state.selected);
      return;
    }
    if (!state.doc) return;
    const page = state.scroll.clientHeight * 0.9;
    const keys = {
      ArrowLeft: () => goToPage(currentPage() - 1),
      ArrowRight: () => goToPage(currentPage() + 1),
      ArrowUp: () => (e.metaKey ? goToPage(0) : scrollBy(-60)),
      ArrowDown: () => (e.metaKey ? goToPage(state.doc.pages.length - 1) : scrollBy(60)),
      PageUp: () => scrollBy(-page),
      PageDown: () => scrollBy(page),
      " ": () => scrollBy(e.shiftKey ? -page : page),
      Home: () => goToPage(0),
      End: () => goToPage(state.doc.pages.length - 1),
    };
    if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
  }, true);

  // Ctrl/Cmd + wheel, and pinch in Chromium-based views (wheel events with ctrlKey).
  document.addEventListener("wheel", (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    if (!inView(e.target)) return;
    const step = Math.max(-22, Math.min(22, e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY));
    setZoom(state.zoom * Math.exp(-step * 0.01), e.clientX, e.clientY);
  }, { passive: false });

  // Pinch in WebKit (macOS): gesture events with a running scale.
  let gestureStart = 100;
  document.addEventListener("gesturestart", (e) => { e.preventDefault(); gestureStart = state.zoom; }, { passive: false });
  document.addEventListener("gesturechange", (e) => {
    e.preventDefault();
    if (inView(e.target)) setZoom(gestureStart * e.scale, e.clientX, e.clientY);
  }, { passive: false });
  document.addEventListener("gestureend", (e) => { e.preventDefault(); sharpenSoon(30); }, { passive: false });

  document.addEventListener("scroll", (e) => {
    if (!e.target.classList || !e.target.classList.contains("p2a-scroll")) return;
    updatePage();
    hidePopup();
    sharpenSoon(150);
  }, true);
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(sharpen, 150); });

  document.documentElement.classList.add("p2a-mode-library");  // until Python shows a tab

  window.P2A = {
    openTab, activate, setLibrary, setAudio, setZoom, zoomStep, goToPage, setTool, setColor,
    get state() { return state; },
    get tabs() { return tabs; },
  };
})();
