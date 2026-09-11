let token = "", state = null;
const $ = id => document.getElementById(id);
const num = (x, digits=0) => x == null ? "—" : Number(x).toLocaleString("de-CH", {maximumFractionDigits:digits});
const pct = x => x == null ? "insufficient_data" : num(x*100,1)+" %";
function intervalLabel(p) {
 return p.audit?.interval_kind === "empirical_80" ? "Empirisches 80%-Intervall" : "Unkalibrierter Szenariobereich";
}
const esc = x => String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function api(url, data) {
 const response = await fetch(url, {method:data?"POST":"GET",headers:{Authorization:"Bearer "+token,...(data?{"Content-Type":"application/json"}:{})},body:data?JSON.stringify(data):undefined});
 const body = await response.json();
 if (!response.ok) throw Error(typeof body.detail === "string" ? body.detail : "Eingaben prüfen oder Verbindung erneut versuchen.");
 return body;
}
async function guarded(action){$("error").textContent="";try{await action()}catch(e){$("error").textContent=e.message}}
$("loginForm").addEventListener("submit",e=>{e.preventDefault();token=$("token").value;guarded(load)});
$("refresh").onclick=()=>guarded(load);
async function load(){
 state=await api("/api/dashboard");$("login").hidden=true;$("workspace").hidden=false;$("token").value="";
 $("connection").textContent=state.channels.map(c=>c.title).join(" · ")||"Wartet auf erste Synchronisierung";
 $("banner").textContent=state.demo?"DEMO · Alle gezeigten Werte sind synthetische Testdaten.":state.sync?
 "Letzter Import: "+new Date(state.sync.finished_at||state.sync.started_at).toLocaleString("de-CH")+" · "+state.sync.status+(state.sync.issues.length?" · "+state.sync.issues.join(" / "):""):
 "Noch nicht verbunden. Es wurden keine echten YouTube-Daten importiert.";
 $("subscribers").textContent=num(state.channels.reduce((s,c)=>s+(c.subscribers||0),0));
 $("views").textContent=num(state.videos.reduce((s,v)=>s+(v.views||0),0));
 $("winners").textContent=state.videos.filter(v=>v.direction==="Gewinnt").length;
 $("learned").textContent=num(state.learning.evaluated_forecasts);
 $("empty").hidden=state.videos.length>0;
 $("videos").innerHTML=state.videos.map(v=>`<tr><td><button data-video="${esc(v.id)}">${esc(v.title)}</button><small>${v.focus?"FOKUSVIDEO · ":""}${esc(v.content_type)} · ${num(v.duration/60,1)} min</small></td><td class="${v.direction==="Gewinnt"?"up":v.direction==="Verliert"?"down":""}">${num(v.score,1)}<small>${esc(v.direction)} · ${esc(v.regime||"unknown")}</small></td><td>${num(v.velocity,1)}</td><td>${num(v.acceleration,2)}</td><td>${num(v.views)}</td><td>${v.subscriber_conversion==null?"—":pct(v.subscriber_conversion)}</td></tr>`).join("");
 document.querySelectorAll("[data-video]").forEach(b=>b.onclick=()=>guarded(()=>detail(b.dataset.video)));
 await loadMemory();
}
function chart(rows, x, y){
 if(rows.length<2)return "<p class='muted'>Noch nicht genügend Messpunkte.</p>";
 const xx=rows.map(x),yy=rows.map(y),min=Math.min(...yy),max=Math.max(...yy);
 const points=rows.map((_,i)=>((xx[i]-xx[0])/Math.max(1,xx.at(-1)-xx[0])*600).toFixed(2)+","+(140-(yy[i]-min)/Math.max(0.01,max-min)*125).toFixed(2)).join(" ");
 return `<svg class="spark" viewBox="0 0 600 150" role="img" aria-label="Verlauf, Werte von ${esc(num(min,2))} bis ${esc(num(max,2))}"><polyline points="${points}"/></svg><p class="muted">Wertebereich: ${num(min,2)} bis ${num(max,2)}</p>`;
}
async function detail(id){
 const d=await api("/api/videos/"+encodeURIComponent(id)),v=state.videos.find(v=>v.id===id);
 const retention=d.reports.retention?.rows||[],traffic=d.reports.traffic?.rows||[];
 const impressions=d.reach.reduce((s,r)=>s+r.impressions,0);
 const ctr=impressions?d.reach.reduce((s,r)=>s+r.impressions*(r.ctr||0),0)/impressions:null;
 $("detail").hidden=false;
 $("detail").innerHTML=`<div class="card"><p class="eyebrow">VIDEOANALYSE</p><h2>${esc(v.title)}</h2><p class="muted">Letzter Snapshot: ${esc(v.last_snapshot)} · Analytics bis ${esc(v.analytics_end||"unbekannt")} · ${v.peer_count} Vergleichsvideos</p>${chart(d.snapshots,r=>new Date(r.observed_at).getTime(),r=>r.views)}<h3>Growth-Zeitfenster</h3><p>Regime: <strong>${esc(v.regime)}</strong> · Evidenz: ${esc(v.evidence_quality)}</p><table><thead><tr><th>Fenster</th><th>Views/h</th><th>Views/h²</th><th>Datenqualität</th></tr></thead><tbody>${Object.entries(v.windows||{}).map(([label,w])=>`<tr><td>${esc(label)}</td><td>${num(w.velocity,2)}</td><td>${num(w.acceleration,3)}</td><td>${esc(w.quality)}</td></tr>`).join("")}</tbody></table><p class="muted">CTR und Analytics: ${esc(JSON.stringify(v.metric_status||{}))}. Regime sind beschreibende Signale, keine Breakout-Wahrscheinlichkeiten.</p><h3>Prognostizierte Gesamtviews</h3><p class="muted">Ziele beziehen sich auf Gesamtzähler unter der Bedingung organischen zukünftigen Zuwachses, nicht auf nachgewiesene organische Lifetime-Views. Fehlende Daten: insufficient_data. Gesamtzähler sind nicht automatisch rein organisch. Bei erkanntem Werbetraffic werden neue Prognosen ausgesetzt. Wahrscheinlichkeiten sind empirische Schätzungen, insbesondere im Millionenbereich unsicher.</p><div class="forecast">${v.forecasts.map(p=>`<article><span>${p.hours===24?"24 Stunden":p.hours===168?"7 Tage":"30 Tage"}</span><strong>${num(p.views)}</strong><small>${num(p.lower)} – ${num(p.upper)}</small><p class="muted">${intervalLabel(p)} · n=${p.n}</p><p class="muted">100.000: ${pct(p.p100k)}<br>1.000.000: ${pct(p.p1m)}</p><p class="muted">Confidence: ${esc(p.audit?.confidence||"insufficient_data")} · Regime: ${esc(p.audit?.regime||v.regime)}<br>100k: ${esc(p.audit?.targets?.["100000"]?.status||"insufficient_data")}<br>1M: ${esc(p.audit?.targets?.["1000000"]?.status||"insufficient_data")}<br>Modell: ${esc(p.model)}</p><p class="muted">Bis ${new Date(p.target).toLocaleString("de-CH")}</p></article>`).join("")||"<p>Prognosen benötigen mehrere aktuelle Snapshots.</p>"}</div></div>
 <div class="detail-grid"><div class="card"><h3>Was die Messwerte zeigen</h3>${v.reasons.map(r=>"<p>"+esc(r)+"</p>").join("")}<h3>Priorisierte Tests (Hypothesen)</h3>${(v.forecasts[0]?.recommendations||[]).map(r=>`<p><strong>P${r.priority} · ${esc(r.dimension)}</strong>: ${esc(r.hypothesis)}<br>${esc(r.test)}<br><small>${esc(r.confidence)} · ${esc(r.evidence||"insufficient_data")}</small></p>`).join("")}<h3>Nächste Schritte</h3><ul>${v.actions.map(r=>"<li>"+esc(r)+"</li>").join("")}</ul><p class="muted">Aktionen sind Vorschläge. Am YouTube-Kanal wird nichts automatisch geändert.</p></div>
 <div class="card"><h3>Qualität & Monetarisierung</h3><p>Organische Views im Traffic-Bericht: ${num(v.organic_views_reported)}</p><p>Als Werbung klassifizierte Views: ${num(v.advertising_views_reported)}</p><p>Watchtime-Effizienz: ${v.watchtime_efficiency==null?"—":pct(v.watchtime_efficiency)}</p><p>Gewonnene / Netto-Abonnenten: ${num(v.subscribers_gained)} / ${num(v.subscribers_net)}</p><p>Thumbnail-Impressionen: ${d.reach.length?num(impressions):"Nicht verfügbar"}</p><p>Gewichtete CTR: ${ctr==null?"Nicht verfügbar":pct(ctr)}</p><p class="muted">Reichweite: letzte ${d.reach.length} verfügbare Tagesberichte.</p><p>Umsatz: ${num(d.monetization.revenue,2)} USD · RPM: ${num(d.monetization.rpm,2)} USD</p><p class="muted">Returning Viewers und langfristiger Zuschauerwert: derzeit nicht verfügbar.</p></div></div>
 <div class="detail-grid"><div class="card"><h3>Audience Retention</h3>${chart(retention,r=>r.elapsedVideoTimeRatio,r=>r.audienceWatchRatio)}<p class="muted">X: relativer Videofortschritt · Y: Wiedergaberatio. Wiederholungen können Werte über 100 % ergeben.</p></div><div class="card"><h3>Traffic-Quellen</h3>${traffic.map(r=>"<p>"+esc(r.insightTrafficSourceType)+" <strong>"+num(r.views)+"</strong></p>").join("")||"<p class='muted'>Noch kein Bericht verfügbar.</p>"}</div></div>`;
 $("detail").scrollIntoView({behavior:"smooth",block:"start"});
}
function tab(memory){$("memoryPanel").hidden=!memory;$("videosPanel").hidden=memory;$("memoryTab").classList.toggle("selected",memory);$("videosTab").classList.toggle("selected",!memory)}
$("memoryTab").onclick=()=>tab(true);$("videosTab").onclick=()=>tab(false);
$("decisionForm").onsubmit=e=>{e.preventDefault();guarded(async()=>{const d=Object.fromEntries(new FormData(e.target));d.horizon_hours=Number(d.horizon_hours);d.expected_views_gain=Number(d.expected_views_gain);d.control_video_id=d.control_video_id||null;await api("/api/memory",d);e.target.reset();await loadMemory()})};
async function loadMemory(){
 const rows=await api("/api/memory");
 $("memory").innerHTML=rows.map(({decision:d,evidence:e,changes=[]})=>`<article class="card"><p class="eyebrow">#${d.id} · ${esc(d.status)} · ${esc(d.design)}</p><h3>${esc(d.hypothesis)}</h3><p>Erwartet: +${num(d.expected_result.value)} Views in ${d.expected_result.horizon_hours} Stunden.</p><p class="muted">${["content_strategy","title_strategy","thumbnail_strategy","hook_strategy","audience"].map(k=>esc(d[k])).join(" · ")}</p><p>${esc(e.status)} · ${e.wins} Erfolge / ${e.failures} Fehlschläge in unabhängigen Vergleichen</p><p class="muted">Confidence P(Erfolgsrate &gt; 50 %): ${pct(e.confidence)} · 95%-Intervall der Erfolgsrate: ${pct(e.success_probability_interval[0])} – ${pct(e.success_probability_interval[1])}</p>
 ${d.actual_result?`<p>Tatsächlich: +${num(d.actual_result.views_gain)} Views · Abweichung: ${num(d.deviation)}</p><p>Vermutete Ursache: ${esc(d.suspected_cause)}</p><p>Nächste Hypothese: ${esc(d.next_hypothesis)}</p><form data-review="${d.id}"><label>Vermutete Ursache<textarea name="suspected_cause" minlength="3" required></textarea></label><label>Nächste Hypothese<textarea name="next_hypothesis" minlength="8" required></textarea></label><button>Interpretation ergänzen</button></form>`:""}
 ${changes.map(c=>`<p>Änderung ${esc(c.dimension)} · ${esc(c.applied_at)}: ${esc(c.before_value)} → ${esc(c.after_value)} · ${esc(c.rationale)}</p>`).join("")}
 ${d.status!=="draft"?`<form data-change="${d.id}"><label>Änderung<select name="dimension"><option value="title">Titel</option><option value="thumbnail">Thumbnail</option><option value="hook">Hook</option><option value="content">Content</option><option value="audience">Zielgruppe</option></select></label><label>Tatsächlich geändert am (lokale Zeit)<input type="datetime-local" name="applied_at" required></label><label>Vorher<textarea name="before_value" maxlength="2000" required></textarea></label><label>Nachher<textarea name="after_value" maxlength="2000" required></textarea></label><label>Begründung<textarea name="rationale" minlength="3" maxlength="2000" required></textarea></label><button>Änderung protokollieren</button></form>`:""}
 ${d.status==="draft"?`<form data-activate="${d.id}" class="inline"><label>Video-ID für Messbeginn<input name="video_id" required></label><button>Messung registrieren</button></form>`:""}</article>`).join("")||"<p class='muted'>Noch keine Entscheidungen gespeichert. Dokumentiere die erste Hypothese vor der Messung.</p>";
 document.querySelectorAll("[data-activate]").forEach(f=>f.onsubmit=e=>{e.preventDefault();guarded(async()=>{await api("/api/memory/"+f.dataset.activate+"/activate",Object.fromEntries(new FormData(f)));await loadMemory()})});
 document.querySelectorAll("[data-change]").forEach(f=>f.onsubmit=e=>{e.preventDefault();guarded(async()=>{const data=Object.fromEntries(new FormData(f));data.applied_at=new Date(data.applied_at).toISOString();await api("/api/memory/"+f.dataset.change+"/changes",data);await loadMemory()})});
 document.querySelectorAll("[data-review]").forEach(f=>f.onsubmit=e=>{e.preventDefault();guarded(async()=>{await api("/api/memory/"+f.dataset.review+"/reviews",Object.fromEntries(new FormData(f)));await loadMemory()})});
}
