// Command-center motion for every page (loaded only when Settings > Theme is "Command center").
// Panels slide in, numbers count up, bars fill, table rows cascade, light follows the mouse, buttons ripple.
// Nothing here changes data; with "reduce motion" turned on in the system it does nothing.
(function () {
  if (!document.body.classList.contains('cc')) return;
  if (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const pageOwnsMotion = !!window.CC_PAGE_MOTION;  // Business Overview runs its own

  // light that follows the mouse
  let glow = document.getElementById('ccglow');
  if (!glow) { glow = document.createElement('div'); glow.className = 'ccglow'; glow.id = 'ccglow'; document.body.appendChild(glow); }
  addEventListener('pointermove', e => { glow.style.transform = `translate(${e.clientX - 260}px, ${e.clientY - 260}px)`; }, { passive: true });

  // panels: HUD corners, spotlight under the mouse, sliding in one after another
  const panels = document.querySelectorAll('.page .card, .page .tile, .page .ybox, .page .filters, .page .pcard, .page .alert');
  panels.forEach((p, i) => {
    if (!p.querySelector(':scope > .hud') && !p.classList.contains('alert')) {
      const h = document.createElement('span'); h.className = 'hud'; h.setAttribute('aria-hidden', 'true'); p.appendChild(h);
    }
    p.addEventListener('pointermove', e => {
      const r = p.getBoundingClientRect(); p.style.setProperty('--mx', (e.clientX - r.left) + 'px'); p.style.setProperty('--my', (e.clientY - r.top) + 'px');
    }, { passive: true });
    if (!pageOwnsMotion && !p.closest('.att-list') && i < 40) { p.classList.add('anim'); p.style.setProperty('--d', Math.min(0.04 + i * 0.045, 1.1) + 's'); }
  });

  // Needs-attention cards and table rows cascade in
  document.querySelectorAll('.att-list .alert').forEach((a, i) => { if (i < 12) { a.classList.add('ccin'); a.style.setProperty('--d', (0.3 + i * 0.06) + 's'); } });
  document.querySelectorAll('.page table.tbl').forEach(t => {
    t.querySelectorAll('tr').forEach((r, i) => { if (i > 0 && i < 30) { r.classList.add('ccrow'); r.style.setProperty('--d', (0.25 + i * 0.03) + 's'); } });
  });

  // numbers count up: $1,911,292 · 6.35% · $83.7M · 1,260 (only the big figures, not the tables)
  if (!pageOwnsMotion) {
    const sel = '.kpi-val, .t-val, .yval, .ov .split b, .totals .kpi b, .board b, .big';
    document.querySelectorAll(sel).forEach(el => {
      const node = [...el.childNodes].find(n => n.nodeType === 3 && /\d/.test(n.textContent)); if (!node) return;
      const txt = node.textContent, m = txt.match(/-?[\d,]*\.?\d+/); if (!m) return;
      const raw = m[0], to = parseFloat(raw.replace(/,/g, '')); if (!isFinite(to) || to === 0) return;
      const dec = raw.includes('.') ? raw.split('.')[1].length : 0, comma = raw.includes(','), pre = txt.slice(0, m.index), post = txt.slice(m.index + raw.length);
      const fmt = v => { const s = v.toFixed(dec); return comma ? Number(s).toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec }) : s; };
      const t0 = performance.now() + 250, dur = 1100 + Math.min(900, Math.log10(Math.abs(to) + 1) * 150);
      const step = t => { const p = Math.max(0, Math.min((t - t0) / dur, 1)), e = 1 - Math.pow(1 - p, 3);
        node.textContent = pre + fmt(to * e) + post; if (p < 1) requestAnimationFrame(step); else node.textContent = txt; };
      node.textContent = pre + fmt(0) + post; requestAnimationFrame(step);
      setTimeout(() => { node.textContent = txt; }, dur + 600);  // the real value even if the tab was in the background
      el.classList.add('cccount');
    });
  }

  // bars fill from empty
  document.querySelectorAll('.hf-bar > div, .fbar > div, .ybar > div, .bar > div, .meter > div').forEach((b, i) => {
    const w = b.style.width; if (!w) return;
    b.style.width = '0'; b.style.transition = 'width 1.3s cubic-bezier(.2,.9,.2,1)';
    setTimeout(() => { b.style.width = w; }, 350 + i * 40);
  });

  // buttons and tabs ripple when clicked
  document.addEventListener('pointerdown', e => {
    const b = e.target.closest('.btn, .seg a, .presets a, .att-filter button, .side nav a'); if (!b) return;
    const r = b.getBoundingClientRect(), s = document.createElement('span');
    s.className = 'ccripple'; s.style.left = (e.clientX - r.left) + 'px'; s.style.top = (e.clientY - r.top) + 'px';
    b.classList.add('ccripple-host'); b.appendChild(s); setTimeout(() => s.remove(), 650);
  });
})();
