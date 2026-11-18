/* Bazo — application voyageur (PWA). Un seul fichier, aucune dépendance, CSP stricte (pas d'eval). */
"use strict";

const API = window.location.origin;
const LS_ID = "bazo_telegram_id", LS_TOKEN = "bazo_api_token";
const REFRESH_MS = 30000;
const NS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const svgEl = (tag, attrs = {}, parent) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (parent) parent.appendChild(e); return e; };
const vib = (p = 10) => { try { navigator.vibrate && navigator.vibrate(p); } catch (_) { /* ignore */ } };
const todayISO = () => new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Paris" });
const dateFr = (iso) => { const [y, m, d] = iso.split("-"); return new Date(+y, +m - 1, +d).toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" }); };
const STATUT_LIB = { fluide: "À l'heure", retard: "Retard", supprime: "Supprimé" };
const CLS = { fluide: "ok", retard: "retard", supprime: "supprime" };

const state = { id: localStorage.getItem(LS_ID) || "", token: localStorage.getItem(LS_TOKEN) || "", tab: "accueil",
  live: null, liveAt: 0, geo: null, trainNodes: new Map(), sens: "aller", types: null, profil: null, fil: { num: null }, timers: [] };

/* ------------------------------------------------------------------ réseau */
async function api(path, { method = "GET", body } = {}) {
  const h = { Accept: "application/json" };
  if (state.token) h.Authorization = `Bearer ${state.token}`;
  if (body !== undefined) h["Content-Type"] = "application/json";
  let r;
  try { r = await fetch(API + path, { method, headers: h, body: body !== undefined ? JSON.stringify(body) : undefined }); }
  catch (_) { const e = new Error("Pas de connexion. Réessaie dans un instant."); e.status = 0; throw e; }
  if (r.status === 401 || r.status === 403) { if (path.includes(encodeURIComponent(state.id))) { logout("Session expirée : reconnecte-toi avec un nouveau jeton (/token dans le bot)."); } }
  if (!r.ok) {
    let d = ""; try { const j = await r.json(); d = typeof j.detail === "string" ? j.detail : (j.detail && j.detail[0] && j.detail[0].msg) || ""; } catch (_) { /* vide */ }
    const e = new Error(d || `Erreur ${r.status}`); e.status = r.status; throw e;
  }
  if (r.status === 204) return null;
  return r.json();
}
const me = () => encodeURIComponent(state.id);

/* ------------------------------------------------------------------ UI utilitaires */
function toast(msg, kind = "") {
  const t = document.createElement("div"); t.className = `toast ${kind}`; t.textContent = msg; $("toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; setTimeout(() => t.remove(), 320); }, 3200);
  if (kind === "err") vib([20, 40, 20]); else if (kind === "ok") vib(12);
}
function modal(html) { $("modal-body").innerHTML = html; $("modal").classList.remove("hidden"); document.body.style.overflow = "hidden"; }
function closeModal() { $("modal").classList.add("hidden"); document.body.style.overflow = ""; }
$("modal").addEventListener("click", (e) => { if (e.target.dataset.close !== undefined || e.target.closest("[data-close]")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
const skeleton = () => `<div class="card"><div class="skel lg"></div><div class="skel md"></div><div class="skel sm"></div></div>`;
const empty = (titre, texte) => `<div class="empty"><b>${esc(titre)}</b>${esc(texte)}</div>`;
async function confirmer(titre, texte, okLabel = "Confirmer", cls = "primary") {
  return new Promise((res) => {
    modal(`<h3>${esc(titre)}</h3><p class="lead">${texte}</p><div class="btn-row"><button class="btn ghost" data-close>Annuler</button><button class="btn ${cls}" id="m-ok">${esc(okLabel)}</button></div>`);
    $("m-ok").onclick = () => { closeModal(); res(true); };
    $("modal").querySelector(".modal-back").onclick = () => res(false);
  });
}

/* ------------------------------------------------------------------ session */
function logout(msg) {
  state.token = ""; localStorage.removeItem(LS_TOKEN);
  state.timers.forEach(clearInterval); state.timers = [];
  $("app").classList.add("hidden"); $("onboard").classList.remove("hidden");
  $("in-id").value = state.id; $("login-err").textContent = msg || ""; $("login-err").classList.toggle("err", !!msg);
}
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("in-id").value.trim(), tok = $("in-token").value.trim();
  $("login-err").className = "form-msg"; $("login-err").textContent = "Connexion…";
  try {
    const h = { Authorization: `Bearer ${tok}` };
    const r = await fetch(`${API}/v1/profil/${encodeURIComponent(id)}`, { headers: h });
    if (r.status === 401 || r.status === 403) throw new Error("Jeton refusé. Dans le bot Telegram, tape /token pour en obtenir un nouveau.");
    if (r.status === 404) throw new Error("Identifiant inconnu : inscris-toi d'abord via @BazoTER_bot (/start).");
    if (!r.ok) throw new Error(`Erreur ${r.status}`);
    state.id = id; state.token = tok; localStorage.setItem(LS_ID, id); localStorage.setItem(LS_TOKEN, tok);
    state.profil = await r.json();
    demarrer();
  } catch (err) { $("login-err").className = "form-msg err"; $("login-err").textContent = err.message; }
});

function demarrer() {
  $("onboard").classList.add("hidden"); $("app").classList.remove("hidden");
  show(state.tab);
  state.timers.push(setInterval(() => { if (!document.hidden && state.tab === "accueil") chargerAccueil(true); }, REFRESH_MS));
  state.timers.push(setInterval(() => { majTrains(); majAge(); }, 1000));
}

/* ------------------------------------------------------------------ navigation */
const LOADERS = { accueil: () => chargerAccueil(), trajet: chargerTrajet, fil: chargerFil, entraide: chargerHub, moi: chargerMoi };
function show(tab) {
  state.tab = tab;
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("hidden", v.dataset.view !== tab));
  document.querySelectorAll(".tabbar button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  window.scrollTo({ top: 0 });
  LOADERS[tab]();
}
document.querySelectorAll(".tabbar button").forEach((b) => b.addEventListener("click", () => { vib(6); show(b.dataset.tab); }));
$("btn-go-trajet").addEventListener("click", () => show("trajet"));
function badgeTab(tab, on) { const b = document.querySelector(`.tabbar button[data-tab="${tab}"]`); let d = b.querySelector(".dot"); if (on && !d) { d = document.createElement("i"); d.className = "dot"; b.appendChild(d); } if (!on && d) d.remove(); }

/* ================================================================== ACCUEIL */
async function chargerAccueil(silencieux = false) {
  try {
    const [live, pending, statut] = await Promise.all([
      api(`/v1/live/${me()}`), api(`/v1/checkout/pending/${me()}`).catch(() => null), api(`/v1/statut/${me()}`).catch(() => null)]);
    state.live = live; state.liveAt = Date.now();
    if (live.reseau) { dessinerCarte(); majTrains(); majAge(); } else carteIndisponible();
    dessinerHero(); dessinerPending(pending); dessinerStats(statut);
    chargerGreveResume();
  } catch (e) {
    if (!silencieux) toast(e.message, "err");
    if (!state.live) { $("map-badge-txt").textContent = "Hors ligne"; $("nt-sub").textContent = e.message; }
  }
}

function carteIndisponible() {
  $("map-svg").innerHTML = ""; state.geo = null;
  $("map-badge").className = "pill"; $("map-badge-txt").textContent = "Temps réel en pause"; $("map-age").textContent = "";
}

const nowSrv = () => (state.live && state.live.reseau ? state.live.reseau.serveur_ts + (Date.now() - state.liveAt) / 1000 : Date.now() / 1000);

function courbe(pts) {
  const out = [];
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6], c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    out.push(`M${p1[0].toFixed(1)},${p1[1].toFixed(1)} C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`);
  }
  return out;
}

function dessinerCarte() {
  const r = state.live.reseau, svg = $("map-svg");
  const W = svg.clientWidth || 390, H = svg.clientHeight || 280;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.innerHTML = "";
  const defs = svgEl("defs", {}, svg);
  const f = svgEl("filter", { id: "blur", x: "-50%", y: "-50%", width: "200%", height: "200%" }, defs);
  svgEl("feGaussianBlur", { stdDeviation: "4.5" }, f);
  const st = r.stations, lats = st.map((s) => s.lat), lons = st.map((s) => s.lon);
  const latMin = Math.min(...lats), latMax = Math.max(...lats), lonMoy = (Math.min(...lons) + Math.max(...lons)) / 2, lonAmp = Math.max(...lons) - Math.min(...lons) || 1;
  const mx = 34, cy = H * 0.52, K = (H * 0.22) / lonAmp;
  const pts = st.map((s) => [mx + ((s.lat - latMin) / (latMax - latMin)) * (W - 2 * mx), cy + (s.lon - lonMoy) * K]);
  const glow = svgEl("g", {}, svg), core = svgEl("g", {}, svg), longueurs = [];
  courbe(pts).forEach((d, i) => {
    const cls = CLS[r.segments[i].statut] || "ok";
    svgEl("path", { d, class: `seg-glow ${cls}`, "stroke-width": 9 }, glow);
    longueurs.push(svgEl("path", { d, class: `seg-core ${cls}`, "stroke-width": 3.4 }, core).getTotalLength());
  });
  const cumul = [0]; longueurs.forEach((l, i) => cumul.push(cumul[i] + l));
  const moi = new Set(); if (state.live.prochain_train) { moi.add(state.live.prochain_train.uic_dep); moi.add(state.live.prochain_train.uic_arr); }
  const cle = new Set(["Nancy", "Metz", "Thionville", "Luxembourg"]);
  st.forEach((s, i) => {
    svgEl("circle", { cx: pts[i][0], cy: pts[i][1], r: moi.has(s.uic) ? 5.5 : 3.6, class: `st${moi.has(s.uic) ? " user" : ""}` }, svg);
    if (cle.has(s.nom) || moi.has(s.uic)) {
      const haut = i > 0 && i < st.length - 1 && pts[i][1] > cy;
      const bord = i === 0 ? "start" : i === st.length - 1 ? "end" : "middle";
      const x = i === 0 ? Math.max(8, pts[i][0] - 6) : i === st.length - 1 ? Math.min(W - 8, pts[i][0] + 6) : pts[i][0];
      svgEl("text", { x, y: pts[i][1] + (haut ? -15 : 24), class: "st-label key", "text-anchor": bord }, svg).textContent = s.nom;
    }
  });
  const couche = svgEl("g", {}, svg);
  state.geo = { pts, cumul, segNodes: [...core.children], couche, idxUic: Object.fromEntries(st.map((s, i) => [s.uic, i])) };
  state.trainNodes = new Map();
  const pire = r.resume.statut, nb = r.resume.nb_supprimes, rt = r.resume.nb_retards;
  $("map-badge").className = `pill ${CLS[pire] || "ok"}`;
  $("map-badge-txt").textContent = pire === "supprime" ? `Perturbé · ${nb} suppression${nb > 1 ? "s" : ""}` : pire === "retard" ? `Retards · ${rt} train${rt > 1 ? "s" : ""}` : "Trafic fluide";
}

function pointSurTrace(arc) {
  const { cumul, segNodes } = state.geo, total = cumul[cumul.length - 1];
  arc = Math.max(0, Math.min(total, arc)); let k = 0;
  while (k < segNodes.length - 1 && arc > cumul[k + 1]) k++;
  const p = segNodes[k].getPointAtLength(Math.max(0, arc - cumul[k])); return [p.x, p.y];
}
function positionTrain(t, now) {
  const g = state.geo, arr = t.stops.filter((s) => !s[3] && g.idxUic[s[0]] !== undefined);
  if (arr.length < 2 || now < arr[0][2] - 20 || now > arr[arr.length - 1][1] + 45) return null;
  const arc = (s) => g.cumul[g.idxUic[s[0]]];
  for (let i = 0; i < arr.length; i++) {
    if (now >= arr[i][1] && now <= arr[i][2]) return arc(arr[i]);
    if (i < arr.length - 1 && now > arr[i][2] && now < arr[i + 1][1]) { const f = (now - arr[i][2]) / Math.max(1, arr[i + 1][1] - arr[i][2]); return arc(arr[i]) + (arc(arr[i + 1]) - arc(arr[i])) * f; }
  }
  return arc(arr[0]);
}
function majTrains() {
  if (!state.live || !state.live.reseau || !state.geo || state.tab !== "accueil") return;
  const now = nowSrv(), vus = new Set(), moi = state.live.prochain_train && state.live.prochain_train.numero;
  for (const t of state.live.reseau.trains) {
    if (t.statut === "supprime") continue;
    const arc = positionTrain(t, now); if (arc === null) continue;
    vus.add(t.numero);
    const [x, y] = pointSurTrace(arc), couleur = t.statut === "retard" ? "#F5B840" : "#5EEAC0";
    let n = state.trainNodes.get(t.numero);
    if (!n) {
      const g = svgEl("g", { class: "train" }, state.geo.couche);
      n = { g, halo: svgEl("circle", { r: 8, class: "train-halo", fill: couleur }, g), noyau: svgEl("circle", { r: 3.8, class: "train-core", fill: couleur }, g) };
      if (t.numero === moi) svgEl("circle", { r: 9, class: "train-me" }, g);
      g.addEventListener("click", () => tip(t)); state.trainNodes.set(t.numero, n);
    }
    n.halo.setAttribute("fill", couleur); n.noyau.setAttribute("fill", couleur); n.g.setAttribute("transform", `translate(${x.toFixed(1)},${y.toFixed(1)})`);
  }
  for (const [num, n] of state.trainNodes) if (!vus.has(num)) { n.g.remove(); state.trainNodes.delete(num); }
}
let tipTimer;
function tip(t) {
  const dernier = t.stops[t.stops.length - 1], nom = (state.live.reseau.stations.find((s) => s.uic === dernier[0]) || {}).nom, el = $("map-tip");
  el.textContent = `Train ${t.numero}${nom ? " → " + nom : ""} · ${t.retard >= 5 ? "+" + t.retard + " min" : "à l'heure"}`;
  el.classList.remove("hidden"); clearTimeout(tipTimer); tipTimer = setTimeout(() => el.classList.add("hidden"), 3500); vib(5);
}
function majAge() {
  if (!state.live || !state.live.reseau) return;
  const age = Math.max(0, Math.round(nowSrv() - state.live.reseau.generated_ts)), el = $("map-age");
  el.textContent = age < 90 ? `à jour · ${age} s` : `il y a ${Math.round(age / 60)} min`; el.classList.toggle("old", age > 600);
}

function ligneTrain(v, tag = "") {
  const cls = CLS[v.statut] || "ok";
  const arr = v.statut === "supprime" ? "Ne circule pas sur ton trajet" : `Arrivée ${v.arrivee_reelle}${v.retard_arrivee >= 1 ? ` (prévue ${v.arrivee_plan})` : ""}`;
  return `<div class="row alt ${v.statut === "supprime" ? "supprime" : ""}"><div class="time">${esc(v.depart_plan)}</div><div class="l"><span>Train ${esc(v.numero)}${tag ? `<span class="tag">${esc(tag)}</span>` : ""}</span><small>${esc(arr)}</small></div><div class="r ${cls}">${esc(v.etiquette)}</div></div>`;
}
function dessinerHero() {
  const L = state.live, nt = L.prochain_train, p = L.profil || {};
  $("nt-status").className = "status"; $("nt-plan").textContent = "";
  if (!nt) {
    const raisons = { trajet_hors_sillon: "Ton trajet n'est pas encore couvert par la carte en direct — tes alertes et ton attestation fonctionnent normalement.", aucun_train: "Plus de train prévu sur ton trajet pour l'instant.", donnees_indisponibles: "Données temps réel indisponibles pour le moment.", temps_reel_indisponible: "Le suivi en direct reprend dans quelques minutes." };
    $("nt-eyebrow").textContent = "Mon prochain train";
    $("nt-route").textContent = p.gare_depart ? `${p.gare_depart} ⇄ ${p.gare_retour}` : "Mon trajet";
    $("nt-time").textContent = "—"; $("nt-status").textContent = ""; $("nt-sub").textContent = raisons[L.raison] || "Aucun train à afficher.";
  } else {
    const cls = CLS[nt.statut] || "ok";
    $("nt-eyebrow").textContent = (nt.habituel ? "Mon train habituel" : "Mon prochain train") + (nt.en_cours ? " · en route" : "");
    $("nt-route").textContent = `${nt.depart_gare} → ${nt.arrivee_gare}`;
    $("nt-time").textContent = nt.statut === "supprime" ? nt.depart_plan : nt.depart_reel;
    if (nt.statut !== "supprime" && nt.retard_depart >= 1) $("nt-plan").textContent = nt.depart_plan;
    $("nt-status").textContent = nt.etiquette; $("nt-status").className = `status ${cls}`;
    const parts = [`Train <b>${esc(nt.numero)}</b>`];
    if (nt.statut === "supprime") parts.push("ce train est <b>supprimé</b> sur ton trajet — regarde les alternatives ci-dessous.");
    else parts.push(`arrivée à ${esc(nt.arrivee_gare)} <b>${esc(nt.arrivee_reelle)}</b>${nt.retard_arrivee >= 1 ? ` <span class="tag amber">+${nt.retard_arrivee} min</span>` : ""}`);
    $("nt-sub").innerHTML = parts.join(" · ");
  }
  const alt = $("nt-alt");
  if (nt && nt.alternatives && nt.alternatives.length) { alt.innerHTML = `<h3>Alternatives qui circulent</h3>` + nt.alternatives.map((x) => ligneTrain(x, x.relation === "precedent" ? "avant" : "après")).join(""); alt.classList.remove("hidden"); } else alt.classList.add("hidden");
  const nx = $("nt-next"), dejaVus = new Set((nt && nt.alternatives ? nt.alternatives : []).map((x) => x.numero));
  const suivants = nt && nt.suivants ? nt.suivants.filter((v) => !dejaVus.has(v.numero)) : [];
  if (suivants.length) { nx.innerHTML = `<h3>Ensuite sur ton trajet</h3>` + suivants.map((v) => ligneTrain(v)).join(""); nx.classList.remove("hidden"); } else nx.classList.add("hidden");
  const b = $("btn-pdf"); b.disabled = !L.pdf.disponible;
  b.textContent = L.pdf.disponible ? `Attestation ${L.pdf.mois_libelle}` : "Attestation (après un trajet validé)";
  $("pdf-msg").textContent = "";
}
function dessinerPending(p) {
  const el = $("banner-checkout"); const n = p ? p.nb_pending : 0; badgeTab("trajet", n > 0);
  if (!n) { el.classList.add("hidden"); return; }
  el.className = "banner"; el.innerHTML = `<span class="grow">${n === 1 ? "Un trajet attend ta confirmation" : n + " trajets attendent ta confirmation"} — comment s'est passé ton train ?</span><span>›</span>`;
  el.onclick = () => show("trajet"); el.classList.remove("hidden");
}
function dessinerStats(s) {
  const el = $("card-stats"); if (!s) { el.innerHTML = ""; return; }
  const pct = Math.min(100, Math.round(Math.max(s.retard_min / 120, s.suppressions / 3) * 100));
  const lib = s.statut_gf.replace(/[*`_]/g, "").replace(/^\S+\s/, "");
  el.innerHTML = `<h3>${esc(s.mois_libelle)} · Garantie Fiabilité</h3><div class="kpis"><div class="kpi"><b class="tnum">${s.retard_min}</b><small>min de retard</small></div><div class="kpi"><b class="tnum">${s.suppressions}</b><small>suppression${s.suppressions > 1 ? "s" : ""}</small></div><div class="kpi"><b class="tnum">${s.nb_trajets}</b><small>trajet${s.nb_trajets > 1 ? "s" : ""} validé${s.nb_trajets > 1 ? "s" : ""}</small></div></div><div class="bar"><i style="width:${pct}%"></i></div><p class="mention">${esc(lib)} · ${esc(s.conseil_gf)}</p>`;
}
async function chargerGreveResume() {
  const el = $("card-greve-today");
  try {
    const g = await api(`/v1/greve/${me()}`);
    const j = g.jours.find((x) => x.actif && x.mode !== "veille" && x.nb_supprimes > 0);
    if (!j) { el.innerHTML = ""; return; }
    const miens = j.segments.reduce((a, s) => a + (s.nb_supprimes || 0), 0), total = j.segments.reduce((a, s) => a + s.trains.length, 0);
    el.innerHTML = `<h3>Mouvement social · ${esc(dateFr(j.date))}</h3><p style="margin:0 0 10px;font-size:14.5px">${total ? `Dans tes créneaux : <b>${miens}</b> train${miens > 1 ? "s" : ""} supprimé${miens > 1 ? "s" : ""} sur ${total}. ` : ""}Sur tout le Sillon : ${j.nb_supprimes} supprimés sur ${j.nb_total} suivis. ${j.source === "officiel" ? "Grille officielle importée." : "Détection automatique, affinée au fil des annonces."}</p><button class="btn small ghost" id="btn-greve-detail">Voir ma grille</button>`;
    $("btn-greve-detail").onclick = () => { show("moi"); setTimeout(() => { const t = $("moi-greve"); t && t.scrollIntoView({ behavior: "smooth" }); }, 200); };
  } catch (_) { el.innerHTML = ""; }
}
$("btn-pdf").addEventListener("click", () => genererPdf(state.live.pdf.mois, $("pdf-msg"), $("btn-pdf")));
async function genererPdf(mois, msgEl, btn) {
  if (btn) btn.disabled = true; if (msgEl) { msgEl.className = "hint"; msgEl.textContent = "Préparation de l'attestation…"; }
  try {
    const j = await api(`/v1/pdf/${me()}/link?mois=${encodeURIComponent(mois)}`, { method: "POST" });
    if (msgEl) msgEl.textContent = "Attestation prête — ouverture…"; vib(12);
    window.location.href = `${API}/v1/pdf/download?token=${encodeURIComponent(j.token)}`;
  } catch (e) { const m = e.status === 404 ? "Aucun incident validé sur cette période : rien à attester." : e.message; if (msgEl) { msgEl.className = "hint err"; msgEl.textContent = m; } else toast(m, "err"); }
  finally { if (btn) btn.disabled = false; }
}

/* ================================================================== TRAJET */
document.querySelectorAll("#sens-switch button").forEach((b) => b.addEventListener("click", () => { state.sens = b.dataset.sens; document.querySelectorAll("#sens-switch button").forEach((x) => x.classList.toggle("on", x === b)); chargerListeTrains(); }));
async function chargerTrajet() { chargerPendingTrajet(); chargerListeTrains(); chargerPropositions(); }
async function chargerPendingTrajet() {
  const el = $("trajet-pending");
  try {
    const p = await api(`/v1/checkout/pending/${me()}`); badgeTab("trajet", p.nb_pending > 0);
    if (!p.nb_pending) { el.classList.add("hidden"); return; }
    el.innerHTML = `<h3>À confirmer · ${esc(dateFr(p.date))}</h3>` + p.trajets.map((t) => `<div style="padding:8px 0 4px"><div style="font-weight:650">${esc(t.heure_theo)} · Train ${esc(t.numero_train)} <small>${esc(t.gare_depart || "")} → ${esc(t.gare_arrivee || "")}</small></div><p class="muted" style="margin:4px 0 10px">Comment s'est passé ce trajet ?</p><div class="btn-row">
      <button class="btn small ok" data-co="${t.id}" data-a="ok">À l'heure</button>
      <button class="btn small amber" data-co="${t.id}" data-a="suiv">Pris le suivant</button>
      <button class="btn small danger" data-co="${t.id}" data-a="suppr">Supprimé</button>
      <button class="btn small amber" data-co="${t.id}" data-a="bus">Bus de substitution</button>
      <button class="btn small ghost" data-co="${t.id}" data-a="rien">Pas pris</button></div></div>`).join("");
    el.classList.remove("hidden");
    el.querySelectorAll("[data-co]").forEach((b) => b.onclick = async () => {
      b.disabled = true;
      try { const r = await api(`/v1/checkout/${b.dataset.co}`, { method: "POST", body: { id_telegram: state.id, action: b.dataset.a } });
        toast(r.statut === "annule" ? "Trajet retiré." : r.statut === "valide" ? "Trajet validé — merci !" : `Trajet enregistré (${r.statut}).`, "ok"); chargerPendingTrajet(); }
      catch (e) { toast(e.message, "err"); b.disabled = false; }
    });
  } catch (_) { el.classList.add("hidden"); }
}
function destinationCourte(dest, arrivee) {
  const d = String(dest || "").replace(/\s*\(.*\)\s*$/, "").trim();
  return d && d.toLowerCase() !== String(arrivee || "").toLowerCase() ? d : "";
}
async function chargerListeTrains() {
  const el = $("trajet-liste"); el.innerHTML = skeleton(); $("trajet-ctx").textContent = "";
  try {
    const d = await api(`/v1/trains/${me()}?sens=${state.sens}`);
    $("trajet-ctx").textContent = `${d.gare_depart} → ${d.gare_arrivee} · ${d.tranche} · ${dateFr(d.date)}`;
    if (!d.trains.length) { el.innerHTML = empty("Aucun train trouvé", "Vérifie ton créneau dans Moi › Mon profil, ou réessaie dans un instant."); return; }
    el.innerHTML = `<h3>Quel train prends-tu ?</h3>` + d.trains.map((t, i) => `<div class="row tap ${t.supprime ? "supprime" : ""}" data-i="${i}"><div class="time">${esc(t.heure_theo)}</div><div class="l"><span>Train ${esc(t.numero_train)}</span><small>${t.heure_arrivee_theo ? "arrivée " + esc(t.heure_arrivee_theo) : ""}${destinationCourte(t.destination, d.gare_arrivee) ? (t.heure_arrivee_theo ? " · " : "") + "direction " + esc(destinationCourte(t.destination, d.gare_arrivee)) : ""}</small></div><div class="r ${t.supprime ? "supprime" : "neutre"}">${t.supprime ? "Supprimé" : "Choisir"}</div></div>`).join("");
    el.querySelectorAll(".row").forEach((r) => r.onclick = () => confirmerCheckin(d, d.trains[+r.dataset.i]));
  } catch (e) { el.innerHTML = empty("Impossible de charger les trains", e.message); }
}
async function confirmerCheckin(d, t) {
  vib(6);
  const ok = await confirmer(`Train de ${t.heure_theo}`, `${esc(d.gare_depart)} → ${esc(d.gare_arrivee)} · train <b>${esc(t.numero_train)}</b>${t.supprime ? "<br><br><b>Ce train est annoncé supprimé.</b> Le déclarer quand même permet de compter la suppression sur ton attestation." : ""}<br><br>Après le trajet, Bazo te demandera comment ça s'est passé.`, "Je prends ce train");
  if (!ok) return;
  try {
    await api("/v1/checkin", { method: "POST", body: { id_telegram: state.id, sens: state.sens, numero_train: t.numero_train, heure_theo: t.heure_theo, heure_arrivee_theo: t.heure_arrivee_theo, destination: t.destination, date: d.date } });
    toast("Trajet déclaré. Bon voyage !", "ok"); chargerPendingTrajet(); chargerFilChips();
  } catch (e) { toast(e.message, "err"); }
}
async function chargerPropositions() {
  const el = $("trajet-props");
  try {
    const p = await api(`/v1/propositions/${me()}`);
    const liste = p.propositions.slice(0, 8);
    if (!liste.length) { el.innerHTML = ""; el.classList.add("hidden"); return; }
    el.classList.remove("hidden");
    el.innerHTML = `<h3>Le radar a vu un incident sur tes trains</h3><p class="muted" style="margin:0 0 8px">Tu étais à bord ? Confirme pour l'ajouter à ton attestation. Sinon, écarte-le.</p>` + liste.map((x) => `<div class="row" style="padding-left:0;padding-right:0"><div class="l"><span>${esc(x.date.slice(8))}/${esc(x.date.slice(5, 7))} · ${esc(x.heure_theo)} · Train ${esc(x.numero_train)}</span><small>${esc(x.gare_depart)} → ${esc(x.gare_arrivee)} · ${x.type_incident === "supprime" ? "supprimé" : "+" + x.retard_min + " min"}${x.motif ? " · " + esc(x.motif) : ""}</small></div><div class="votes"><button class="btn ok" data-p="${x.id}" data-a="accepter">J'y étais</button><button class="btn ghost" data-p="${x.id}" data-a="refuser">Non</button></div></div>`).join("");
    el.querySelectorAll("[data-p]").forEach((b) => b.onclick = async () => { b.disabled = true; try { await api(`/v1/propositions/${me()}/${b.dataset.p}`, { method: "POST", body: { action: b.dataset.a } }); toast(b.dataset.a === "accepter" ? "Incident ajouté à ton attestation." : "Proposition écartée.", "ok"); chargerPropositions(); } catch (e) { toast(e.message, "err"); b.disabled = false; } });
  } catch (_) { el.innerHTML = ""; el.classList.add("hidden"); }
}

/* ================================================================== FIL (À bord) */
async function chargerFil() { await chargerFilChips(); }
async function chargerFilChips() {
  const chips = $("fil-trains"), cont = $("fil-contenu");
  try {
    const h = await api(`/v1/hub/${me()}`);
    const mes = h.mes_checkins || [];
    if (!mes.length) { chips.innerHTML = ""; cont.innerHTML = `<div class="card">${empty("Aucun train déclaré aujourd'hui", "Déclare ton trajet pour suivre et partager ce qui se passe à bord.")}<div class="btn-row"><button class="btn primary block" id="fil-go">Déclarer mon trajet</button></div></div>`; $("fil-go").onclick = () => show("trajet"); return; }
    if (!mes.some((m) => m.numero_train === state.fil.num)) state.fil.num = mes[0].numero_train;
    chips.innerHTML = mes.map((m) => `<button class="chip ${m.numero_train === state.fil.num ? "on" : ""}" data-n="${esc(m.numero_train)}" data-h="${esc(m.heure_theo)}">${esc(m.heure_theo)} · ${esc(m.numero_train)}</button>`).join("");
    chips.querySelectorAll(".chip").forEach((c) => c.onclick = () => { state.fil.num = c.dataset.n; chips.querySelectorAll(".chip").forEach((x) => x.classList.toggle("on", x === c)); chargerSignalements(h.date, c.dataset.n, c.dataset.h); });
    const cur = mes.find((m) => m.numero_train === state.fil.num);
    if (state.tab === "fil") chargerSignalements(h.date, cur.numero_train, cur.heure_theo);
  } catch (e) { if (state.tab === "fil") cont.innerHTML = `<div class="card">${empty("Impossible de charger", e.message)}</div>`; }
}
async function chargerSignalements(date, numero, heure) {
  const cont = $("fil-contenu"); cont.innerHTML = skeleton();
  try {
    const [s, types] = await Promise.all([api(`/v1/trains/${date}/${encodeURIComponent(numero)}/signalements?id_telegram=${me()}`), typesSignalement()]);
    const lib = Object.fromEntries(types.types.map((t) => [t.code, t.libelle]));
    const rows = s.signalements.map((x) => {
      const moi = x.id_auteur === state.id, statutCls = x.statut === "confirme" ? "ok" : x.statut === "refute" ? "coral" : "amber";
      const statutLib = x.statut === "confirme" ? "confirmé" : x.statut === "refute" ? "contesté" : "à confirmer";
      return `<div class="row" style="align-items:flex-start"><div class="l"><span>${esc(x.libelle || lib[x.code_type] || x.code_type)}<span class="tag ${statutCls}">${statutLib}</span></span><small>${x.nb_confirmations} confirment · ${x.nb_refutations} contestent${moi ? " · toi" : ""}</small>${moi && x.effet_pdf_possible ? `<div style="margin-top:8px"><button class="btn small ghost" data-traj="${x.id}">Inscrire sur mon attestation</button></div>` : ""}</div>${moi ? "" : `<div class="votes"><button class="btn ok" data-v="${x.id}" data-k="confirme">Oui</button><button class="btn danger" data-v="${x.id}" data-k="refute">Non</button></div>`}</div>`;
    }).join("");
    cont.innerHTML = `<div class="card" style="margin-top:8px"><h3>Train ${esc(numero)} · ${esc(heure)}</h3>${rows ? `<p class="muted" style="margin:0 0 6px">Les autres voyageurs ont-ils raison ? Confirme ou conteste.</p><div class="list-card" style="margin:0;box-shadow:none;border:1px solid var(--line)">${rows}</div>` : empty("Rien à signaler pour l'instant", "Tu es à bord ? Dis-le aux autres voyageurs.")}<div class="btn-row" style="margin-top:14px"><button class="btn primary block" id="btn-sig">Signaler quelque chose</button></div><p class="mention">${esc(s.mention)}</p></div>`;
    $("btn-sig").onclick = () => ouvrirSignalement(date, numero, heure, types);
    cont.querySelectorAll("[data-v]").forEach((b) => b.onclick = async () => { b.disabled = true; try { await api(`/v1/signalements/${b.dataset.v}/vote`, { method: "POST", body: { id_telegram: state.id, vote: b.dataset.k } }); toast("Merci, c'est noté.", "ok"); chargerSignalements(date, numero, heure); } catch (e) { toast(e.message, "err"); b.disabled = false; } });
    cont.querySelectorAll("[data-traj]").forEach((b) => b.onclick = async () => {
      const ok = await confirmer("C'est bien ton trajet ?", "Ce signalement sera inscrit sur ton attestation comme <b>déclaration du voyageur</b> (distinct des données officielles). Il faut avoir réellement pris ce train.", "Oui, inscrire");
      if (!ok) return;
      try { await api(`/v1/signalements/${b.dataset.traj}/trajet`, { method: "POST", body: { id_telegram: state.id, action: "inscrire" } }); toast("Inscrit sur ton attestation.", "ok"); chargerSignalements(date, numero, heure); } catch (e) { toast(e.message, "err"); }
    });
  } catch (e) { cont.innerHTML = `<div class="card">${empty("Impossible de charger", e.message)}</div>`; }
}
async function typesSignalement() { if (!state.types) state.types = await api("/v1/signalements/types"); return state.types; }
function ouvrirSignalement(date, numero, heure, types) {
  const t = types.types.filter((x) => x.code !== "je_suis_bloque");
  modal(`<h3>Que se passe-t-il ?</h3><p class="lead">Train ${esc(numero)} · ${esc(heure)}. Choisis un motif : pas de texte libre, c'est plus rapide et plus fiable.</p><div class="form-grid">${t.map((x) => `<button class="btn ${x.effet_pdf ? "" : "ghost"} block" data-code="${esc(x.code)}" data-val="${x.valeur_requise ? 1 : 0}" style="justify-content:space-between">${esc(x.libelle)}${x.effet_pdf ? '<span class="tag amber">attestation</span>' : ""}</button>`).join("")}</div><p class="mention">${esc(types.mention)}. Les signalements « attestation » te seront proposés pour ton PDF après confirmation.</p>`);
  $("modal-body").querySelectorAll("[data-code]").forEach((b) => b.onclick = async () => {
    let valeur = null;
    if (b.dataset.val === "1") { valeur = window.prompt("Numéro de voie annoncé en gare (ex. 3) :"); if (!valeur) return; }
    try { const r = await api("/v1/signalements", { method: "POST", body: { id_telegram: state.id, date, numero_train: numero, heure_theo: heure, code_type: b.dataset.code, valeur } }); closeModal(); toast(r.existant ? "Déjà signalé — merci." : "Signalement envoyé aux voyageurs.", "ok"); chargerSignalements(date, numero, heure); }
    catch (e) { toast(e.message, "err"); }
  });
}

/* ================================================================== ENTRAIDE */
async function chargerHub() {
  const el = $("hub-contenu"); el.innerHTML = skeleton();
  try {
    const h = await api(`/v1/hub/${me()}`); state.hub = h;
    const mes = h.mes_checkins || [];
    let html = "";
    if (h.hubs.length) html += h.hubs.map((x) => carteHub(x, h)).join("");
    html += `<section class="card"><h3>Je suis bloqué</h3>${mes.length ? `<p class="muted" style="margin:0 0 10px">Ton train ne part pas ou s'arrête ? Préviens les voyageurs du même quai. À ${h.seuils.quorum} personnes, un point d'entraide s'ouvre.</p><div class="form-grid">${mes.map((m) => `<button class="btn amber block" data-bl="${esc(m.numero_train)}" data-h="${esc(m.heure_theo)}" data-g="${esc(m.gare_depart)}">${esc(m.heure_theo)} · Train ${esc(m.numero_train)} depuis ${esc(m.gare_depart)}</button>`).join("")}</div>` : empty("Déclare d'abord ton trajet", "Le bouton « Je suis bloqué » s'active pour les trains que tu as déclarés aujourd'hui.")}</section>`;
    if (!h.hubs.length) html = `<section class="card soft" style="margin-top:14px"><h3>Comment ça marche</h3><p style="margin:0;font-size:14.5px;color:var(--ink-2)">Un point d'entraide s'ouvre automatiquement quand un train est <b>supprimé</b>, en <b>retard de ${h.seuils.retard_min} min</b> ou plus, ou quand <b>${h.seuils.quorum} voyageurs</b> se déclarent bloqués. Si l'attente dépasse <b>${h.seuils.attente_covoit_min / 60} h</b>, les conducteurs peuvent proposer des places — partage de frais plafonné (${h.bareme_eur_km} €/km partagé), rendez-vous en gare uniquement.</p></section>` + html;
    el.innerHTML = html + `<p class="mention" style="padding:0 4px">${esc(h.mention)}</p>`;
    el.querySelectorAll("[data-bl]").forEach((b) => b.onclick = async () => {
      const ok = await confirmer("Te déclarer bloqué ?", `Train <b>${esc(b.dataset.bl)}</b> de ${esc(b.dataset.h)} à ${esc(b.dataset.g)}. Les autres voyageurs de ce train le verront. Signalement communautaire, sans effet sur ton attestation.`, "Je suis bloqué", "amber");
      if (!ok) return;
      try { await api(`/v1/hub/${me()}/bloque`, { method: "POST", body: { date: h.date, numero_train: b.dataset.bl, heure_theo: b.dataset.h, gare: b.dataset.g } }); toast("C'est noté — courage.", "ok"); chargerHub(); } catch (e) { toast(e.message, "err"); }
    });
    el.querySelectorAll("[data-res]").forEach((b) => b.onclick = async () => { try { const r = await api(`/v1/hub/${me()}/offres/${b.dataset.res}/reserver`, { method: "POST" }); toast(`Place réservée · contact : ${r.offre.contact || "via le conducteur"}`, "ok"); chargerHub(); } catch (e) { toast(e.message, "err"); } });
    el.querySelectorAll("[data-annul]").forEach((b) => b.onclick = async () => { try { await api(`/v1/hub/${me()}/offres/${b.dataset.annul}/reserver`, { method: "DELETE" }); toast("Réservation annulée.", "ok"); chargerHub(); } catch (e) { toast(e.message, "err"); } });
    el.querySelectorAll("[data-stat]").forEach((b) => b.onclick = async () => { try { await api(`/v1/hub/${me()}/offres/${b.dataset.stat}/statut`, { method: "POST", body: { statut: b.dataset.s } }); toast(b.dataset.s === "partie" ? "Bonne route !" : "Offre annulée.", "ok"); chargerHub(); } catch (e) { toast(e.message, "err"); } });
    el.querySelectorAll("[data-abus]").forEach((b) => b.onclick = async () => { const ok = await confirmer("Signaler cette offre ?", "Pour un comportement inapproprié, un prix abusif ou une offre fantôme. Trois signalements suspendent l'offre.", "Signaler", "danger"); if (!ok) return; try { await api(`/v1/hub/${me()}/offres/${b.dataset.abus}/abus`, { method: "POST", body: { motif: "signalé depuis l'application" } }); toast("Merci, l'offre est surveillée.", "ok"); chargerHub(); } catch (e) { toast(e.message, "err"); } });
    el.querySelectorAll("[data-pub]").forEach((b) => b.onclick = () => ouvrirOffre(h.hubs.find((x) => String(x.id) === b.dataset.pub), h));
  } catch (e) { el.innerHTML = `<div class="card">${empty("Impossible de charger l'entraide", e.message)}</div>`; }
}
function carteHub(x, h) {
  const actif = x.statut === "actif", cls = actif ? "supprime" : x.statut === "ferme" ? "neutre" : "retard";
  const motif = { train_supprime: "train supprimé", terminus_anticipe: "terminus anticipé", retard_60: "retard ≥ 60 min", quorum_3_bloques: `${x.nb_bloques} voyageurs bloqués` }[x.motif] || (x.motif && x.motif.startsWith("quorum") ? `${x.nb_bloques} voyageurs bloqués` : null);
  const offres = (x.offres || []).map((o) => {
    const mine = o.est_conducteur, pass = o.est_passager;
    let actions = "";
    if (mine) actions = o.statut === "ouverte" || o.statut === "complete" ? `<button class="btn small ok" data-stat="${o.id}" data-s="partie">Je pars</button><button class="btn small ghost" data-stat="${o.id}" data-s="annulee">Annuler</button>` : "";
    else if (pass) actions = `<button class="btn small ghost" data-annul="${o.id}">Annuler ma place</button>`;
    else if (o.statut === "ouverte" && o.places_restantes > 0) actions = `<button class="btn small primary" data-res="${o.id}">Réserver</button><button class="btn small ghost" data-abus="${o.id}" aria-label="Signaler">⚑</button>`;
    const statutLib = { ouverte: `${o.places_restantes} place${o.places_restantes > 1 ? "s" : ""}`, complete: "Complet", partie: "Partie", annulee: "Annulée", suspendue: "Suspendue" }[o.statut] || o.statut;
    return `<div class="row" style="padding-left:0;padding-right:0;align-items:flex-start"><div class="l"><span>${esc(o.conducteur)} → ${esc(o.destination)} · départ ${esc(o.depart)}</span><small>${esc(o.point_rdv)} · ${o.participation_eur ? o.participation_eur.toFixed(2).replace(".", ",") + " € par passager" : "gratuit"}${o.contact ? ` · <b>${esc(o.contact)}</b>` : ""}${mine && o.passagers && o.passagers.length ? " · passagers : " + esc(o.passagers.join(", ")) : ""}</small><div class="btn-row" style="margin-top:8px">${actions}</div></div><div class="r ${o.statut === "ouverte" ? "ok" : "neutre"}">${statutLib}</div></div>`;
  }).join("");
  return `<section class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><h3 style="margin:0">${esc(x.gare)} · Train ${esc(x.numero_train)} de ${esc(x.heure_theo)}</h3><span class="r ${cls}">${actif ? "Actif" : x.statut === "ferme" ? "Fermé" : "En attente"}</span></div>
    <p style="margin:10px 0 0;font-size:14.5px;color:var(--ink-2)">${actif ? `Point d'entraide ouvert${motif ? " — " + esc(motif) : ""}.` : `${x.nb_bloques}/${h.seuils.quorum} voyageurs bloqués${x.moi_bloque ? " (dont toi)" : ""}. L'entraide s'ouvre au seuil ou si le radar confirme l'incident.`}${x.attente_min != null ? ` Prochain train utile dans <b>${x.attente_min >= 60 ? Math.floor(x.attente_min / 60) + " h " + String(x.attente_min % 60).padStart(2, "0") : x.attente_min + " min"}</b>.` : ""}</p>
    ${actif && x.covoiturage_ouvert ? `<p style="margin:8px 0 0;font-size:14px"><b>Entraide voiture ouverte</b> — attente de plus de 2 h. Rendez-vous en gare uniquement.</p>${offres ? `<div style="margin-top:6px">${offres}</div>` : `<p class="muted" style="margin:8px 0 0">Aucune offre pour l'instant.</p>`}<div class="btn-row" style="margin-top:12px"><button class="btn primary block" data-pub="${x.id}">Je conduis, je propose des places</button></div>` : actif ? `<p class="muted" style="margin:8px 0 0">L'entraide voiture s'ouvre si l'attente dépasse 2 h. En attendant : alternatives sur l'écran Accueil.</p>` : ""}</section>`;
}
function ouvrirOffre(hub, h) {
  if (!hub) return;
  const dests = (state.live && state.live.reseau ? state.live.reseau.stations.map((s) => s.nom) : ["Metz", "Thionville", "Luxembourg", "Nancy"]).filter((n) => n !== hub.gare);
  const now = new Date(); now.setMinutes(now.getMinutes() + 15); const hh = now.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris" }).replace(":", "h");
  modal(`<h3>Proposer des places</h3><p class="lead">Depuis ${esc(hub.gare)}. Dépose en gare uniquement. Participation plafonnée à ${h.bareme_eur_km} €/km partagés entre tous.</p>
  <form id="f-offre" class="form-grid">
    <label>Destination<select id="o-dest">${dests.map((d) => `<option>${esc(d)}</option>`).join("")}</select></label>
    <div class="form-2"><label>Places<select id="o-places">${[1, 2, 3, 4].map((n) => `<option ${n === 3 ? "selected" : ""}>${n}</option>`).join("")}</select></label><label>Départ<input id="o-dep" value="${hh}" pattern="\\d{2}h\\d{2}" required /></label></div>
    <label>Point de rendez-vous<select id="o-rdv">${Object.entries(h.points_rdv).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("")}</select></label>
    <div class="form-2"><label>Participation par passager (€)<input id="o-eur" type="number" min="0" max="50" step="0.5" value="0" /></label><label>Contact (visible des passagers)<input id="o-contact" placeholder="06… ou @telegram" maxlength="80" /></label></div>
    <label class="check"><input type="checkbox" id="o-charte" required /><span>${esc(h.charte)}</span></label>
    <p id="o-msg" class="form-msg"></p>
    <button class="btn primary big" type="submit">Publier mon offre</button></form>`);
  $("f-offre").onsubmit = async (e) => {
    e.preventDefault(); const m = $("o-msg"); m.className = "form-msg"; m.textContent = "Publication…";
    try {
      const r = await api(`/v1/hub/${me()}/offres`, { method: "POST", body: { hub_id: hub.id, destination: $("o-dest").value, places: +$("o-places").value, point_rdv_code: $("o-rdv").value, depart: $("o-dep").value, participation_eur: +$("o-eur").value, contact: $("o-contact").value || null, accepte_charte: $("o-charte").checked } });
      closeModal(); toast(`Offre publiée${r.plafond_eur != null ? ` · plafond ${r.plafond_eur.toFixed(2).replace(".", ",")} € par passager` : ""}`, "ok"); chargerHub();
    } catch (err) { m.className = "form-msg err"; m.textContent = err.message; }
  };
}

/* ================================================================== MOI */
async function chargerMoi() {
  const el = $("moi-contenu"); el.innerHTML = skeleton();
  try {
    const [p, hab, greve, pdf, statut] = await Promise.all([api(`/v1/profil/${me()}`), api(`/v1/habituels/${me()}`).catch(() => ({ habituels: [] })), api(`/v1/greve/${me()}`).catch(() => null), api(`/v1/pdf/mois/${me()}`).catch(() => ({ mois: [] })), api(`/v1/statut/${me()}`).catch(() => null)]);
    state.profil = p;
    $("moi-titre").textContent = p.prenom && p.prenom !== "Voyageur" ? `Bonjour ${p.prenom}` : "Moi";
    $("moi-sous").textContent = `${p.gare_depart} ${p.heures} ⇄ ${p.gare_retour} ${p.heures_retour}`;
    el.innerHTML = `
      <section class="card"><div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0">Mon profil</h3><button class="btn small ghost" id="btn-edit">Modifier</button></div>
        <div class="row" style="padding-left:0;padding-right:0"><div class="l"><span>Aller · ${esc(p.gare_depart)} → ${esc(p.gare_retour)}</span><small>${esc(p.heures)}</small></div></div>
        <div class="row" style="padding-left:0;padding-right:0"><div class="l"><span>Retour · ${esc(p.gare_retour)} → ${esc(p.gare_depart)}</span><small>${esc(p.heures_retour)}</small></div></div>
        <div class="row switch" style="padding-left:0;padding-right:0"><div class="l"><span>Alertes temps réel</span><small>Retard, suppression, bascule sur un autre train</small></div><input type="checkbox" id="sw-alertes" ${p.alertes_actives ? "checked" : ""} /></div></section>
      <section class="card"><h3>Mes trains habituels</h3><p class="muted" style="margin:0 0 8px">Ils sont surveillés en priorité et affichés en premier sur l'accueil.</p>
        ${hab.habituels.length ? hab.habituels.map((x) => `<div class="row" style="padding-left:0;padding-right:0"><div class="time">${esc(x.heure)}</div><div class="l"><span>${esc(x.gare_depart)} → ${esc(x.gare_arrivee)}</span><small>${x.manuel ? "déclaré" : "déduit de tes trajets"}${x.numero_train ? " · train " + esc(x.numero_train) : ""}</small></div><button class="btn small ghost" data-delhab="${esc(x.heure)}" data-sens="${x.gare_depart === p.gare_depart ? "aller" : "retour"}">Retirer</button></div>`).join("") : `<p class="muted">Aucun pour l'instant.</p>`}
        <form id="f-hab" class="form-2" style="margin-top:10px;align-items:end"><label>Sens<select id="h-sens"><option value="aller">Aller</option><option value="retour">Retour</option></select></label><label>Heure de départ<input id="h-heure" placeholder="08h33" pattern="\\d{2}h\\d{2}" required /></label><button class="btn primary block" style="grid-column:1/-1" type="submit">Ajouter</button></form></section>
      ${greve ? `<section class="card" id="moi-greve"><h3>Grève & plans de transport</h3>${greve.jours.map((j) => `<div style="padding:6px 0"><div style="font-weight:650">${esc(dateFr(j.date))} <span class="tag ${j.mode === "veille" ? "" : j.nb_supprimes ? "coral" : "ok"}">${j.mode === "veille" ? "veille" : j.source === "officiel" ? "grille officielle" : "détection auto"}</span></div>${j.mode === "veille" ? `<small>Aucun mouvement social annoncé.</small>` : j.segments.map((s) => `<small style="display:block;margin-top:4px"><b>${esc(s.gare_depart)} → ${esc(s.gare_arrivee)}</b> · ${s.fenetre || ""} · ${s.nb_supprimes} supprimé${s.nb_supprimes > 1 ? "s" : ""} / ${s.trains.length}</small><div class="chips" style="padding:6px 0 0">${s.trains.map((t) => `<span class="chip ${t.statut === "supprime" ? "supprime" : t.statut === "maintenu" || t.statut === "circule" ? "ok" : ""}" style="min-height:30px;padding:4px 10px;font-size:13px">${esc(t.heure)}${t.statut === "supprime" ? " ✕" : ""}</span>`).join("")}</div>`).join("")}</div>`).join("")}<p class="mention">${greve.jours.some((j) => j.mode === "auto") ? "Détection automatique : la grille s'affine au fil des annonces SNCF. Un train annoncé maintenu peut encore changer." : ""}</p></section>` : ""}
      <section class="card"><h3>Mon attestation Garantie Fiabilité</h3>${statut ? `<p style="margin:0 0 10px;font-size:14.5px">${esc(statut.mois_libelle)} : <b>${statut.retard_min} min</b> de retard validées, <b>${statut.suppressions}</b> suppression${statut.suppressions > 1 ? "s" : ""}. ${esc(statut.conseil_gf)}</p>` : ""}
        <div class="form-2" style="align-items:end"><label>Mois<select id="pdf-mois">${pdf.mois.map((m) => `<option value="${esc(m.code)}">${esc(m.libelle)}</option>`).join("")}</select></label><button class="btn primary" id="btn-pdf-moi">Télécharger le PDF</button></div><p class="hint" id="pdf-msg-moi"></p>
        <div class="btn-row" style="margin-top:8px"><button class="btn ghost" id="btn-reclam">Kit réclamation SNCF</button><button class="btn ghost" id="btn-incidents">Mes incidents du mois</button></div></section>
      <section class="card soft"><h3>Mon compte</h3><div class="btn-row"><button class="btn ghost" id="btn-export">Exporter mes données</button><button class="btn ghost" id="btn-logout">Se déconnecter</button><button class="btn danger" id="btn-delete">Supprimer mon compte</button></div><p class="mention">Bazo est un service indépendant de voyageurs, sans lien avec SNCF ni CFL. Données hébergées en France, jamais revendues.</p></section>`;
    $("btn-edit").onclick = () => ouvrirProfil(p);
    $("sw-alertes").onchange = async (e) => { try { await api(`/v1/profil/${me()}`, { method: "PUT", body: { alertes_actives: e.target.checked } }); toast(e.target.checked ? "Alertes activées." : "Alertes en pause.", "ok"); } catch (err) { toast(err.message, "err"); e.target.checked = !e.target.checked; } };
    $("f-hab").onsubmit = async (e) => { e.preventDefault(); try { await api(`/v1/habituels/${me()}`, { method: "POST", body: { sens: $("h-sens").value, heure: $("h-heure").value.trim() } }); toast("Train habituel ajouté.", "ok"); chargerMoi(); } catch (err) { toast(err.message, "err"); } };
    el.querySelectorAll("[data-delhab]").forEach((b) => b.onclick = async () => { try { await api(`/v1/habituels/${me()}?sens=${b.dataset.sens}&heure=${encodeURIComponent(b.dataset.delhab)}`, { method: "DELETE" }); toast("Retiré.", "ok"); chargerMoi(); } catch (err) { toast(err.message, "err"); } });
    $("btn-pdf-moi").onclick = () => genererPdf($("pdf-mois").value, $("pdf-msg-moi"), $("btn-pdf-moi"));
    $("btn-reclam").onclick = ouvrirReclamation; $("btn-incidents").onclick = ouvrirIncidents;
    $("btn-export").onclick = exporterDonnees; $("btn-logout").onclick = () => logout("");
    $("btn-delete").onclick = async () => { const ok = await confirmer("Supprimer ton compte ?", "Profil, trajets et signalements seront effacés définitivement. Les attestations déjà téléchargées restent valables.", "Supprimer définitivement", "danger"); if (!ok) return; try { await api(`/v1/account/${me()}`, { method: "DELETE" }); localStorage.clear(); state.id = ""; logout("Compte supprimé. À bientôt peut-être."); } catch (e) { toast(e.message, "err"); } };
  } catch (e) { el.innerHTML = `<div class="card">${empty("Impossible de charger ton profil", e.message)}</div>`; }
}
const TRANCHES = ["04h-06h", "06h-08h", "08h-10h", "10h-12h", "12h-14h", "14h-16h", "16h-18h", "18h-20h", "20h-22h"];
async function ouvrirProfil(p) {
  let gares = [];
  try { gares = (await api("/v1/geo/gares?region=grand_est")).gares.map((g) => g.nom); } catch (_) { gares = [p.gare_depart, p.gare_retour]; }
  const opt = (liste, cur) => liste.map((g) => `<option ${g === cur ? "selected" : ""}>${esc(g)}</option>`).join("");
  modal(`<h3>Mon profil</h3><p class="lead">Tes gares et créneaux servent au radar, aux alertes et à la liste des trains à déclarer.</p><form id="f-profil" class="form-grid">
    <label>Prénom<input id="p-prenom" value="${esc(p.prenom || "")}" maxlength="40" /></label>
    <div class="form-2"><label>Gare de départ<select id="p-gd">${opt(gares, p.gare_depart)}</select></label><label>Créneau aller<select id="p-h">${opt(TRANCHES, p.heures)}</select></label></div>
    <div class="form-2"><label>Gare d'arrivée<select id="p-gr">${opt(gares, p.gare_retour)}</select></label><label>Créneau retour<select id="p-hr">${opt(TRANCHES, p.heures_retour)}</select></label></div>
    <p id="p-msg" class="form-msg"></p><button class="btn primary big" type="submit">Enregistrer</button></form>`);
  $("f-profil").onsubmit = async (e) => {
    e.preventDefault(); const m = $("p-msg"); m.className = "form-msg"; m.textContent = "Enregistrement…";
    try { await api(`/v1/profil/${me()}`, { method: "PUT", body: { prenom: $("p-prenom").value.trim() || null, gare_depart: $("p-gd").value, gare_retour: $("p-gr").value, heures: $("p-h").value, heures_retour: $("p-hr").value } }); closeModal(); toast("Profil mis à jour.", "ok"); chargerMoi(); state.live = null; }
    catch (err) { m.className = "form-msg err"; m.textContent = err.message; }
  };
}
async function ouvrirReclamation() {
  modal(`<h3>Kit réclamation</h3>${skeleton()}`);
  try {
    const k = await api(`/v1/reclamation/${me()}`);
    const texte = k.texte.replace(/\*|`/g, "");
    const bloc = (texte.match(/```([\s\S]*?)```/) || [null, null])[1];
    modal(`<h3>Kit réclamation · ${esc(k.mois)}</h3><p class="lead">Joins ton attestation PDF sur le portail SNCF et colle ce texte.</p><div class="btn-row" style="margin-bottom:12px"><a class="btn primary" href="https://www.sncf-voyageurs.com/fr/contactez-nous/demande-et-reclamation/" target="_blank" rel="noopener">Ouvrir le portail SNCF</a><button class="btn ghost" id="btn-copy">Copier le texte</button></div><pre class="copy" id="reclam-txt">${esc(bloc || texte)}</pre>`);
    $("btn-copy").onclick = async () => { try { await navigator.clipboard.writeText($("reclam-txt").textContent); toast("Texte copié.", "ok"); } catch (_) { toast("Sélectionne le texte pour le copier.", ""); } };
  } catch (e) { modal(`<h3>Kit réclamation</h3><p class="lead">${esc(e.message)}</p>`); }
}
async function ouvrirIncidents() {
  modal(`<h3>Mes incidents</h3>${skeleton()}`);
  try {
    const d = await api(`/v1/incidents/${me()}`);
    modal(`<h3>Mes incidents · ${esc(d.mois)}</h3>${d.incidents.length ? `<div class="list-card" style="margin:0">${d.incidents.map((i) => `<div class="row"><div class="time">${esc(i.heure || "")}</div><div class="l"><span>${esc(i.date.slice(8))}/${esc(i.date.slice(5, 7))} · Train ${esc(i.train)}</span><small>${esc(i.trajet || "")}${i.motif ? " · " + esc(i.motif) : ""} · ${esc(i.provenance_libelle || "")}</small></div><div class="r ${i.type === "supprime" ? "supprime" : "retard"}">${i.type === "supprime" ? "Supprimé" : "+" + i.retard + " min"}</div></div>`).join("")}</div>` : empty("Aucun incident validé ce mois", "Bonne nouvelle pour tes trajets, moins pour l'attestation.")}`);
  } catch (e) { modal(`<h3>Mes incidents</h3><p class="lead">${esc(e.message)}</p>`); }
}
async function exporterDonnees() {
  try {
    const r = await fetch(`${API}/v1/account/${me()}/export`, { headers: { Authorization: `Bearer ${state.token}` } });
    if (!r.ok) throw new Error(`Erreur ${r.status}`);
    const blob = await r.blob(), a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `bazo_export_${state.id}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 5000); toast("Export téléchargé.", "ok");
  } catch (e) { toast(e.message, "err"); }
}

/* ------------------------------------------------------------------ démarrage */
window.addEventListener("resize", () => { if (state.live && state.live.reseau && state.tab === "accueil") { dessinerCarte(); majTrains(); } });
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.token && state.tab === "accueil") chargerAccueil(true); });
window.addEventListener("online", () => toast("Connexion rétablie.", "ok"));
window.addEventListener("offline", () => toast("Hors ligne — les données affichées peuvent dater.", ""));
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => { /* hors PWA */ });
if (state.id && state.token) demarrer(); else logout("");
