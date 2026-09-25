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
async function manualSync(){
 const button=$("refresh");
 if(button.disabled)return;
 const label=button.textContent;
 button.disabled=true;button.textContent="Synchronisiere…";
 try{
  let failure=null;
  try{
   const result=await api("/api/sync",{});
   if(result.status==="deferred")failure=new Error(result.detail);
  }catch(error){failure=error}
  // Reload even after partial failure so the persisted import status is visible.
  try{await load()}catch(error){failure=failure?new Error(failure.message+" Dashboard: "+error.message):error}
  if(failure)throw failure;
 }finally{button.disabled=false;button.textContent=label}
}
const BACKFILL_MAX_ROUNDS=25;
function renderHealth(h){
 const node=$("healthBanner");
 if(!h||h.level==="ok"){node.hidden=true;node.textContent="";node.className="notice";return}
 node.hidden=false;
 node.className="notice "+(h.level==="critical"?"down":"");
 const c=h.checks||{};
 node.textContent=(h.level==="critical"?"ACHTUNG – Jobs laufen nicht durch: ":"Hinweis: ")+(h.warnings||[]).join(" · ")
  +` · Letzter Sync: ${c.last_sync_status||"—"} (Serie ohne ok: ${c.deferred_streak??0}) · Plan ${c.plan_age_hours==null?"—":num(c.plan_age_hours,0)+" h"} · Discovery ${c.discovery_age_hours==null?"—":num(c.discovery_age_hours,0)+" h"} alt`;
}
const STATE_LABELS={protect_momentum:"Momentum schützen",scale_opportunity:"Chance skalieren",needs_packaging_test:"Packaging testen",needs_retention_analysis:"Retention analysieren",needs_discovery:"Discovery verbessern",needs_distribution:"Verteilung fehlt – Distribution zuerst",revival_candidate:"Revival-Kandidat",observe:"Beobachten",paid_cooldown:"Paid-Cooldown",paid_excluded:"Aktuell Paid beeinflusst",insufficient_data:"Zu wenig Daten"};
const PAID_LABELS={organic:"Aktuell organisch",organic_with_paid_history:"Aktuell organisch · historisch Werbung",paid_cooldown:"Paid-Cooldown",paid_excluded:"Aktuell Paid beeinflusst"};
function paidCell(r){const p=r.paid||{};const s=r.paid_status||"organic";const extra=s==="paid_cooldown"||s==="paid_excluded"?` · noch ${p.days_until_clean??"?"} saubere Tage (${p.clean_days??0}/${p.required_clean_days||32})`:"";return `<span class="${s==="paid_excluded"?"down":s==="organic"?"up":""}">${esc(PAID_LABELS[s]||s)}</span><small>${esc(r.paid_note||"")}${esc(extra)}</small>`}
const ACTION_LABELS={protect_no_change:"Nichts ändern – Momentum schützen",test_title:"Titel testen",test_thumbnail:"Thumbnail testen",test_title_thumbnail:"Titel + Thumbnail testen",investigate_retention:"Retention untersuchen",improve_discovery:"Discovery verbessern",cross_promote:"Quer bewerben",create_followup_content:"Folgevideo planen",observe:"Beobachten",target_search_opportunity:"Suchintention gezielt bedienen",target_suggested_cluster:"Suggested-Cluster ansteuern",packaging_for_audience:"Packaging für Ziel-Audience",revive_existing_video:"Bestehendes Video reaktivieren",distribute_playlist_context:"Verteilung über Playlist/Endscreen erhöhen",probe_missing_evidence:"Fehlende Evidenz risikoarm beschaffen",link_from_own_video:"Aus eigenem Video verlinken (Endscreen/Infokarte)",place_in_existing_playlist:"In bestehende Playlist aufnehmen",create_playlist_context:"Thematische Playlist anlegen"};
const OUTCOME_LABELS={positive:"positiv",negative:"negativ",neutral:"neutral",inconclusive:"unklar"};
const stateClass=s=>s==="protect_momentum"||s==="scale_opportunity"||s==="revival_candidate"?"up":s==="paid_excluded"?"down":"";
function scoreText(s){return s==null?"—":num(s,0)}
function trackText(record,action){const r=record?.[action];return r?`${r.positive}+ / ${r.negative}− / ${r.neutral}= / ${r.inconclusive}?`:"noch keine"}
function renderTraffic(a){
 // Primaeres Ziel: zusaetzliche qualifizierte Views. Nur ausfuehrbare Acquisition-Aktionen, kein "beobachten".
 if(!a){$("trafficTitle").textContent="Noch keine Trafficquellen gefunden";return}
 const q=a.traffic_queue||[],run=a.running||[],res=a.results||[],board=a.scoreboard||{};
 $("trafficTitle").textContent=q.length?`Jetzt tun: ${q.length} Traffic-Aktion${q.length>1?"en":""}`:
  (run.length?"Alle gefundenen Quellen laufen bereits":"Keine ausführbare Trafficquelle gefunden");
 $("trafficMeta").textContent=`Flächen vom ${esc(a.day||"—")} · ${num(a.surfaces_found)} geprüfte Quellen`;
 const pools=a.pools||{};
 $("trafficGoal").textContent=a.goal||"";
 // Die Pool-Suche muss nachpruefbar sein: welcher Query, welche realen Treffer, welcher Filtergrund.
 const pq=(pools.queries||[]).map(x=>`<li><code>${esc(x.query)}</code> (${esc(x.kind)}, Quelle ${esc(x.source||"—")}): ${num(x.results)} Treffer, ${num(x.stored)} gespeichert${x.note?" – "+esc(x.note):""}${(x.titles||[]).length?`<br><small>${(x.titles||[]).map(esc).join(" · ")}</small>`:""}</li>`).join("");
 const pc=(pools.candidates||[]).map(c=>`<li>${c.kept?"✓":"✗"} <strong>${esc(c.title||"—")}</strong> <small>${esc(c.kind)} · ${esc(c.size||"")}${c.query?` · gefunden über „${esc(c.query)}“`:""}</small>${c.url?` <a href="${esc(c.url)}" rel="noreferrer noopener" target="_blank">öffnen</a>`:""}<br><small>${esc(c.reason||"noch nicht geprüft")}</small></li>`).join("");
 $("trafficPools").innerHTML=`<h3>Suche nach fremden Playlists und Kanälen</h3>`
  +(pools.searched?`<ul>${pq}</ul>`:`<p class="muted">${esc(pools.note||"In diesem Lauf wurde keine Pool-Suche ausgeführt.")}</p>`)
  +(pc?`<p><strong>Kandidaten und Filtergründe</strong> (${num(pools.kept)} aufgenommen, ${num(pools.rejected)} verworfen)</p><ul>${pc}</ul>`:"");
 $("trafficKpi").innerHTML=[["Zusätzliche Views (attribuiert)",num(board.attributed_views_delta),"aus gemessenen Quellen seit Start"],
  ["Laufende Traffic-Aktionen",num(board.running),"von dir bestätigt"],
  ["Ausgewertet",num(board.evaluated),"mit Quellen-Attribution"],
  ["Vorschläge offen",num(board.proposed),"warten auf Durchführung"]]
  .map(([l,v,s])=>`<article><span>${l}</span><strong>${v}</strong><small>${esc(s)}</small></article>`).join("");
 $("trafficQueue").innerHTML=q.map(e=>`<article class="queue-item"><h3>#${e.rank} ${esc(e.title)} — ${esc(e.action_label||e.action)}</h3>
  <p><strong>Trafficquelle:</strong> ${esc(e.surface||"—")}${e.surface_url?` <a href="${esc(e.surface_url)}" rel="noreferrer noopener" target="_blank">öffnen</a>`:""} <small>${esc(e.traffic_source||"")}${e.verified?" · erreichbar geprüft":""}</small></p>
  <p><strong>Warum:</strong> ${esc(e.why||"")}</p>
  <p><strong>Wirkmechanismus:</strong> ${esc(e.mechanism_status||"unbekannt")}${e.mechanism_note?" – "+esc(e.mechanism_note):""}${e.upgrade_rule?" <small>"+esc(e.upgrade_rule)+"</small>":""}</p>
  <p><strong>Aktion:</strong></p><ol>${(e.steps||[]).map(x=>"<li>"+esc(x)+"</li>").join("")}</ol>
  <p><strong>Erwarteter Mechanismus:</strong> ${esc(e.mechanism||"")}</p>
  <p><strong>Primärmetrik:</strong> ${esc(e.primary_metric||"")} · <strong>Messfenster:</strong> ${e.window_days} Tage (Auswertung ${esc(e.evaluate_after)})</p>
  <p><strong>Erfolg:</strong> ${esc(e.success_criterion||"")}<br><strong>Abbruch:</strong> ${esc(e.stop_criterion||"")}</p>
  <p><strong>Nicht gleichzeitig verändern:</strong> ${(e.do_not_change||[]).map(esc).join(", ")} <small>Hebel: ${esc(e.primary_lever||"")}</small></p>
  <p class="muted">${esc(e.rules||"")}</p>
  <p class="notice">${esc(e.note||"")}</p>
  <button class="secondary start-experiment" data-action-id="${e.action_id}" title="Maßnahme #${e.action_id}">${esc((e.confirm&&e.confirm.label)||"Als durchgeführt markieren")}</button><small>${esc((e.confirm&&e.confirm.effect)||"")}</small>
  </article>`).join("")||"<p class='muted'>Keine ausführbare Trafficquelle: siehe gesperrte Quellen und die Grenzen der Suche unten.</p>";
 $("trafficRunning").innerHTML=run.length?`<h3>Läuft – von dir bestätigt</h3><ul>${run.map(r=>`<li>${esc(r.title)}: ${esc(r.action_label||r.action)} über ${esc(r.surface||r.traffic_source)} – seit ${esc(r.started_day)}, Auswertung ${esc(r.evaluate_after)} (${esc(r.target_metric)})</li>`).join("")}</ul>`:"";
 $("trafficBlocked").innerHTML=(a.blocked||[]).length?`<h3>Gesperrt, weil nicht trennbar</h3><ul>${a.blocked.map(b=>`<li>${esc(b.title)} · ${esc(b.surface)} (${esc(b.traffic_source)}): ${esc(b.reason)}</li>`).join("")}</ul>`:"";
 $("trafficResults").innerHTML=res.length?`<h3>Ergebnisse je Quelle</h3><ul>${res.map(r=>`<li>${esc(r.title)} · ${esc(r.surface||r.traffic_source)}: <strong>${esc(OUTCOME_LABELS[r.outcome]||r.outcome||"offen")}</strong> ${r.attribution?`— ${num(r.attribution.views_before)} → ${num(r.attribution.views_after)} Views aus ${esc(r.attribution.source)} (${r.attribution.views_delta>=0?"+":""}${num(r.attribution.views_delta)})`:""}</li>`).join("")}</ul>`:"";
 const cap=a.capabilities||{};
 $("trafficSurfaces").innerHTML=(a.surfaces||[]).map(s=>`<p><strong>${esc(s.title)}</strong> (${esc(s.kind)}, ${esc(s.traffic_source)}) → ${esc(s.video_id||"")} · Potenzial ${scoreText(s.traffic_potential)} · erwartet ${num(s.expected_weekly_views,1)} Views/Woche<br><small>${esc(s.why||"")}</small></p>`).join("")
  +`<p><strong>Genutzte Quellen:</strong> ${(cap.used||[]).map(esc).join("; ")}</p><p><strong>Nicht genutzt:</strong> ${(cap.not_used||[]).map(esc).join("; ")}</p>`;
}
let startNotice = null;   // letzte Rueckmeldung zum Experiment-Start, direkt in der Karte sichtbar
function renderQueue(p){
 const notice=$("queueStart");
 notice.textContent=startNotice?startNotice.text:"";
 notice.className="notice"+(startNotice&&startNotice.failed?" down":"");
 notice.hidden=!startNotice;
 // JETZT TUN: was ein Mensch heute ausführt. YouTube bleibt read-only – nichts davon passiert automatisch.
 const q=(p&&p.queue)||[],running=(p&&p.running_experiments)||[],results=(p&&p.results)||[];
 $("queueTitle").textContent=q.length?`Jetzt tun: ${q.length} Experiment${q.length>1?"e":""}`:"Heute kein ausführbares Experiment";
 $("queueMeta").textContent=p?`Plan vom ${p.day||"—"} · höchstens ${p.queue_limit||3} gleichzeitig`:"";
 $("queueNote").textContent=(p&&p.queue_note)||"";
 $("queueList").innerHTML=q.map(e=>{
  const ev=e.evidence||{},b=e.baseline||{};
  return `<article class="queue-item"><h3>#${e.rank} ${esc(e.title)} — ${esc(ACTION_LABELS[e.action]||e.action)}</h3>
  <p><strong>Ziel:</strong> ${esc(e.objective||"—")}${e.audience?" · <strong>Audience/Kontext:</strong> "+esc(e.audience):""}</p>
  <p><strong>Warum:</strong> ${esc(e.why||"")}</p>
  <p><strong>Evidenz:</strong> ${esc(EVIDENCE_LABELS[ev.level]||"keine externe Chance")}${ev.demand_source?" · "+esc(evidenceText(ev)):""}${ev.uncertainty?" · Unsicherheit "+esc(ev.uncertainty):""}${ev.confidence?" · Confidence "+esc(ev.confidence):""}</p>
  ${(ev.family_labels||[]).length?`<ul class="muted">${ev.family_labels.map(f=>"<li>"+esc(f)+"</li>").join("")}</ul>`:""}
  ${ev.own_route?`<p class="muted">Belegte eigene Route: ${esc(ev.own_route.label)} (${num((ev.own_route.share||0)*100,0)} % von ${num(ev.own_route.views_7d)} Views)</p>`:""}
  ${(ev.missing||[]).length?`<p><strong>Fehlende Evidenz:</strong> ${ev.missing.map(esc).join("; ")}</p>`:""}
  <ol>${(e.steps||[]).map(x=>"<li>"+esc(x)+"</li>").join("")}</ol>
  <p><strong>Baseline:</strong> ${num(b.views_7d)} Views/7 T · ${b.impressions_7d==null?"Impressions unbekannt":num(b.impressions_7d)+" Impressions"} · CTR ${b.ctr_7d==null?"—":num(b.ctr_7d*100,1)+" %"} <small>${esc(b.note||"")}</small></p>
  <p><strong>Erwartetes Signal:</strong> ${esc(e.expected_signal||"")} · <strong>Messfenster:</strong> ${e.window_days} Tage (ab ${esc(e.measure_from)}, Auswertung ${esc(e.evaluate_after)})</p>
  <p><strong>Erfolg:</strong> ${esc(e.success_criterion||"")}</p>
  <p><strong>Abbruch:</strong> ${esc(e.stop_criterion||"")}</p>
  <p><strong>Primärer Hebel:</strong> ${esc(e.primary_lever||"—")} <small>${esc(e.one_lever_note||"")}</small></p>
  ${e.requires?`<p><strong>Benötigte Ressource:</strong> ${esc(e.requires.kind==="source_video"?"eigenes Quellvideo":e.requires.kind==="playlist"?"bestehende Playlist":"keine")}${e.requires.verified?" · belegt":" · nicht belegt"}${(e.requires.named||[]).length?": "+e.requires.named.map(esc).join(", "):""}${e.requires.evidence?" <small>"+esc(e.requires.evidence)+"</small>":""}</p>`:""}
  ${e.choices?`<p class="notice"><strong>Deine Auswahl nötig:</strong> ${esc(e.choices.question)}<ul>${(e.choices.options||[]).map(o=>`<li>${esc(o.title||o.id)} — ${esc(o.evidence||(o.item_count!=null?o.item_count+" Videos":""))}</li>`).join("")}</ul>Das System wählt hier bewusst nicht für dich.</p>`:""}
  ${(e.deferred_levers||[]).length?`<p class="muted">Bewusst nicht in diesem Experiment (eigene Experimente): ${e.deferred_levers.map(esc).join("; ")}</p>`:""}
  <p><strong>Externer Kontext:</strong> ${esc(e.context_status||"kein externer Kontext")}${e.context_reason?" <small>"+esc(e.context_reason)+"</small>":""}</p>
  <p><strong>Nicht verändern:</strong> ${(e.do_not_change||[]).map(esc).join(", ")}</p>
  <p class="notice">${esc(e.note||"")}</p>
  ${e.action_id?`<button class="secondary start-experiment" data-action-id="${e.action_id}" title="Maßnahme #${e.action_id}">${esc((e.confirm&&e.confirm.label)||"Als durchgeführt markieren – Experiment starten")}</button><small>${esc((e.confirm&&e.confirm.effect)||"")}</small>`:""}
  </article>`}).join("")||"<p class='muted'>Kein Vorschlag: geschützte oder laufende Videos, oder die Evidenz reicht nicht für ein konkretes Experiment.</p>";
 $("queueRunning").innerHTML=running.length?`<h3>Läuft – von dir als durchgeführt bestätigt (nicht anfassen)</h3><ul>${running.map(r=>`<li>${esc(r.title)}: ${esc(ACTION_LABELS[r.action]||r.action)} – seit ${esc(r.held_since)}, Auswertung ${esc(r.evaluate_after)} (${esc(r.target_metric)})</li>`).join("")}</ul>`:"";
 $("queueNotTestable").innerHTML=((p&&p.not_testable)||[]).length?`<h3>Derzeit nicht sinnvoll testbar</h3><ul>${p.not_testable.map(x=>`<li>${esc(x.title)}: ${esc(x.reason)}</li>`).join("")}</ul>`:"";
 $("queueResults").innerHTML=results.length?`<h3>Ergebnisse abgeschlossener Experimente</h3><ul>${results.map(r=>`<li>${esc(r.created_day)} ${esc(ACTION_LABELS[r.action]||r.action)} (${esc(r.video_id)}): <strong>${esc(OUTCOME_LABELS[r.outcome]||r.outcome||"offen")}</strong>${r.metric?` · ${esc(r.metric)} ${num(r.before)} → ${num(r.after)}${r.relative_change==null?"":" ("+(r.relative_change>=0?"+":"")+num(r.relative_change*100,0)+" %)"}`:""}${r.reason?" · "+esc(r.reason):""}<small>${esc(r.note||"")}</small></li>`).join("")}</ul>`:"";
}
function renderGrowth(g){
 renderQueue(g&&g.plan);
 const p=g?.plan;
 if(!p){$("growthTitle").textContent="Noch kein Growth Plan";$("growthDay").textContent="";$("growthTop").innerHTML="";$("growthDetail").innerHTML="<p class='muted'>Der nächste stündliche Sync erzeugt den ersten Plan.</p>";$("growthRanking").innerHTML="";return}
 const top=(p.ranking||[]).find(r=>r.video_id===p.priority_video_id)||{};
 const protectedText=(p.protected||[]).map(x=>esc(x.title)+" ("+esc(ACTION_LABELS[x.action]||x.action)+")").join(", ");
 $("growthTitle").textContent=p.active_status==="active"?"Aktive Growth-Priorität #1: "+(p.priority_title||p.priority_video_id):p.status==="no_active_action"?"Keine aktive Maßnahme empfohlen":"Noch nicht genug Daten für eine Priorität";
 $("growthDay").textContent="Plan vom "+esc(p.day)+" · Confidence "+esc(p.confidence||"insufficient_data")+(protectedText?" · Momentum schützen: "+protectedText:"");
 $("growthTop").innerHTML=p.active_status!=="active"?`<article><span>Aktive Maßnahme</span><strong>keine</strong><small>${esc(p.why_priority||"")}</small></article>`+(p.momentum_top?`<article><span>Momentum-Spitze</span><strong>${esc(p.momentum_top.title)}</strong><small>${esc(STATE_LABELS[p.momentum_top.state]||p.momentum_top.state)} · Score ${scoreText(p.momentum_top.opportunity_score)}</small></article>`:""):[["Aktive Growth-Priorität",scoreText(top.active_priority_score),"intern + extern (V6), relativ"],["Growth Opportunity",scoreText(top.opportunity_score),"Momentum-Rang "+(top.momentum_rank||"—")],["Viewer Acquisition",scoreText(top.viewer_score),"neue Zuschauer"],["Subscriber Opportunity",scoreText(top.subscriber_score),"Abo-Umwandlung"],["Zustand",esc(STATE_LABELS[top.state]||top.state||"—"),top.breakout?"Breakout-Signale aktiv":"kein Breakout"],["Werbung",esc(PAID_LABELS[top.paid_status]||"—"),esc(top.paid_note||"")]].map(([l,v,s])=>`<article><span>${l}</span><strong>${v}</strong><small>${s}</small></article>`).join("");
 const ext=p.external_signals||{},int=p.internal_signals||{};
 $("growthDetail").innerHTML=`<div class="detail-grid"><div><p><strong>Empfohlene Aktion:</strong> ${p.action?esc(ACTION_LABELS[p.action]||p.action):"Keine aktive Maßnahme empfohlen"}</p><p><strong>Warum:</strong> ${esc(p.why)}</p><p class="muted">${esc(p.why_priority)}</p>${protectedText?`<p><strong>Momentum schützen (keine Änderung):</strong> ${protectedText}</p>`:""}<p><strong>Interne Signale:</strong> ${esc(STATE_LABELS[int.state]||int.state||"—")} · Regime ${esc(int.regime||"—")} · Opportunity ${scoreText(int.opportunity_score)} · ${esc(PAID_LABELS[int.paid_status]||"")}</p><p><strong>Externe Nachfrage-Signale:</strong> ${ext.available?`${esc(KIND_LABELS[ext.kind]||ext.kind)} „${esc(ext.key)}“ · ${esc(GAP_LABELS[ext.gap]||ext.gap)} · Score ${scoreText(ext.score)} · ${esc(evidenceText(ext))}`:"keine externe Chance mit ausreichender Evidenz"}</p><p><strong>Kombinierte Entscheidung:</strong> ${esc(p.combined_decision||"—")}</p><p><strong>Ziel heute:</strong> ${esc(p.objective)} · Viewer-Fokus: ${esc(p.viewer_focus||"—")} · Subscriber-Fokus: ${esc(p.subscriber_focus||"—")}</p></div><div><p><strong>Nicht verändern:</strong> ${(p.do_not_change||[]).map(esc).join(", ")}</p><p><strong>Entscheidende Kennzahl:</strong> ${esc(p.success_metric)} – ${esc(p.success_criterion)}</p><p><strong>Messfenster:</strong> ${p.window_days} Tage · nächste Auswertung ${esc(p.next_evaluation)}</p>${(top.notes||[]).map(n=>"<p class='muted'>"+esc(n)+"</p>").join("")}</div></div>`;
 $("growthRanking").innerHTML=(p.ranking||[]).map(r=>`<tr><td>${r.momentum_rank||r.priority}</td><td>${r.active_rank?"<span class='up'>#"+r.active_rank+"</span><small>Score "+scoreText(r.active_priority_score)+"</small>":"—<small>"+esc(r.ineligible_reason||"")+"</small>"}</td><td>${esc(r.title)}<small>${esc(r.video_id)}</small></td><td>${scoreText(r.opportunity_score)}</td><td>${scoreText(r.viewer_score)}</td><td>${scoreText(r.subscriber_score)}</td><td class="${stateClass(r.state)}">${esc(STATE_LABELS[r.state]||r.state)}${r.revival?"<small>"+r.revival_signals.map(esc).join(" · ")+"</small>":""}</td><td>${r.breakout?"<span class='up'>ja</span>":esc(r.regime)}</td><td>${paidCell(r)}</td><td>${esc(ACTION_LABELS[r.action]||r.action)}<small>${esc(r.reason)}</small></td><td>${r.window_days} Tage<small>${esc(r.next_evaluation)}${r.held_since?" · seit "+esc(r.held_since):""}</small></td><td>${esc(trackText(p.track_record,r.action))}</td></tr>`).join("");
}
const GAP_LABELS={existing_video_opportunity:"Bestehendes Video sichtbar machen",packaging_opportunity:"Packaging für diese Audience",search_opportunity:"Search-Chance",suggested_opportunity:"Suggested/Browse-Chance",followup_content_opportunity:"Folgevideo-Chance",insufficient_evidence:"Zu wenig Evidenz"};
const KIND_LABELS={search:"Suchintention",suggested:"Nachbarvideo",cluster:"Audience-Cluster"};
const evidenceText=e=>e?.demand_source==="own_analytics"?"eigene Analytics (real)":e?.demand_source==="own_traffic_plus_public_proxy"?"eigene Traffic-Quellen + öffentlicher Proxy":"öffentlicher Proxy";
const EVIDENCE_LABELS={own_analytics:"belegt (eigene Analytics)",multi_signal_proxy:"mehrere unabhängige Signale",weak_proxy:"nur Proxy – Hypothese",probe:"nur Proxy – Hypothese",none:"keine Evidenz"};
const evidenceBadge=e=>`${esc(EVIDENCE_LABELS[e?.evidence_level]||evidenceText(e))}${e?.score_capped?" · Score gedeckelt":""}${e?.actionable?"":" · nicht handlungsauslösend"}`;
function trendText(t){const s=(t||[]).map(x=>x.score).filter(x=>x!=null);if(s.length<2)return "neu";const d=s.at(-1)-s[0];return (d>=0?"+":"")+num(d,0)+" über "+s.length+" Tage"}
function renderDiscovery(d){
 if(!d){$("discoveryStatus").textContent="Kein Discovery-Status.";return}
 const b=d.best;
 $("discoveryTitle").textContent=b?`Beste externe Chance: „${b.key}“ → ${b.video_title||b.video_id||"kein passendes Video"}`:"Noch keine externe Chance ermittelt";
 const run=d.last_run;
 $("discoveryStatus").textContent=(run?`Letzter Discovery-Lauf ${run.day} · ${run.status} · ${num(run.units_used)} API-Einheiten${run.issues?.length?" · "+run.issues.join(" / "):""}`:"Noch kein Discovery-Lauf; der nächste Sync startet ihn (einmal täglich).")+` · Quota heute ${num(d.quota?.units_used)} / ${num(d.quota?.daily_limit)} Einheiten · ${d.insufficient||0} Kandidaten ohne ausreichende Evidenz · ${num(d.actionable)} belegt/handlungsauslösend (Proxy-Deckel ${num(d.evidence_policy?.proxy_score_cap)})`;
 $("discoveryTop").innerHTML=b?[["Ziel-Audience / Topic",esc(b.evidence?.title||b.key),esc(KIND_LABELS[b.kind]||b.kind)],["Passendes Sealand-Video",esc(b.video_title||"—"),esc(GAP_LABELS[b.gap]||b.gap)],["Search / Suggested",`${scoreText(b.scores?.search_opportunity_score)} / ${scoreText(b.scores?.suggested_opportunity_score)}`,"relative Prioritäten"],["Subscriber-Fit",scoreText(b.scores?.subscriber_fit_score),esc(evidenceText(b.evidence))]].map(([l,v,s])=>`<article><span>${l}</span><strong>${v}</strong><small>${s}</small></article>`).join(""):"";
 const pv=d.per_video||{};
 $("discoveryDetail").innerHTML=Object.entries(pv).map(([vid,o])=>`<p><strong>${esc(state?.videos?.find(v=>v.id===vid)?.title||vid)}:</strong> ${o?`${esc(KIND_LABELS[o.kind]||o.kind)} „${esc(o.key)}“ · ${esc(GAP_LABELS[o.gap]||o.gap)} · externe Audience ${scoreText(o.score)} · Nachfrage: ${esc(evidenceText(o))}${o.shared_tokens?.length?" · gemeinsame Themen: "+o.shared_tokens.map(esc).join(", "):""}`:"noch keine externe Chance mit ausreichender Evidenz"}</p>`).join("")+(b?`<p class="muted">Unsicherheit: ${esc(b.evidence?.uncertainty||"—")} · fehlende Komponenten: ${(b.evidence?.missing||[]).map(esc).join(", ")||"keine"}</p>`:"");
 $("discoveryTable").innerHTML=(d.top||[]).map(o=>`<tr><td>${esc(o.evidence?.title||o.key)}<small>${esc(o.key)}</small></td><td>${esc(KIND_LABELS[o.kind]||o.kind)}<small>${esc(GAP_LABELS[o.gap]||o.gap)}</small></td><td>${esc(o.video_title||"—")}</td><td>${scoreText(o.scores?.external_audience_score)}</td><td>${scoreText(o.scores?.search_opportunity_score)}</td><td>${scoreText(o.scores?.suggested_opportunity_score)}</td><td>${scoreText(o.scores?.subscriber_fit_score)}</td><td>${evidenceBadge(o.evidence)}<small>${o.evidence?.own_search_views_90d!=null?num(o.evidence.own_search_views_90d)+" eigene Such-Views":o.evidence?.own_suggested_views_90d!=null?num(o.evidence.own_suggested_views_90d)+" eigene Suggested-Views":""}${o.outcome?" · "+esc(OUTCOME_LABELS[o.outcome]||o.outcome):""}</small></td><td>${esc(trendText(o.trend))}</td></tr>`).join("")||"<tr><td colspan='9' class='muted'>Noch keine Chancen mit ausreichender Evidenz.</td></tr>";
 const mem=Object.entries(d.memory||{}).map(([k,v])=>`${esc(KIND_LABELS[k]||k)}: ${v.positive}+ / ${v.negative}− / ${v.neutral}= / ${v.inconclusive}? (Gewicht ${num(v.weight,2)})`).join(" · ")||"noch keine ausgewerteten Chancen";
 $("discoverySources").innerHTML=(d.capabilities||[]).map(c=>`<p><strong>${esc(c.source)}</strong> – ${esc(c.status)}<br>${esc(c.note)}</p>`).join("")+`<p>Discovery-Gedächtnis (28-Tage-Auswertung): ${mem}</p>`+(d.limits||[]).map(x=>"<p>"+esc(x)+"</p>").join("");
}
async function discoveryRun(){
 const button=$("discoveryRun");
 if(button.disabled)return;
 const label=button.textContent;
 button.disabled=true;button.textContent="Discovery läuft…";
 try{
  let failure=null;
  try{await api("/api/discovery/run",{})}catch(error){failure=error}
  try{await load()}catch(error){failure=failure?new Error(failure.message+" Dashboard: "+error.message):error}
  if(failure)throw failure;
 }finally{button.disabled=false;button.textContent=label}
}
function growthCard(g){
 if(!g||!g.opportunity)return "<div class='card'><h3>Growth Engine (V5)</h3><p class='muted'>Noch keine Bewertung.</p></div>";
 const comp=s=>(s.components||[]).map(c=>`<tr><td>${esc(c.name)}</td><td>${c.available?num(c.signal,2):"fehlt"}</td><td>${c.weight}</td><td>${c.value==null?"—":num(c.value,3)}</td><td>${c.reference==null?"—":num(c.reference,3)}</td></tr>`).join("");
 const table=(label,s)=>`<h3>${label}: ${scoreText(s.score)}</h3><p class="muted">${esc(s.reason)}</p><div class="table-scroll"><table><thead><tr><th>Komponente</th><th>Signal</th><th>Gewicht</th><th>Wert</th><th>Referenz</th></tr></thead><tbody>${comp(s)}</tbody></table></div>`;
 const acts=(g.actions||[]).map(a=>`<p>${esc(a.created_day)} · ${esc(ACTION_LABELS[a.action]||a.action)} · ${esc(a.status)}${a.outcome?" · "+esc(OUTCOME_LABELS[a.outcome]||a.outcome):""}${a.evaluation?.detail?.relative_change!=null?" · "+esc(a.evaluation.detail.metric)+" "+num(a.evaluation.detail.relative_change*100,1)+" % (beobachtet, nicht kausal)":""}<br><small>Ziel ${esc(a.target_metric)} · ${a.window_days} Tage · Erfolg: ${esc(a.payload?.success_criterion||"")} · Stop: ${esc(a.payload?.stop_criterion||"")}</small></p>`).join("")||"<p class='muted'>Noch keine Aktionen.</p>";
 const ext=(g.actions||[]).find(a=>a.status==="pending")?.payload?.audience;
 return `<div class="card"><p class="eyebrow">GROWTH ENGINE · V5</p>${ext?`<p><strong>Ziel-Audience (V6):</strong> ${esc(ext.target||"")} · ${esc(KIND_LABELS[ext.kind]||ext.kind)} · ${esc(GAP_LABELS[ext.gap]||ext.gap)} · Score ${scoreText(ext.score)} · ${esc(evidenceText(ext))}</p>`:""}<h3>${esc(STATE_LABELS[g.state]||g.state)} → ${esc(ACTION_LABELS[g.action]||g.action)}</h3><p class="muted">Stand ${esc(g.day)} · Revival: ${g.revival?.candidate?"Kandidat ("+(g.revival.signals||[]).map(esc).join(", ")+")":esc(g.revival?.reason||"nein")} · Live-Momentum V2: ${g.momentum?.score==null?"—":num(g.momentum.score,1)}</p><p class="muted">Werbung (aktuell berechnet): ${esc(PAID_LABELS[g.paid_live?.status]||PAID_LABELS[g.momentum?.paid?.status]||"—")}${g.paid_live&&g.paid_stored&&g.paid_live.status!==g.paid_stored.status?` (gespeichert: ${esc(PAID_LABELS[g.paid_stored.status]||g.paid_stored.status)})`:""} · Werbetage gesamt ${num((g.paid_live||g.momentum?.paid)?.paid_days_total)} · letzter Werbetag ${esc((g.paid_live||g.momentum?.paid)?.last_paid_day||"—")} · sauberes Fenster ${num((g.paid_live||g.momentum?.paid)?.clean_days)} / ${num((g.paid_live||g.momentum?.paid)?.required_clean_days)} Tage · organischer Peak ${g.revival?.peak?.peak_velocity==null?"—":num(g.revival.peak.peak_velocity,1)+" Views/Tag (nur werbefreie Wochen)"}${g.revival?.peak?.paid_peak_velocity!=null?" · Peak mit Werbung "+num(g.revival.peak.paid_peak_velocity,1)+" (nicht als Baseline verwendet)":""}</p>${table("Growth Opportunity",g.opportunity)}${table("Viewer Acquisition",g.viewer)}${table("Subscriber Opportunity",g.subscriber)}<h3>Aktionen und Auswertung</h3>${acts}</div>`;
}
const REGIME_LABELS={declining:"Rückläufig",stable:"Stabil",growing:"Wachsend",accelerating:"Beschleunigend",breakout_candidate:"Breakout-Kandidat",breakout:"Breakout",paid_excluded:"Werbung",insufficient_data:"Zu wenig Daten"};
const regimeClass=r=>r==="breakout"||r==="breakout_candidate"||r==="accelerating"||r==="growing"?"up":r==="declining"?"down":"";
function v4For(id){return state?.learning_v4?.videos?.[id]||{regime:"insufficient_data",recommendation:null,forecasts:[]}}
function forecastCell(v4){
 const p=(v4.forecasts||[]).find(f=>f.horizon_hours===168);
 if(!p)return "—";
 const range=p.lower_views!=null?`<small>${num(p.lower_views)} – ${num(p.upper_views)} · empirisch 80 %</small>`:"<small>Szenario, unkalibriert</small>";
 return `${num(p.predicted_views)}${range}<small>${esc(p.model)} · Origin ${esc(p.origin_day)}</small>`;
}
function modelCell(bt){
 if(!bt)return "insufficient_data";
 const b=bt.models?.[bt.best_baseline]||{},m=bt.models?.[bt.champion]||{};
 return `${esc(bt.champion)}${bt.accepted?" ✓":""}<small>MAE ${num(m.mae)} vs Baseline ${num(b.mae)} · zeitlich, ${num(bt.n_videos)} Videos · neue Videos: ${bt.cross_video?.status==="backtested"?"geprüft":"unzureichend"}</small>`;
}
function renderLearning(l){
 if(!l){$("learningStatus").textContent="Kein Lernstatus verfügbar.";return}
 const d=l.dataset;
 const sz=d?.sample_sizes?.["168"]||{};
 $("learningStatus").textContent=d?`Datensatz ${d.signature.slice(0,12)} · gebaut ${new Date(d.built_at).toLocaleString("de-CH")} · 7-Tage-Datensatz: n_rows ${num(sz.n_rows)} (korrelierte Tageszeilen) · n_origins ${num(sz.n_origins)} · n_videos ${num(sz.n_videos)} · größtes Video ${sz.largest_video_share==null?"—":num(sz.largest_video_share*100)+" %"} der Zeilen · Baseline: ${d.baselines.status} (${num(d.baselines.n_rows)} organische Kanaltage, ${esc(d.baselines.weighting||"pooled")})`:"Noch kein historischer Datensatz gebaut; der nächste Sync oder ein Lernlauf erzeugt ihn.";
 const g=l.generalization||{};
 $("learningScope").textContent=`Zeitlich validiert auf bestehenden Videos (Walk-forward, ${num(g.n_videos)} Videos) · Generalisierung auf neue Videos: ${g.status==="validated"?"validiert":"unzureichende Daten ("+num(g.n_videos)+" von mindestens "+num(g.required_videos)+" Videos)"} · Confidence bleibt unter ${num(g.required_videos)} Videos höchstens „low“, unabhängig von der Zeilenzahl.`;
 $("learningTable").innerHTML=["24","168","720"].map(h=>{const bt=l.backtests?.[h],sc=l.scorecard?.[h]||{};if(!bt)return `<tr><td>${h}h</td><td colspan="7">insufficient_data</td></tr>`;const b=bt.models?.[bt.best_baseline]||{},m=bt.models?.[bt.champion]||{},cv=bt.cross_video||{};return `<tr><td>${h==="24"?"24 Stunden":h==="168"?"7 Tage":"30 Tage"}</td><td>${esc(bt.champion)}${bt.accepted?" <span class='up'>zeitlich validiert</span>":" <small>Baseline gewinnt</small>"}</td><td>${num(b.mae,1)} Views<small>RMSE ${num(b.rmse,1)}</small></td><td>${num(m.mae,1)} Views<small>balanciert ${num(m.mae_video_balanced,1)} · RMSE ${num(m.rmse,1)}</small></td><td>${num(bt.n_rows)} · ${num(bt.n_origins)} · ${num(bt.n_videos)} · ${bt.folds}</td><td>${esc(bt.residual_quantiles?.kind||"—")}<small>n=${num(bt.residual_quantiles?.n)}</small></td><td>${sc.never_validated?"<span class='down'>noch nie live validiert</span>":num(sc.n)+" · "+(sc.model_mae==null?"—":num(sc.model_mae)+" / "+num(sc.baseline_mae)+" Views")}${sc.live_fallback?" <span class='down'>Fallback</span>":""}</td><td>${cv.status==="backtested"?(cv.accepted?"<span class='up'>validiert</span>":"Baseline gewinnt"):"unzureichende Daten"}<small>${num(cv.n_videos)}/${num(cv.required_videos)} Videos</small></td></tr>`}).join("");
 const signals=(l.backtests?.["168"]?.signals||[]).filter(s=>s.kind==="spearman_with_target_ratio").sort((a,b)=>b.value-a.value);
 const fmt=s=>`${esc(s.feature)} (${num(s.value,2)}, n=${s.n})`;
 $("learningSignals").textContent=signals.length?`Beobachtete Korrelationen mit relativem 7-Tage-Wachstum (Spearman, nicht kausal) – positiv: ${signals.slice(0,3).map(fmt).join(", ")} · negativ: ${signals.slice(-3).reverse().map(fmt).join(", ")}`:"Noch keine belastbaren Signal-Korrelationen (n < 30).";
 const bo=l.backtests?.["168"]?.breakout;
 $("learningAudit").innerHTML=(d?`<p>Breakout-Kalibrierung: ${esc(bo?.status||"insufficient_data")}${bo?.brier_model!=null?` · Brier ${num(bo.brier_model,3)} vs Basisrate ${num(bo.brier_base_rate,3)}`:""} · positive Fälle: ${num(bo?.positives)}</p><p>Ausschlüsse (7d): ${esc(JSON.stringify(d.exclusions["168"]||{}))}</p>`+d.audit.videos.map(v=>`<p>${esc(v.video_id)}: ${esc(v.first_day)} – ${esc(v.last_day)} · ${num(v.analytics_days)} Tage · ${num(v.paid_days)} Werbetage · ${num(v.retention_windows)} Retention-Monate · ${num(v.reach_days)} CTR-Tage</p>`).join("")+`<p>Ausgeschlossene Quellen: ${d.audit.excluded_feature_sources.map(esc).join("; ")}</p>`:"")+(l.limits||[]).map(x=>"<p>"+esc(x)+"</p>").join("");
}
async function learningRun(){
 const button=$("learningRun");
 if(button.disabled)return;
 const label=button.textContent;
 button.disabled=true;button.textContent="Lernlauf läuft…";
 try{
  let failure=null;
  try{await api("/api/learning/rebuild",{})}catch(error){failure=error}
  try{await load()}catch(error){failure=failure?new Error(failure.message+" Dashboard: "+error.message):error}
  if(failure)throw failure;
 }finally{button.disabled=false;button.textContent=label}
}
const day=x=>x==null?"—":String(x);
function backfillText(b){
 if(!b)return "Kein Backfill-Status verfügbar.";
 const run=b.last_run;
 const stages=Object.entries(b.stages||{}).map(([k,v])=>`${k} ${v.complete}/${b.videos} fertig`+(v.error?` · ${v.error} Fehler`:"")+(v.partial?` · ${v.partial} unterbrochen`:"")).join(" · ");
 const last=run?"Letzter Backfill: "+new Date(run.finished_at||run.started_at).toLocaleString("de-CH")+" · "+run.status+(run.issues?.length?" · "+run.issues.join(" / "):""):"Noch kein Backfill gestartet.";
 return last+(stages?" — "+stages:"");
}
function coverageText(c){
 if(!c)return "";
 return `Tageswerte ${day(c.daily_first)} bis ${day(c.daily_last)} (${num(c.daily_rows)} Zeilen) · Traffic-Quellen ${day(c.traffic_first)} bis ${day(c.traffic_last)} (${num(c.traffic_days)} Videotage, davon ${num(c.paid_days)} mit Werbetraffic) · Retention ${num(c.retention_windows)} Monatsfenster · Impressionen/CTR ${day(c.reach_first)} bis ${day(c.reach_last)} (${num(c.reach_days)} Videotage)`;
}
function renderBackfill(b){
 $("backfillStatus").textContent=backfillText(b);
 $("backfillCoverage").textContent=coverageText(b?.coverage);
 $("backfillLimits").innerHTML=(b?.limits||[]).map(l=>"<li>"+esc(l)+"</li>").join("");
}
async function backfill(){
 const button=$("backfillRun");
 if(button.disabled)return;
 const label=button.textContent;
 button.disabled=true;
 try{
  let failure=null,status="deferred",rounds=0;
  try{
   // Each round is one bounded server run; persisted cursors make continuation safe.
   while(status==="deferred"&&rounds<BACKFILL_MAX_ROUNDS){
    rounds++;button.textContent="Backfill läuft… (Runde "+rounds+")";
    const result=await api("/api/backfill",{});
    status=result.status;
    if(status!=="ok"&&status!=="deferred")failure=new Error(result.issues?.join(" / ")||status);
   }
   if(status==="deferred")failure=new Error("Backfill noch nicht abgeschlossen. Erneut starten, um fortzusetzen.");
  }catch(error){failure=error}
  try{await load()}catch(error){failure=failure?new Error(failure.message+" Dashboard: "+error.message):error}
  if(failure)throw failure;
 }finally{button.disabled=false;button.textContent=label}
}
function strategyCard(s){
 const r=s?.recommendation;
 if(!r)return "<div class='card'><h3>Strategie (V4)</h3><p class='muted'>Noch keine Empfehlung; der nächste Sync oder Lernlauf erzeugt sie.</p></div>";
 const list=(rows,f)=>rows.length?"<ul>"+rows.map(x=>"<li>"+f(x)+"</li>").join("")+"</ul>":"<p class='muted'>—</p>";
 const sig=x=>`${esc(x.signal)}: ${num(x.value,3)} vs Kanalmedian ${num(x.channel_median,3)} (n=${x.n})`;
 const fc=(s.forecasts||[]).map(p=>`<article><span>${p.horizon_hours===24?"1 Tag":p.horizon_hours===168?"7 Tage":"30 Tage"}</span><strong>${num(p.predicted_views)}</strong><small>${p.lower_views!=null?num(p.lower_views)+" – "+num(p.upper_views):"Szenario"}</small><p class="muted">${esc(p.model)} · Baseline ${num(p.baseline_views)}${p.actual_views!=null?"<br>Tatsächlich "+num(p.actual_views)+" · "+esc(p.eligibility||""):""}</p></article>`).join("");
 return `<div class="card"><p class="eyebrow">STRATEGIE · V4</p><h3>${esc(r.label)} <small class="muted">${esc(r.regime)} · Confidence ${esc(r.confidence.level)} (n_rows ${num(r.confidence.n_rows)}, n_origins ${num(r.confidence.n_origins)}, n_videos ${r.confidence.n_videos}${r.confidence.backtest_accepted?", zeitlich validiertes Modell":""})</small></h3>
 <p><strong>Was passiert:</strong> ${esc(r.what)}</p><p class="muted">${esc(r.confidence.reason)}</p><p class="notice">Zeitlich validiert auf bestehenden Videos · Generalisierung auf neue Videos: ${r.generalization?.status==="validated"?"validiert":"unzureichende Daten ("+num(r.generalization?.n_videos)+" von "+num(r.generalization?.required_videos)+" Videos)"}</p>
 <div class="detail-grid"><div><h3>Warum es passieren könnte</h3>${list(r.why,esc)}<h3>Dafür</h3>${list(r.for,sig)}<h3>Dagegen</h3>${list(r.against,sig)}</div>
 <div><h3>Als Nächstes testen/ändern</h3>${list(r.next,x=>"<strong>"+esc(x.action)+"</strong>: "+esc(x.detail))}<h3>Ausdrücklich NICHT ändern</h3>${list(r.do_not_change,esc)}</div></div>
 <h3>Analytics-Prognosen (Tageswerte, Lag beachtet)</h3><div class="forecast">${fc||"<p class='muted'>Noch keine Analytics-Prognose.</p>"}</div>
 <p class="muted">100k: ${esc(r.targets["100000"].status)} · 1M: ${esc(r.targets["1000000"].status)} – ${esc(r.targets["100000"].reason||"")}</p>
 <h3>Experimente</h3>${list(r.experiments||[],e=>`#${e.decision_id} ${esc(e.hypothesis)} · ${esc(e.effect_status)}${e.effect?` · vorher ${num(e.effect.before_mean_daily,1)}/Tag → nachher ${num(e.effect.after_mean_daily,1)}/Tag (${num(e.effect.relative_change*100,1)} %, beobachtet, nicht kausal)`:""}${e.conflicting_evidence?" · <span class='down'>widersprüchliche Ergebnisse für diese Strategie</span>":""}`)}</div>`;
}
$("loginForm").addEventListener("submit",e=>{e.preventDefault();token=$("token").value;guarded(load)});
$("refresh").onclick=()=>guarded(manualSync);
$("backfillRun").onclick=()=>guarded(backfill);
$("learningRun").onclick=()=>guarded(learningRun);
$("discoveryRun").onclick=()=>guarded(discoveryRun);
async function acquisitionRun(){
 // Erst Discovery (fremde Playlists/Kanaele, kostet Quota), dann die Auswertung zu Flaechen und Aktionen.
 const button=$("acquisitionRun");
 if(button.disabled)return;
 const label=button.textContent;
 button.disabled=true;button.textContent="Suche läuft…";
 try{
  let failure=null;
  try{await api("/api/discovery/run",{})}catch(error){failure=error}
  try{await api("/api/acquisition/run",{})}catch(error){failure=failure||error}
  try{await load()}catch(error){failure=failure||error}
  if(failure)throw failure;
 }finally{button.disabled=false;button.textContent=label}
}
$("acquisitionRun").onclick=()=>guarded(acquisitionRun);
async function startExperiment(button){
 // Bestaetigung der Durchfuehrung durch den Menschen - das System aendert nichts auf YouTube.
 if(button.disabled)return;
 const label=button.textContent, id=button.dataset.actionId;
 button.disabled=true;button.textContent="Wird gestartet…";
 try{
  let result;
  try{
   result=await api(`/api/growth/actions/${id}/start`,{});
  }catch(error){
   // Kein stilles Fehlschlagen: die Ursache steht direkt an der Karte.
   startNotice={failed:true,text:`Start fehlgeschlagen (Maßnahme #${id}): ${error.message}`};
   renderQueue(state&&state.growth_v5?state.growth_v5.plan:null);
   throw error;
  }
  startNotice={failed:result.status!=="running",
   text:result.status==="running"
    ?`Bestätigt: Maßnahme #${result.id} läuft seit ${result.started_day}, Auswertung ${result.evaluate_after}. Baseline eingefroren.`
    :`Unerwarteter Status „${result.status}“ für Maßnahme #${result.id} – nicht als laufend gewertet.`};
  await load();
 }finally{button.disabled=false;button.textContent=label}
}
$("trafficQueue").addEventListener("click",e=>{const b=e.target.closest(".start-experiment");if(b)guarded(()=>startExperiment(b))});
$("queueList").addEventListener("click",e=>{const b=e.target.closest(".start-experiment");if(b)guarded(()=>startExperiment(b))});
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
 renderHealth(state.health);
 renderTraffic(state.acquisition);
 renderGrowth(state.growth_v5);
 renderDiscovery(state.discovery_v6);
 renderBackfill(state.backfill);
 renderLearning(state.learning_v4);
 const bt7=state.learning_v4?.backtests?.["168"];
 $("videos").innerHTML=state.videos.map(v=>{const q=v4For(v.id),r=q.recommendation,next=r?.next?.[0];const ps=state.growth_v5?.plan?.ranking?.find(x=>x.video_id===v.id);const paid=(v.advertising_views_reported||0)>0||q.regime==="paid_excluded";return `<tr><td><button data-video="${esc(v.id)}">${esc(v.title)}</button><small>${v.focus?"FOKUSVIDEO · ":""}${esc(v.content_type)} · ${num(v.duration/60,1)} min</small></td><td class="${regimeClass(q.regime)}">${esc(REGIME_LABELS[q.regime]||q.regime)}<small>${esc(q.regime)} · Stand ${esc(q.origin_day||"—")}</small></td><td class="${v.direction==="Gewinnt"?"up":v.direction==="Verliert"?"down":""}">${num(v.score,1)}<small>${esc(v.direction)} · ${esc(v.regime||"unknown")}</small></td><td>${num(v.velocity,1)}</td><td>${num(v.views)}</td><td>${forecastCell(q)}</td><td>${modelCell(bt7)}</td><td>${num(r?.confidence?.n_rows)}<small>${esc(r?.confidence?.level||"insufficient_data")} · ${r?.confidence?.n_videos??0} Videos</small></td><td class="${paid?"down":""}">${ps?esc(PAID_LABELS[ps.paid_status]||ps.paid_status)+"<small>"+esc(ps.paid_note||"")+"</small>":paid?"Werbetraffic":"organisch (Bericht)"}</td><td>${next?`<strong>${esc(next.action)}</strong><small>${esc(next.detail)}</small>`:"—"}</td></tr>`}).join("");
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
 const retention=d.reports.retention?.rows||[],traffic=d.reports.traffic?.rows||[],h=d.history||{traffic_sources:[],retention_months:[],progress:[]};
 const impressions=d.reach.reduce((s,r)=>s+r.impressions,0);
 const ctr=impressions?d.reach.reduce((s,r)=>s+r.impressions*(r.ctr||0),0)/impressions:null;
 $("detail").hidden=false;
 $("detail").innerHTML=`<div class="card"><p class="eyebrow">VIDEOANALYSE</p><h2>${esc(v.title)}</h2><p class="muted">Letzter Snapshot: ${esc(v.last_snapshot)} · Analytics bis ${esc(v.analytics_end||"unbekannt")} · ${v.peer_count} Vergleichsvideos</p>${chart(d.snapshots,r=>new Date(r.observed_at).getTime(),r=>r.views)}<h3>Growth-Zeitfenster</h3><p>Regime: <strong>${esc(v.regime)}</strong> · Evidenz: ${esc(v.evidence_quality)}</p><table><thead><tr><th>Fenster</th><th>Views/h</th><th>Views/h²</th><th>Datenqualität</th></tr></thead><tbody>${Object.entries(v.windows||{}).map(([label,w])=>`<tr><td>${esc(label)}</td><td>${num(w.velocity,2)}</td><td>${num(w.acceleration,3)}</td><td>${esc(w.quality)}</td></tr>`).join("")}</tbody></table><p class="muted">CTR und Analytics: ${esc(JSON.stringify(v.metric_status||{}))}. Regime sind beschreibende Signale, keine Breakout-Wahrscheinlichkeiten.</p><h3>Prognostizierte Gesamtviews</h3><p class="muted">Ziele beziehen sich auf Gesamtzähler unter der Bedingung organischen zukünftigen Zuwachses, nicht auf nachgewiesene organische Lifetime-Views. Fehlende Daten: insufficient_data. Gesamtzähler sind nicht automatisch rein organisch. Bei erkanntem Werbetraffic werden neue Prognosen ausgesetzt. Wahrscheinlichkeiten sind empirische Schätzungen, insbesondere im Millionenbereich unsicher.</p><div class="forecast">${v.forecasts.map(p=>`<article><span>${p.hours===24?"24 Stunden":p.hours===168?"7 Tage":"30 Tage"}</span><strong>${num(p.views)}</strong><small>${num(p.lower)} – ${num(p.upper)}</small><p class="muted">${intervalLabel(p)} · n=${p.n}</p><p class="muted">100.000: ${pct(p.p100k)}<br>1.000.000: ${pct(p.p1m)}</p><p class="muted">Confidence: ${esc(p.audit?.confidence||"insufficient_data")} · Regime: ${esc(p.audit?.regime||v.regime)}<br>100k: ${esc(p.audit?.targets?.["100000"]?.status||"insufficient_data")}<br>1M: ${esc(p.audit?.targets?.["1000000"]?.status||"insufficient_data")}<br>Modell: ${esc(p.model)}</p><p class="muted">Bis ${new Date(p.target).toLocaleString("de-CH")}</p></article>`).join("")||"<p>Prognosen benötigen mehrere aktuelle Snapshots.</p>"}</div></div>
 <div class="detail-grid"><div class="card"><h3>Was die Messwerte zeigen</h3>${v.reasons.map(r=>"<p>"+esc(r)+"</p>").join("")}<h3>Priorisierte Tests (Hypothesen)</h3>${(v.forecasts[0]?.recommendations||[]).map(r=>`<p><strong>P${r.priority} · ${esc(r.dimension)}</strong>: ${esc(r.hypothesis)}<br>${esc(r.test)}<br><small>${esc(r.confidence)} · ${esc(r.evidence||"insufficient_data")}</small></p>`).join("")}<h3>Nächste Schritte</h3><ul>${v.actions.map(r=>"<li>"+esc(r)+"</li>").join("")}</ul><p class="muted">Aktionen sind Vorschläge. Am YouTube-Kanal wird nichts automatisch geändert.</p></div>
 <div class="card"><h3>Qualität & Monetarisierung</h3><p>Organische Views im Traffic-Bericht: ${num(v.organic_views_reported)}</p><p>Als Werbung klassifizierte Views: ${num(v.advertising_views_reported)}</p><p>Watchtime-Effizienz: ${v.watchtime_efficiency==null?"—":pct(v.watchtime_efficiency)}</p><p>Gewonnene / Netto-Abonnenten: ${num(v.subscribers_gained)} / ${num(v.subscribers_net)}</p><p>Thumbnail-Impressionen: ${d.reach.length?num(impressions):"Nicht verfügbar"}</p><p>Gewichtete CTR: ${ctr==null?"Nicht verfügbar":pct(ctr)}</p><p class="muted">Reichweite: letzte ${d.reach.length} verfügbare Tagesberichte.</p><p>Umsatz: ${num(d.monetization.revenue,2)} USD · RPM: ${num(d.monetization.rpm,2)} USD</p><p class="muted">Returning Viewers und langfristiger Zuschauerwert: derzeit nicht verfügbar.</p></div></div>
 ${growthCard(d.growth)}
 ${strategyCard(d.strategy)}
 <div class="card"><h3>Historie (Backfill)</h3><p class="muted">Tagesgenaue Traffic-Quellen ${esc(day(h.traffic_first))} bis ${esc(day(h.traffic_last))} · Retention in ${h.retention_months.length} Monatsfenstern · Werbetraffic gesamt: ${num(h.paid_views)} Views (vom organischen Training ausgeschlossen)</p>${h.traffic_sources.map(t=>"<p>"+esc(t.source)+(t.paid?" · WERBUNG":"")+" <strong>"+num(t.views)+"</strong> <small>"+num(t.watch_minutes)+" min</small></p>").join("")||"<p class='muted'>Noch keine historischen Traffic-Quellen importiert.</p>"}${h.retention_months.length?`<table><thead><tr><th>Monat</th><th>Ø Wiedergaberatio</th><th>Punkte</th></tr></thead><tbody>${h.retention_months.map(m=>`<tr><td>${esc(m.start)} – ${esc(m.end)}</td><td>${m.average_watch_ratio==null?"—":pct(m.average_watch_ratio)}</td><td>${m.points}</td></tr>`).join("")}</tbody></table>`:""}<p class="muted">Backfill-Stand: ${h.progress.map(p=>esc(p.kind)+" "+esc(p.status)+" bis "+esc(day(p.through))+(p.note?" ("+esc(p.note)+")":"")).join(" · ")||"noch nicht gestartet"}</p></div>
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
