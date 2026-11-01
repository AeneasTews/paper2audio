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

  const state = {
    doc: null,              // {token, title, pages: [[w, h], ...]}
    zoom: 100,
    tool: null,             // null (select text), "highlight", "underline", "note"
    color: COLORS[0][1],
    annotations: [],
    selected: null,         // id of the selected annotation
    sidebarTab: "annotations",
    filter: "",
    outline: null,
    folded: new Set(),      // indexes of folded outline entries
  };

  const $ = (id) => document.getElementById(id);
  const icon = (name, cls = "") => `<span class="p2a-icon ${cls}">${ICONS[name] || ""}</span>`;
  const el = (tag, cls, html) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  };
  const pages = () => [...document.querySelectorAll("#p2a-pages .p2a-page")];
  const pageEl = (i) => document.querySelector(`#p2a-pages .p2a-page[data-index="${i}"]`);
  const notify = (message) => window.emitEvent && window.emitEvent("p2a_notify", message);

  // --- opening documents ------------------------------------------------------------------

  function open(doc) {
    state.doc = doc;
    state.annotations = [];
    state.selected = null;
    state.outline = null;
    state.folded = new Set();
    const container = $("p2a-pages");
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
    $("p2a-tab-title").textContent = doc.title;
    $("p2a-page-count").textContent = `of ${doc.pages.length}`;
    setControlsEnabled(true);
    resetView();
    fetch(`/annotations/${doc.token}`).then((r) => r.json()).then((list) => {
      if (state.doc !== doc) return;
      state.annotations = list;
      renderAllMarks();
      renderSidebar();
    });
    fetch(`/pdf/${doc.token}/outline`).then((r) => r.json()).then((items) => {
      if (state.doc !== doc) return;
      state.outline = items;
      if (state.sidebarTab === "outline") renderSidebar();
    });
    renderSidebar();
  }

  function showMessage(iconName, text) {
    state.doc = null;
    state.annotations = [];
    const container = $("p2a-pages");
    container.innerHTML = "";
    const box = el("div", "p2a-message", icon(iconName));
    box.append(el("div", "", ""));
    box.lastChild.textContent = text;
    container.append(box);
    $("p2a-tab-title").textContent = "No paper open";
    $("p2a-page-count").textContent = "";
    setControlsEnabled(false);
    resetView();
    renderSidebar();
  }

  function showText(title, url) {
    showMessage("file-text", "");
    $("p2a-tab-title").textContent = title;
    fetch(url).then((r) => r.text()).then((text) => {
      const container = $("p2a-pages");
      container.innerHTML = "";
      const doc = el("div", "p2a-textdoc");
      doc.textContent = text;
      container.append(doc);
    });
  }

  // Structured papers (HTML, JATS XML, EPUB, LaTeX): the labeled blocks they're read from,
  // shown as a formatted document. Text only: downloaded pages never run as web pages here.
  function showBlocks(title, url) {
    showMessage("file-text", "Loading…");
    $("p2a-tab-title").textContent = title;
    fetch(url).then((r) => {
      if (!r.ok) return r.json().then((e) => { throw new Error(e.detail || r.statusText); });
      return r.json();
    }).then((blocks) => {
      const container = $("p2a-pages");
      container.innerHTML = "";
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
        text.split(/(\u2063[^\u2064]*\u2064)/).forEach((part) => {
          if (part.startsWith("\u2063")) {
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
      container.append(doc);
    }).catch((e) => showMessage("file-text", `Can’t show this file: ${e.message}`));
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
    const scroller = $("p2a-scroll");
    if (!scroller || !state.doc) return;
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
    const doc = state.doc;
    fetch(`/pdf/${doc.token}/${page.dataset.index}/text`).then((r) => r.json()).then((items) => {
      if (state.doc !== doc) return;
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
    const scroller = $("p2a-scroll");
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
    const scroller = $("p2a-scroll");
    scroller.scrollTop = page.offsetTop + fraction * page.offsetHeight - (fraction ? scroller.clientHeight / 3 : 12);
    sharpenSoon(50);
  }

  // Zoom is a percentage of the fitted width. (x, y): point to keep in place, in window
  // coordinates; by default the centre of the view.
  function setZoom(value, x, y) {
    if (!state.doc) return;
    const scroller = $("p2a-scroll"), container = $("p2a-pages");
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
  function resetView() {
    state.zoom = 100;
    $("p2a-pages").style.width = "100%";
    $("p2a-zoom").textContent = state.doc ? "100%" : "";
    const scroller = $("p2a-scroll");
    scroller.scrollTop = 0;
    scroller.scrollLeft = 0;
    sharpenSoon(30);
  }

  // --- tools and colors -------------------------------------------------------------------

  function setTool(tool) {
    state.tool = state.tool === tool ? null : tool;
    document.querySelectorAll("[data-action^='tool-']").forEach((b) => {
      b.classList.toggle("p2a-on", b.dataset.action === `tool-${state.tool}`);
    });
    $("p2a-view").classList.toggle("p2a-tool-note", state.tool === "note");
    hidePopup();
  }

  function setColor(color) {
    state.color = color;
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
    document.querySelectorAll("#p2a-pages .p2a-text span").forEach((span) => {
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

  let saveTimer;
  function save() {
    const doc = state.doc;
    if (!doc) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      fetch(`/annotations/${doc.token}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state.annotations),
      }).then((r) => { if (!r.ok) throw new Error(r.statusText); })
        .catch((e) => notify(`Couldn't save annotations: ${e.message}`));
    }, 400);
  }

  // --- drawing annotations on the pages ---------------------------------------------------

  function renderAllMarks() {
    if (state.doc) state.doc.pages.forEach((_, i) => renderMarks(i));
  }

  function renderMarks(index) {
    const page = pageEl(index);
    if (!page) return;
    const marks = page.querySelector(".p2a-marks"), notes = page.querySelector(".p2a-notes");
    marks.innerHTML = "";
    notes.innerHTML = "";
    for (const a of state.annotations.filter((a) => a.page === index)) {
      const selected = a.id === state.selected ? " p2a-selected" : "";
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
    if (scrollCard && state.sidebarTab !== "annotations") state.sidebarTab = "annotations";
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
    underline.addEventListener("click", () => createFromSelection("underline", state.color));
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
    state.sidebarTab = tab;
    renderSidebar();
  }

  function renderSidebar() {
    document.querySelectorAll("[data-action^='tab-']").forEach((b) => {
      b.classList.toggle("p2a-on", b.dataset.action === `tab-${state.sidebarTab}`);
    });
    $("p2a-sidebar-search").style.display = state.sidebarTab === "annotations" ? "" : "none";
    const body = $("p2a-sidebar-body");
    const focused = document.activeElement;
    if (focused && body.contains(focused) && focused.isContentEditable) return;  // don't interrupt typing
    body.innerHTML = "";
    if (!state.doc) {
      body.append(el("div", "p2a-empty", "Open a PDF to see its annotations, pages and outline."));
      return;
    }
    if (state.sidebarTab === "thumbnails") renderThumbnails(body);
    else if (state.sidebarTab === "outline") renderOutline(body);
    else renderCards(body);
  }

  function sortKey(a) {
    const y = a.position ? a.position[1] : a.rects[0]?.[1] ?? 0;
    const x = a.position ? a.position[0] : a.rects[0]?.[0] ?? 0;
    return a.page * 10 + y * 2 + x * 0.01;
  }

  function renderCards(body) {
    const filter = state.filter.trim().toLowerCase();
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

  // --- events ------------------------------------------------------------------------------

  const inView = (target) => !!(target && target.closest && target.closest("#p2a-scroll"));
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
    else if (action.startsWith("emit-")) window.emitEvent(`p2a_${action.slice(5)}`);
  });

  document.addEventListener("change", (e) => {
    if (e.target.id === "p2a-page-input") goToPage(parseInt(e.target.value, 10) - 1);
  });
  document.addEventListener("input", (e) => {
    if (e.target.id === "p2a-annotation-search") { state.filter = e.target.value; renderSidebar(); }
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
    if (e.button !== 0 || e.detail > 1 || !e.target.closest(".p2a-text") || state.tool === "note") return;
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
        if (state.tool === "highlight" || state.tool === "underline") createFromSelection(state.tool, state.color);
        else showPopup(selection.getRangeAt(0));
        return;
      }
      if (!page || !state.doc) return;
      const r = page.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
      if (state.tool === "note") {
        const note = add({ type: "note", color: state.color, page: +page.dataset.index, position: [x, y] });
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
  function scrollBy(dy) { $("p2a-scroll").scrollBy({ top: dy }); }

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
    if (mod && !e.shiftKey) {
      const key = e.key.toLowerCase();
      if (!e.altKey && key === "o") { e.preventDefault(); emit("p2a_open"); return; }
      if (!e.altKey && key === "w") { e.preventDefault(); emit("p2a_close"); return; }
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
    if (e.key === "Escape") {
      hidePopup();
      $("p2a-colors").classList.remove("p2a-open");
      if (state.tool) setTool(state.tool);  // toggles off
      if (state.selected) select(null);
      return;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && state.selected) {
      e.preventDefault();
      remove(state.selected);
      return;
    }
    if (!state.doc) return;
    const page = $("p2a-scroll").clientHeight * 0.9;
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
    if (e.target.id !== "p2a-scroll") return;
    updatePage();
    hidePopup();
    sharpenSoon(150);
  }, true);
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(sharpen, 150); });

  window.P2A = {
    open, showMessage, showText, showBlocks, setZoom, zoomStep, goToPage, setTool, setColor,
    get state() { return state; },
  };
})();
