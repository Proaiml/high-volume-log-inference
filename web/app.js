"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, digits = 0) => (n == null ? "—" : Number(n).toLocaleString("tr-TR", {
  maximumFractionDigits: digits, minimumFractionDigits: digits }));
const pct = (v) => (v == null ? "—" : `%${Math.round(v * 100)}`);
const puan = (s) => Math.round(s * 100);

const LEVEL = {
  sakin: { label: "Sakin", dot: "dot-good" },
  dikkat: { label: "Dikkat", dot: "dot-warning" },
  alarm: { label: "Alarm", dot: "dot-critical" },
};
const STATUS = {
  "YENİ": { dot: "dot-violet", tip: "Bu log türü ilk kez görüldü." },
  "ARTIŞ": { dot: "dot-serious", tip: "Normalden belirgin fazla geldi." },
  "KAYIP": { dot: "dot-critical", tip: "Normalde gelmesi gerekirdi ama hiç gelmedi." },
  "DÜŞÜŞ": { dot: "dot-warning", tip: "Normalden belirgin az geldi." },
  "NORMAL": { dot: "", tip: "Normal aralıkta." },
};
const SEVERITY = {
  kritik: { label: "Kritik", dot: "dot-critical" },
  yuksek: { label: "Yüksek", dot: "dot-serious" },
  orta: { label: "Orta", dot: "dot-warning" },
  dusuk: { label: "Düşük", dot: "" },
};
const LEVEL_TEXT = {
  DEBUG: "geliştiriciler için ayrıntı mesajı, genelde önemsizdir",
  INFO: "olağan bilgi mesajı",
  WARN: "uyarı; bir şeyler ters gidiyor olabilir",
  ERROR: "bir işlem başarısız oldu",
  FATAL: "servis çalışamaz durumda",
};

let state = null;
const cache = {};          // bölüm → son çizilen içeriğin anahtarı (gereksiz yeniden çizimi önler)
let chartGeom = null;
let lastMouse = null;
let selectedWindow = null; // null = canlı (en son pencere)
let selectedData = null;
const openRows = new Set();

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.hata || res.statusText);
  return data;
}

async function fetchWindow(id) {
  const res = await fetch(`/api/window?id=${encodeURIComponent(id)}`, { cache: "no-store" });
  if (!res.ok) return null;
  return res.json();
}

async function poll() {
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    state = await res.json();
    state.__offline = false;
    if (selectedWindow != null) {
      selectedData = await fetchWindow(selectedWindow);
      if (!selectedData) selectedWindow = null;
    }
    render();
  } catch (e) {
    if (state) { state.__offline = true; renderBanners(); }
  } finally {
    setTimeout(poll, 1000);
  }
}

function changed(key, value) {
  const s = JSON.stringify(value);
  if (cache[key] === s) return false;
  cache[key] = s;
  return true;
}

function render() {
  renderTop();
  renderBanners();
  renderKpis();
  renderAttention();
  renderSim();
  renderIncidents();
  renderWeights();
  renderConnect();
  renderWindowList();
  renderWindowDetail();
}

// ------------------------------------------------------------------ üst bar ve uyarılar

function renderTop() {
  $("#clock").textContent = state.zaman;
  const llm = state.llm;
  const text = llm.mod === "claude" ? "Analist: Claude" : "Analist: Simülasyon";
  $("#mode-chip").innerHTML = `<span class="dot ${llm.mesgul ? "dot-series" : ""}"></span>${esc(text)}${llm.mesgul ? " · çalışıyor" : ""}`;
}

function renderBanners() {
  const out = [];
  if (state.__offline) {
    out.push(`<div class="banner error"><b>Sunucuya ulaşılamıyor.</b> Uygulama penceresi kapanmış olabilir; baslat.bat ile yeniden başlatın.</div>`);
  }
  const w = state.isinma;
  if (w.aktif) {
    out.push(`<div class="banner info"><span><b>Isınma:</b> sistem sıfırdan başladı ve "normal"in ne olduğunu öğreniyor
      (${w.toplam - w.kalan}/${w.toplam} pencere). Bu sürede LLM çağrılmaz; ısınma bitince bir senaryo tetikleyin.</span></div>`);
  }
  if (state.llm.hata) out.push(`<div class="banner error"><span><b>LLM hatası:</b> ${esc(state.llm.hata)}</span></div>`);
  if (state.llm.not) out.push(`<div class="banner warn"><span><b>LLM notu:</b> ${esc(state.llm.not)}</span></div>`);
  const sim = state.simulator;
  if (sim && !sim.calisiyor && state.istatistik.log_hizi < 1 && !w.aktif) {
    out.push(`<div class="banner warn"><span><b>Log gelmiyor.</b> Simülatörü başlatın ya da kendi loglarınızı
      gönderin ("Kendi loglarını bağla" kutusu).</span></div>`);
  }
  const html = out.join("");
  if (changed("banners", html)) $("#banners").innerHTML = html;
}

function renderKpis() {
  const s = state.istatistik;
  const budget = state.ayarlar.gunluk_token_butcesi;
  const used = Math.min(1, s.token_bugun / budget);
  const tiles = [
    ["Log hızı", `${fmt(s.log_hizi)} <small>log/sn</small>`, s.dusurulen ? `yetişemeyip atlanan: ${fmt(s.dusurulen)}` : "şu an gelen log sayısı"],
    ["Toplam log", fmt(s.toplam_log), "başlangıçtan beri"],
    ["Log türü (şablon)", fmt(s.sablon_sayisi), s.sablon_sayisi ? `her türde ortalama ${fmt(s.toplam_log / s.sablon_sayisi)} log` : "henüz yok"],
    ["LLM çağrısı", fmt(s.llm_cagrisi), state.llm.mesgul ? "çağrı sürüyor…" : "pencere başına en fazla 1"],
    ["Token (bugün)", fmt(s.token_bugun), `günlük bütçe ${fmt(budget)}`, used],
    ["Sıkıştırma", s.log_per_token ? `${fmt(s.log_per_token)} <small>log/token</small>` : "—", "LLM'e giden her token kaç logu temsil ediyor"],
    ["İsabet / yakalama", `${pct(s.kesinlik)} <small>/</small> ${pct(s.duyarlilik)}`,
      `isabet: alarmlardan gerçek çıkan · yakalama: gerçek olaylardan yakalanan (doğru ${s.dogru}, yanlış ${s.yanlis}, kaçan ${s.kacirilan})`],
  ];
  $("#kpis").innerHTML = tiles.map(([label, value, sub, meter]) => `
    <div class="kpi"><div class="label">${label}</div><div class="value">${value}</div>
      ${meter != null ? `<div class="meter"><i style="width:${(meter * 100).toFixed(1)}%"></i></div>` : ""}
      <div class="sub">${esc(sub)}</div></div>`).join("");
}

// ------------------------------------------------------------------ dikkat seviyesi

function levelSentence(name, params) {
  const p = params[name];
  if (name === "alarm") {
    return `Ciddi bir anormallik var. Pencereler ${p.pencere_sn} sn'ye kısaldı; LLM her pencerede, log türü başına ${p.ornek_sayisi} örnekle ve daha güçlü modelle çağrılıyor.`;
  }
  if (name === "dikkat") {
    return `Normalden farklı bir şeyler var. Pencereler ${p.pencere_sn} sn; LLM her pencerede çağrılıyor.`;
  }
  return `Her şey normal görünüyor. Pencereler ${p.pencere_sn} sn; LLM sadece dikkat çeken bir şey olursa çağrılıyor.`;
}

function renderAttention() {
  const a = state.dikkat;
  const lv = LEVEL[a.ad];
  const p = state.pencere_ilerleme;
  const last = state.son_pencere;
  const why = last && last.sinyal_nedeni && last.sinyal > 0.05
    ? `<div>Son pencerede dikkati yükselten: <b>${esc(last.sinyal_nedeni)}</b></div>` : "";
  const boundsTip = `<div class='t-title'>Sınırlar</div>Alt sınır: kaçırılan olay bildirilince yükselir, yanlış alarmlarla biraz düşer.<br>Üst sınır: günlük token bütçesi azalınca düşer.`;
  $("#attention-body").innerHTML = `
    <div class="att-top">
      <div class="att-level"><span class="dot ${lv.dot}"></span><span class="name">${lv.label}</span>
        <span class="val">${fmt(a.seviye, 2)}</span></div>
      <div class="progress small">
        <div>Şu anki pencere #${p.no}: ${fmt(p.gecen, 1)} / ${fmt(p.uzunluk)} sn · ${fmt(p.log)} log</div>
        <div class="bar"><i style="width:${Math.min(100, (p.gecen / p.uzunluk) * 100).toFixed(1)}%"></i></div>
      </div>
    </div>
    <div class="att-meta">
      <div>${esc(levelSentence(a.ad, a.parametreler))}</div>
      ${why}
      <div class="muted small" data-tip="${esc(boundsTip)}">alt sınır ${fmt(a.taban, 2)} · üst sınır ${fmt(a.tavan, 2)} ⓘ</div>
    </div>`;

  if (changed("chart", a.gecmis)) drawAttentionChart(a.gecmis);

  const params = a.parametreler;
  if (changed("levels", [params, a.ad])) {
    const cols = ["sakin", "dikkat", "alarm"];
    const cell = (k, v) => `<td class="${k === a.ad ? "current" : ""}">${v}</td>`;
    const row = (name, fn) => `<tr><th>${name}</th>${cols.map((k) => cell(k, fn(params[k], k))).join("")}</tr>`;
    $("#level-table").innerHTML = `
      <table class="levels">
        <thead><tr><th></th>${cols.map((k) => `<th class="${k === a.ad ? "current" : ""}">${LEVEL[k].label}</th>`).join("")}</tr></thead>
        <tbody>
          ${row("LLM ne zaman çağrılır", (_, k) => (k === "sakin" ? "dikkat çeken bir şey varsa" : "her pencerede"))}
          ${row("Pencere süresi", (p) => `${p.pencere_sn} sn`)}
          ${row("Pencere başına token", (p) => fmt(p.token_butcesi))}
          ${row("Log türü başına örnek satır", (p) => p.ornek_sayisi)}
          ${row("Model (Claude modunda)", (p) => `<span class="mono">${esc(p.model)}</span>`)}
        </tbody>
      </table>`;
  }
}

function drawAttentionChart(hist) {
  const el = $("#attention-chart");
  const W = el.clientWidth || 640;
  const H = 150;
  const m = { l: 34, r: 52, t: 8, b: 22 };
  const iw = Math.max(10, W - m.l - m.r);
  const ih = H - m.t - m.b;
  const n = hist.length;
  if (n < 2) {
    chartGeom = null;
    el.innerHTML = `<p class="empty">Grafik, pencereler kapandıkça dolacak.</p>`;
    return;
  }
  const x = (i) => m.l + (i * iw) / (n - 1);
  const y = (v) => m.t + (1 - v) * ih;
  const bands = [
    [0.67, 1, "var(--critical)", "alarm"],
    [0.34, 0.67, "var(--warning)", "dikkat"],
    [0, 0.34, "var(--good)", "sakin"],
  ];
  let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Dikkat seviyesi geçmişi">`;
  for (const [lo, hi, color, label] of bands) {
    svg += `<rect x="${m.l}" y="${y(hi)}" width="${iw}" height="${y(lo) - y(hi)}" fill="${color}" opacity="0.08"/>`;
    svg += `<text class="band-label" x="${m.l + iw + 8}" y="${(y(lo) + y(hi)) / 2 + 4}">${label}</text>`;
  }
  for (const v of [0.34, 0.67]) {
    svg += `<line x1="${m.l}" x2="${m.l + iw}" y1="${y(v)}" y2="${y(v)}" stroke="var(--border)" stroke-dasharray="3 3"/>`;
  }
  for (const v of [0, 0.5, 1]) {
    svg += `<text class="axis-text" x="${m.l - 6}" y="${y(v) + 3.5}" text-anchor="end">${fmt(v, v % 1 ? 1 : 0)}</text>`;
  }
  svg += `<line x1="${m.l}" x2="${m.l + iw}" y1="${y(0)}" y2="${y(0)}" stroke="var(--border)"/>`;
  const d = hist.map((h, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(h.dikkat).toFixed(1)}`).join("");
  svg += `<path d="${d}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  hist.forEach((h, i) => {
    if (h.cagri) svg += `<circle cx="${x(i)}" cy="${y(h.dikkat)}" r="4" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2"/>`;
  });
  svg += `<text class="axis-text" x="${m.l}" y="${H - 6}">${esc(hist[0].t)}</text>`;
  svg += `<text class="axis-text" x="${m.l + iw}" y="${H - 6}" text-anchor="end">${esc(hist[n - 1].t)}</text>`;
  svg += `<line id="xhair" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" stroke="var(--text-2)" stroke-width="1" visibility="hidden"/>`;
  svg += `<circle id="xdot" r="5" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>`;
  svg += `</svg>`;
  el.innerHTML = svg;
  chartGeom = { m, iw, n, hist, x, y, W };
  if (lastMouse) chartHover(lastMouse);
}

function chartIndex(ev) {
  const el = $("#attention-chart");
  const rect = el.getBoundingClientRect();
  const mx = (ev.clientX - rect.left) * (chartGeom.W / rect.width);
  const { m, iw, n } = chartGeom;
  return Math.max(0, Math.min(n - 1, Math.round(((mx - m.l) / iw) * (n - 1))));
}

function chartHover(ev) {
  if (!chartGeom) return;
  const el = $("#attention-chart");
  const i = chartIndex(ev);
  const { hist, x, y } = chartGeom;
  const h = hist[i];
  const xh = el.querySelector("#xhair");
  const xd = el.querySelector("#xdot");
  if (!xh) return;
  xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
  xd.setAttribute("cx", x(i)); xd.setAttribute("cy", y(h.dikkat)); xd.setAttribute("visibility", "visible");
  showTip(ev, `<div class="t-title">Pencere #${h.id} · ${esc(h.t)}</div>
    <div class="t-row"><span>Dikkat seviyesi</span><span>${fmt(h.dikkat, 2)}</span></div>
    <div class="t-row"><span>Anlık anormallik sinyali</span><span>${fmt(h.sinyal, 2)}</span></div>
    <div class="t-row"><span>Log hızı</span><span>${fmt(h.hiz)} log/sn</span></div>
    <div class="t-row"><span>LLM çağrıldı mı</span><span>${h.cagri ? "evet" : "hayır"}</span></div>
    <div class="muted small" style="margin-top:4px">Tıklayınca bu pencerede gidenler açılır</div>`);
}

function chartLeave() {
  lastMouse = null;
  hideTip();
  const el = $("#attention-chart");
  el.querySelector("#xhair")?.setAttribute("visibility", "hidden");
  el.querySelector("#xdot")?.setAttribute("visibility", "hidden");
}

// ------------------------------------------------------------------ pencere geçmişi

function selectWindow(id, scroll = true) {
  selectedWindow = id;
  selectedData = null;
  openRows.clear();
  cache.winlist = null;
  cache.windetail = null;
  if (id == null) {
    renderWindowList();
    renderWindowDetail();
  } else {
    fetchWindow(id).then((data) => {
      selectedData = data;
      if (!data) selectedWindow = null;
      renderWindowList();
      renderWindowDetail();
    });
  }
  if (scroll) $("#windows-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderWindowList() {
  if (!state) return;
  const onlyCalled = $("#only-called").checked;
  const list = state.pencereler.filter((w) => !onlyCalled || w.cagrildi);
  if (!changed("winlist", [list, selectedWindow, onlyCalled])) return;
  const item = (w) => {
    const lv = LEVEL[w.seviye];
    const sent = w.cagrildi ? `LLM'e gitti · ${w.gonderilen} tür` : "LLM çağrılmadı";
    return `<button type="button" class="win-item ${selectedWindow === w.id ? "active" : ""}" data-win="${w.id}">
      <span class="l1"><span>#${w.id} · ${esc(w.bas)}</span>${w.olaylar.length ? `<span class="inc">olay: ${w.olaylar.map(esc).join(", ")}</span>` : ""}</span>
      <span class="l2"><span class="dot ${lv.dot}"></span>${lv.label} · ${esc(sent)}</span>
    </button>`;
  };
  $("#win-list").innerHTML = `
    <button type="button" class="win-item live ${selectedWindow == null ? "active" : ""}" data-win="live">
      <span class="l1"><span>● Canlı: hep en son pencere</span></span>
      <span class="l2">yeni pencere kapandıkça güncellenir</span>
    </button>` + (list.length ? list.map(item).join("") : `<p class="empty small" style="padding:8px 10px">Henüz pencere yok.</p>`);
}

function scoreWord(s) {
  if (s >= 0.7) return "yüksek";
  if (s >= 0.4) return "orta";
  return "düşük";
}

function explainRow(r, w) {
  const names = (state.oncelik_modeli.olcutler || []).map((o) => o.ad);
  const weights = w.agirliklar || [];
  const parts = [];
  parts.push(`<p><b>Bu satır ne?</b> <i>${esc(r.servis)}</i> servisinden gelen ve aynı kalıba uyan log satırlarının ortak hali
    (log türü). Değişen kısımlar <code>&lt;*&gt;</code> ile gösterilir. Bu pencerede <b>${fmt(r.adet)}</b> tane geldi.</p>`);

  const exp = fmt(r.beklenen);
  const statusText = {
    "YENİ": "Bu log türü <b>ilk kez görüldü</b>. Yeni bir hatanın ya da yeni bir sürümün (deploy) işareti olabilir.",
    "ARTIŞ": r.gorulen < 10
      ? `Bu log türü ilk kez çok yakın zamanda (${r.gorulen} pencere önce) görüldü ve o zamandan beri sistemin beklediğinden çok daha fazla geliyor: beklenen yaklaşık ${exp}, gelen <b>${fmt(r.adet)}</b>.`
      : `Normalde bu sürede yaklaşık <b>${exp}</b> tane gelirken <b>${fmt(r.adet)}</b> tane geldi (yaklaşık <b>${fmt(r.oran)} kat</b>).`,
    "KAYIP": `Normalde bu sürede yaklaşık <b>${exp}</b> tane gelmesi gerekirdi ama <b>hiç gelmedi</b>. Servis durmuş ya da log gönderimi kesilmiş olabilir.`,
    "DÜŞÜŞ": `Normalden belirgin <b>az</b> geldi (beklenen yaklaşık ${exp}, gelen ${fmt(r.adet)}).`,
    "NORMAL": r.beklenen ? `<b>Normal aralıkta</b> (beklenen yaklaşık ${exp}, gelen ${fmt(r.adet)}).` : "Sistem bu log türünün normalini henüz öğreniyor.",
  }[r.durum];
  parts.push(`<p><b>Ne oldu?</b> ${statusText}</p>`);
  parts.push(`<p><b>Seviye:</b> ${esc(r.seviye)}, yani ${LEVEL_TEXT[r.seviye] || "bilgi mesajı"}.</p>`);
  parts.push(`<p><b>Geçmişi:</b> ${r.dogru || r.yanlis
    ? `${r.dogru} doğrulanmış olayda kanıt oldu, ${r.yanlis} kez yanlış alarma karıştı.`
    : "henüz hiçbir olayda yer almadı."}</p>`);

  const contrib = r.x.map((v, i) => ({ name: names[i], c: (weights[i] || 0) * v }))
    .filter((o) => o.c > 0.15).sort((a, b) => b.c - a.c).slice(0, 2);
  const why = contrib.length ? ` Puanı en çok yükselten ölçütler: <b>${contrib.map((o) => esc(o.name).toLowerCase()).join("</b> ve <b>")}</b>.` : "";
  parts.push(`<p><b>Öncelik puanı: ${puan(r.skor)}/100 (${scoreWord(r.skor)}).</b>${why}</p>`);

  let decision;
  if (!w.cagrildi) {
    decision = `Bu pencerede LLM hiç çağrılmadı (${esc(w.neden)}).` + (r.secim
      ? ` Çağrılsaydı bu log türü ${r.secim === "oncelik" ? "öncelikli olarak" : "keşif için"} gönderilecekti.` : "");
  } else if (r.secim === "oncelik") {
    decision = "<b>LLM'e gönderildi:</b> öncelik puanı yeterince yüksek ve token bütçesine sığdı.";
  } else if (r.secim === "kesif") {
    decision = "<b>LLM'e keşif amacıyla gönderildi:</b> puanı düşük ama sistem gözden bir şey kaçmasın diye bütçenin küçük bir kısmıyla rastgele log türlerine de bakıyor.";
  } else {
    decision = "<b>LLM'e gönderilmedi:</b> puanı düşük; özette sadece \"geri kalan log türleri normal aralıkta\" diye sayıldı.";
  }
  parts.push(`<p><b>Karar:</b> ${decision}</p>`);
  if (r.ornek && r.ornek.length) {
    parts.push(`<p><b>Gerçek örnek satır${r.ornek.length > 1 ? "lar" : ""}:</b></p><pre>${r.ornek.map(esc).join("\n")}</pre>`);
  }
  return `<div class="explain">${parts.join("")}</div>`;
}

function renderWindowDetail() {
  if (!state) return;
  const w = selectedWindow != null ? selectedData : state.son_pencere;
  if (!w) {
    if (changed("windetail", "empty")) $("#win-detail").innerHTML = `<p class="empty">İlk pencere kapanınca burada görünecek.</p>`;
    return;
  }
  if (!changed("windetail", [w.id, w.token, w.neden, w.olaylar, [...openRows], selectedWindow])) return;
  const openSummary = $("#win-detail details.summary")?.open || false;
  const lv = LEVEL[w.seviye];
  const sentCount = w.satirlar.filter((r) => r.secim).length;
  const rows = w.satirlar.map((r) => {
    const st = STATUS[r.durum] || STATUS.NORMAL;
    const open = openRows.has(r.id);
    const sel = !r.secim ? `<span class="muted">—</span>`
      : !w.cagrildi ? `<span class="muted small">${r.secim === "oncelik" ? "öncelik" : "keşif"} (gitmedi)</span>`
      : r.secim === "oncelik" ? `<span class="sel oncelik">Öncelikli</span>` : `<span class="sel kesif">Keşif</span>`;
    return `<tr class="row-main ${r.secim ? "" : "unsent"} ${open ? "open" : ""}" data-row="${r.id}">
      <td><span class="status" data-tip="${esc(st.tip)}">${st.dot ? `<span class="dot ${st.dot}"></span>` : ""}${esc(r.durum)}</span></td>
      <td class="mono">T${r.id}</td>
      <td class="mono">${esc(r.seviye)}</td>
      <td class="tpl"><span class="svc">${esc(r.servis)}:</span> ${esc(r.sablon)}</td>
      <td class="n">${fmt(r.adet)}</td>
      <td class="n">${fmt(r.beklenen)}</td>
      <td class="n"><div class="scorecell"><span class="scorebar"><i style="width:${(r.skor * 100).toFixed(1)}%"></i></span>${puan(r.skor)}</div></td>
      <td>${sel}</td>
    </tr>${open ? `<tr class="row-explain"><td colspan="8">${explainRow(r, w)}</td></tr>` : ""}`;
  }).join("");
  const pinned = selectedWindow != null;
  $("#win-detail").innerHTML = `
    <div class="pin-bar">
      <div class="win-meta">
        <span>Pencere <b>#${w.id}</b> · ${esc(w.bas)}–${esc(w.bit)} (${fmt(w.sure, 1)} sn)</span>
        <span><b>${fmt(w.toplam)}</b> log</span>
        <span><b>${fmt(w.aktif)}</b> log türü</span>
        <span><b>${sentCount}</b> tür ${w.cagrildi ? "LLM'e gönderildi" : "seçildi ama LLM çağrılmadı"}</span>
        <span>Seviye: <span class="dot ${lv.dot}"></span> <b>${lv.label}</b></span>
        ${w.cagrildi ? `<span>Token: <b>${fmt(w.token)}</b></span>` : ""}
        ${w.olaylar.length ? `<span>Olay: <b>${w.olaylar.map(esc).join(", ")}</b></span>` : ""}
      </div>
      ${pinned ? `<button class="btn small" type="button" data-win="live">Canlıya dön</button>` : `<span class="chip"><span class="dot dot-good"></span>canlı</span>`}
    </div>
    <p class="decision ${w.cagrildi ? "called" : ""}">${esc(w.neden)}</p>
    <p class="how">Her satır bir log türüdür. Sistem her türe 0-100 arası bir öncelik puanı verir; puanı yüksek olanlar
      token bütçesine sığdığı kadar LLM'e gider. <b>Ayrıntı için bir satıra tıklayın.</b></p>
    <div class="table-wrap"><table>
      <thead><tr><th>Durum</th><th>ID</th><th>Seviye</th><th>Servis: log türü</th><th class="n">Adet</th>
        <th class="n">Beklenen</th><th class="n">Öncelik (0-100)</th><th>LLM'e</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${w.ozet ? `<details class="summary" ${openSummary ? "open" : ""}><summary>LLM'e giden özet metninin tamamını göster</summary>
      <pre class="prompt">${esc(w.ozet)}</pre></details>` : ""}`;
}

// ------------------------------------------------------------------ simülatör

function renderSim() {
  const sim = state.simulator;
  if (!sim) return;
  $("#sim-toggle").textContent = sim.calisiyor ? "Duraklat" : "Başlat";
  const slider = $("#sim-rate");
  if (document.activeElement !== slider) {
    slider.value = sim.hiz;
    $("#sim-rate-val").textContent = fmt(sim.hiz);
  }
  if (changed("scenarios", sim.senaryolar)) {
    $("#scenarios").innerHTML = sim.senaryolar.map((s) => `
      <button class="scenario" type="button" data-scenario="${esc(s.anahtar)}">
        <b>${esc(s.ad)}</b>
        <span>${esc(s.aciklama)}</span>
        <span class="tag"><span class="dot ${s.gercek ? "dot-critical" : "dot-good"}"></span>${s.gercek ? "gerçek olay" : "zararsız"}</span>
      </button>`).join("");
  }
  if (changed("results", sim.sonuclar)) {
    const icon = { iyi: "dot-good", kotu: "dot-critical", aktif: "dot-series" };
    $("#sim-results").innerHTML = sim.sonuclar.length ? `
      <table class="results"><thead><tr><th>Başladı</th><th>Senaryo</th><th>Sonuç</th></tr></thead><tbody>
      ${sim.sonuclar.map((r) => `<tr><td class="mono">${esc(r.baslangic)}</td><td>${esc(r.ad)}</td>
        <td><span class="res"><span class="dot ${icon[r.tur]}"></span>${esc(r.durum)}${r.kalan ? ` · ${r.kalan} sn` : ""}</span></td></tr>`).join("")}
      </tbody></table>` : `<p class="empty small">Henüz senaryo yok. Yukarıdan birini tetikleyin.</p>`;
  }
}

// ------------------------------------------------------------------ olaylar

function renderIncidents() {
  const list = state.olaylar;
  if (!changed("incidents", list)) return;
  const open = new Set([...document.querySelectorAll("#incidents details[open]")].map((d) => d.dataset.id));
  if (!list.length) {
    $("#incidents").innerHTML = `<p class="empty">Henüz olay yok. Isınma bittikten sonra bir senaryo tetikleyin.</p>`;
    return;
  }
  $("#incidents").innerHTML = list.map((o) => {
    const sev = SEVERITY[o.ciddiyet] || SEVERITY.orta;
    let verdict;
    if (o.tur === "kacirildi") {
      verdict = `<span class="verdict"><span class="dot dot-critical"></span>Kaçırıldı${o.oto ? " (otomatik)" : ""}</span>`;
    } else if (o.karar === "dogru") {
      verdict = `<span class="verdict"><span class="dot dot-good"></span>Doğru olay${o.oto ? " (otomatik)" : ""}</span>`;
    } else if (o.karar === "yanlis") {
      verdict = `<span class="verdict"><span class="dot dot-critical"></span>Yanlış alarm${o.oto ? " (otomatik)" : ""}</span>`;
    } else {
      verdict = `<button class="btn small" type="button" data-fb="dogru" data-id="${esc(o.id)}">Doğru olay</button>
                 <button class="btn small" type="button" data-fb="yanlis" data-id="${esc(o.id)}">Yanlış alarm</button>`;
    }
    const repeat = o.tekrar > 1 ? ` · ${o.tekrar} pencerede sürdü (son ${esc(o.son)})` : "";
    const winLink = o.pencere != null ? `<button class="link" type="button" data-win="${o.pencere}">Pencere #${o.pencere}'de LLM'e ne gitti?</button>` : "";
    return `<article class="incident ${o.tur === "kacirildi" ? "missed" : ""}">
      <div class="inc-head">
        <span class="sev"><span class="dot ${sev.dot}"></span>${sev.label}</span>
        <span>${esc(o.id)} · ${esc(o.zaman)}${repeat}</span>
      </div>
      <h3>${esc(o.baslik)}</h3>
      ${o.ozet ? `<p>${esc(o.ozet)}</p>` : ""}
      ${o.oneri ? `<p class="action">Öneri: ${esc(o.oneri)}</p>` : ""}
      ${o.kanit_metin.length ? `<details data-id="${esc(o.id)}" ${open.has(o.id) ? "open" : ""}>
        <summary>LLM'in dayandığı log türleri (${o.kanit_metin.length})</summary>
        <ul>${o.kanit_metin.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></details>` : ""}
      <div class="inc-actions">${verdict}${winLink}${o.model ? `<span class="inc-foot">${esc(o.model)}</span>` : ""}</div>
    </article>`;
  }).join("");
}

// ------------------------------------------------------------------ ağırlıklar

function renderWeights() {
  const pm = state.oncelik_modeli;
  if (!changed("weights", pm)) return;
  const max = Math.max(3, ...pm.olcutler.map((f) => Math.abs(f.agirlik)), ...pm.olcutler.map((f) => f.baslangic));
  $("#weights").innerHTML = pm.olcutler.map((f) => {
    const w = (Math.abs(f.agirlik) / max) * 100;
    const g = (f.baslangic / max) * 100;
    const diff = f.agirlik - f.baslangic;
    const trend = Math.abs(diff) < 0.05 ? "değişmedi" : diff > 0 ? "güçlendi" : "zayıfladı";
    const tip = `<div class='t-title'>${esc(f.ad)}</div>${esc(f.aciklama)}
      <div class='t-row' style='margin-top:6px'><span>Şu anki ağırlık</span><span>${fmt(f.agirlik, 2)}</span></div>
      <div class='t-row'><span>Başlangıçta</span><span>${fmt(f.baslangic, 2)}</span></div>
      <div class='t-row'><span>Geri bildirimle</span><span>${trend}</span></div>`;
    return `<div class="wrow" data-tip="${esc(tip)}">
      <span class="wlabel">${esc(f.ad)}<small>${esc(f.aciklama)}</small></span>
      <span class="wtrack"><span class="wbar ${f.agirlik < 0 ? "neg" : ""}" style="width:${w.toFixed(1)}%"></span>
        <span class="wghost" style="left:calc(${g.toFixed(1)}% - 1px)"></span></span>
      <span class="wval">${fmt(f.agirlik, 1)}</span>
    </div>`;
  }).join("") + `<p class="weights-foot">Mavi çubuk: şu anki önem · ince çizgi: başlangıçtaki önem ·
    sistem ${fmt(pm.guncelleme)} kez geri bildirimden öğrendi</p>`;
}

// ------------------------------------------------------------------ bağlantı

function renderConnect() {
  if (cache.connect) return;
  cache.connect = "1";
  const origin = location.origin;
  $("#connect").innerHTML = `
    <p>Simülatörü duraklatıp kendi loglarınızı gönderebilirsiniz. Kabul edilen biçimler:
      mlog tarzı satır, JSON satırı ya da düz metin.</p>
    <pre>2026-11-26T12:04:01.123 ERROR [payment-svc@host-2] mesaj
{"ts":"...","level":"error","service":"billing","msg":"..."}</pre>
    <p><b>1. HTTP ile gönder</b> (PowerShell):</p>
    <pre>Invoke-RestMethod -Method Post -Uri ${esc(origin)}/api/ingest -ContentType 'text/plain; charset=utf-8' -Body (Get-Content C:\\loglar\\app.log -Raw)</pre>
    <p><b>2. Bir log dosyasını canlı takip et:</b></p>
    <pre>.\\baslat.bat --tail C:\\loglar\\app.log</pre>
    <p class="muted small">Başka bir makineden log almak için: <code>.\\baslat.bat --host 0.0.0.0</code> ·
      sadece gerçek loglar için: <code>.\\baslat.bat --no-sim</code></p>`;
}

// ------------------------------------------------------------------ ipucu

const tipEl = $("#tooltip");
function showTip(ev, html) {
  tipEl.innerHTML = html;
  tipEl.hidden = false;
  const pad = 14;
  const r = tipEl.getBoundingClientRect();
  let left = ev.clientX + pad;
  let top = ev.clientY + pad;
  if (left + r.width > window.innerWidth - 8) left = ev.clientX - r.width - pad;
  if (top + r.height > window.innerHeight - 8) top = ev.clientY - r.height - pad;
  tipEl.style.left = `${left}px`;
  tipEl.style.top = `${top}px`;
}
function hideTip() { tipEl.hidden = true; }

document.addEventListener("mousemove", (ev) => {
  if (ev.target.closest("#attention-chart")) return;
  const t = ev.target.closest("[data-tip]");
  if (t) showTip(ev, t.dataset.tip); else hideTip();
});
$("#attention-chart").addEventListener("mousemove", (ev) => { lastMouse = ev; chartHover(ev); });
$("#attention-chart").addEventListener("mouseleave", chartLeave);
$("#attention-chart").addEventListener("click", (ev) => {
  if (!chartGeom) return;
  const h = chartGeom.hist[chartIndex(ev)];
  if (h && h.id != null) selectWindow(h.id);
});
window.addEventListener("resize", () => { cache.chart = null; });

// ------------------------------------------------------------------ olay dinleyicileri

document.addEventListener("click", async (ev) => {
  const sc = ev.target.closest("[data-scenario]");
  if (sc) {
    sc.disabled = true;
    try { await api("/api/scenario", { anahtar: sc.dataset.scenario }); } catch (e) { alert(e.message); }
    setTimeout(() => { sc.disabled = false; }, 800);
    return;
  }
  const fb = ev.target.closest("[data-fb]");
  if (fb) {
    fb.disabled = true;
    try { await api("/api/feedback", { id: fb.dataset.id, karar: fb.dataset.fb }); } catch (e) { alert(e.message); }
    return;
  }
  const win = ev.target.closest("[data-win]");
  if (win) {
    const inList = !!win.closest("#win-list");
    selectWindow(win.dataset.win === "live" ? null : Number(win.dataset.win), !inList && win.dataset.win !== "live");
    return;
  }
  const row = ev.target.closest("tr[data-row]");
  if (row) {
    const id = Number(row.dataset.row);
    if (openRows.has(id)) openRows.delete(id); else openRows.add(id);
    renderWindowDetail();
  }
});

$("#only-called").addEventListener("change", renderWindowList);

$("#sim-toggle").addEventListener("click", () =>
  api("/api/sim", { calisiyor: !(state && state.simulator.calisiyor) }).catch((e) => alert(e.message)));
$("#sim-rate").addEventListener("input", (ev) => { $("#sim-rate-val").textContent = fmt(ev.target.value); });
$("#sim-rate").addEventListener("change", (ev) => {
  ev.target.blur(); // odakta kalırsa sayfayı klavyeyle kaydırmak hızı değiştirir
  api("/api/sim", { hiz: Number(ev.target.value) }).catch((e) => alert(e.message));
});
$("#btn-missed").addEventListener("click", async () => {
  const text = prompt("Kaçırılan olayı kısaca tanımlayın:", "Kullanıcı bildirdi");
  if (text === null) return;
  try { await api("/api/missed", { aciklama: text }); } catch (e) { alert(e.message); }
});

// ------------------------------------------------------------------ ayarlar

const dlg = $("#settings");
$("#btn-settings").addEventListener("click", () => {
  if (!state) return;
  const llm = state.llm;
  dlg.querySelector(`input[name=mod][value=${llm.mod}]`).checked = true;
  $("#mod-claude").disabled = !llm.sdk;
  $("#claude-status").textContent = !llm.sdk
    ? "Claude modu için anthropic paketi gerekli: baslat.bat ilk kurulumda kurar (internet gerekir)."
    : llm.anahtar ? "anthropic paketi kurulu, API anahtarı tanımlı." : "anthropic paketi kurulu; API anahtarı girin.";
  $("#auto-fb").checked = state.ayarlar.oto_geri_bildirim;
  $("#budget").value = state.ayarlar.gunluk_token_butcesi;
  $("#api-key").value = "";
  $("#settings-msg").textContent = "";
  dlg.showModal();
});
$("#btn-save").addEventListener("click", async () => {
  const body = {
    llm_modu: dlg.querySelector("input[name=mod]:checked").value,
    oto_geri_bildirim: $("#auto-fb").checked,
    gunluk_token_butcesi: Number($("#budget").value) || 2000000,
  };
  const key = $("#api-key").value.trim();
  if (key) body.api_anahtari = key;
  try {
    await api("/api/settings", body);
    $("#api-key").value = "";
    dlg.close();
  } catch (e) {
    $("#settings-msg").textContent = `Kaydedilemedi: ${e.message}`;
  }
});
$("#btn-reset").addEventListener("click", async () => {
  if (!confirm("Öğrenilen her şey (log türleri, ağırlıklar, olaylar, senaryo sonuçları) silinecek ve sistem sıfırdan ısınmaya başlayacak. Emin misiniz?")) return;
  try {
    await api("/api/reset", {});
    Object.keys(cache).forEach((k) => delete cache[k]);
    selectedWindow = null;
    selectedData = null;
    dlg.close();
  } catch (e) { $("#settings-msg").textContent = `Sıfırlanamadı: ${e.message}`; }
});

poll();
