'use strict';
const $ = id => document.getElementById(id);
const PAGE = 50;
const ACTIVE = ['working', 'waiting', 'blocked'];
const LABEL = {working:'Working', waiting:'Waiting', blocked:'Blocked', finished:'Finished', stopped:'Stopped', interrupted:'Interrupted', lost_contact:'Lost contact'};
const state = {compose:null, drafts:{}, view:'now', filter:'', sessions:[], events:[], before:null, more:false, selected:null, detailEvents:[], detailBefore:null, detailMore:false, synced:null, live:false, request:0, detailRequest:0};
let timer;
const loadedVersion=document.documentElement.dataset.axonVersion;
const RELOAD_STATE='axon:reload-state';
let restoredDraft=null;
try {
  const saved=JSON.parse(sessionStorage.getItem(RELOAD_STATE)||'null');
  if (saved) {state.drafts=saved.drafts||{};state.compose=saved.compose||null;restoredDraft=saved.detail||null;}
  sessionStorage.removeItem(RELOAD_STATE);
} catch {}
function observeVersion(response) {
  const version=response.headers.get('X-Axon-Version');
  if (!loadedVersion || !version || loadedVersion===version || state.reloading) return;
  // Let an in-flight send settle before capturing its draft or navigating away.
  if (document.querySelector('.message-form button:disabled, .card-composer .send-button:disabled')) return;
  const detail=state.selected ? {...state.selected,text:$('message-text').value} : null;
  const saved={drafts:state.drafts,compose:state.compose,detail};
  try {sessionStorage.setItem(RELOAD_STATE,JSON.stringify(saved));}
  catch {if(detail?.text || Object.values(state.drafts).some(Boolean)) return;}
  state.reloading=true;route();location.reload();
}
async function checkVersion() {
  if (document.hidden || state.versionChecking) return;
  state.versionChecking=true;state.versionChecked=Date.now();
  try {observeVersion(await fetch('/health',{cache:'no-store',signal:AbortSignal.timeout(8000)}));}
  catch {} // Normal activity refresh handles outage messages and retry.
  finally {state.versionChecking=false;}
}


function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
function quotaCountdown(seconds) {
  const minutes=Math.max(0,Math.ceil(seconds/60)), hours=Math.floor(minutes/60), days=Math.floor(hours/24);
  return days ? days+'d '+hours%24+'h' : hours ? hours+'h '+minutes%60+'m' : minutes+'m';
}
function renderResources(values=[]) {
  const inference=values.filter(row=>row.provider==='inference');
  const rows=values.filter(row=>row.provider!=='inference');
  for(const provider of ['codex','claude']) if(!rows.some(row=>row.provider===provider)) rows.push({provider,account:'primary',windows:[]});
  rows.sort((a,b)=>['codex','claude'].indexOf(a.provider)-['codex','claude'].indexOf(b.provider) || a.account.localeCompare(b.account));
  const now=Date.now()/1000;
  const view=rows.map(row=>{
    // An empty window blocks the account until its reset; when both are empty, the later reset is the one that matters.
    const blocking=row.windows.filter(item=>item.used_percent!=null && item.used_percent>=100 && !(item.resets_at!=null && item.resets_at<=now));
    const until=blocking.length ? Math.max(...blocking.map(item=>item.resets_at ?? Infinity)) : null;
    return {provider:row.provider,account:row.account,
    stale:!!row.observed_at && now-row.observed_at>300,
    updated:row.observed_at ? relative(row.observed_at) : null,
    unavailable:until===null ? null : Number.isFinite(until) ? quotaCountdown(until-now) : 'Reset unknown',
    windows:['short','weekly'].map(name=>{
      const window=row.windows.find(item=>item.name===name), expired=window?.resets_at!=null && window.resets_at<=now;
      const remaining=window?.used_percent!=null && !expired ? Math.max(0,Math.min(100,100-window.used_percent)) : null;
      const minutes=window?.window_minutes || (name==='short'?300:10080);
      return {name,label:name==='weekly'?'Weekly':minutes<60?minutes+'m':minutes/60+'h',remaining,
        reset:expired?'Awaiting update':window?.resets_at ? 'Resets in '+quotaCountdown(window.resets_at-now) : 'Reset unknown'};
    })};});
  const signature=JSON.stringify([view,inference.map(row=>({...row,stale:now-row.observed_at>30})),state.historyStamp]);if(state.resourceSignature===signature)return;state.resourceSignature=signature;
  const fragment=document.createDocumentFragment();
  const counted=new Set();
  for(const row of view){
    const panel=node('article','resource-panel agent-'+row.provider+(row.stale?' stale':'')+(row.unavailable?' unavailable':''));
    const header=node('div','resource-heading');
    header.append(node('h2','',agentName({agent:row.provider})+(row.account==='primary'?'':' · '+row.account)),node('span','resource-age',!row.updated?'Waiting for data':(row.stale?'Stale · ':'Updated ')+row.updated));
    panel.append(header);
    const windows=node('div','resource-windows');
    for(const window of row.windows){
      const box=node('div','resource-window'), label=node('div','quota-label');
      label.append(node('span','',window.label),node('strong','',window.remaining===null?'Unknown':Math.floor(window.remaining)+'% left'));
      const bar=node('div','quota-bar'+(window.remaining===null?' unknown':window.remaining<=10?' low':window.remaining<=25?' limited':''));
      bar.setAttribute('role','progressbar');bar.setAttribute('aria-label',agentName({agent:row.provider})+' '+window.label+' remaining');bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');
      if(window.remaining!==null){bar.setAttribute('aria-valuenow',String(window.remaining));const fill=node('span','quota-fill');fill.style.width=window.remaining+'%';bar.append(fill);}else bar.setAttribute('aria-valuetext','Unknown');
      box.append(label,bar,node('div','quota-reset',window.reset));windows.append(box);
    }
    panel.append(windows);
    // Tokens belong to the agent, not an account, so they sit on that agent's first panel.
    if(!counted.has(row.provider)){counted.add(row.provider);const line=tokenLine(state.tokens?.[row.provider]);if(line)panel.append(line);}
    if(row.unavailable){
      const overlay=node('div','unavailable-overlay');
      overlay.append(node('span','unavailable-label','Out of tokens'),node('strong','unavailable-countdown',row.unavailable),node('span','unavailable-label','until reset'));
      windows.setAttribute('aria-hidden','true');panel.append(overlay);
    }
    fragment.append(panel);
  }
  for(const row of inference){
    const stale=now-row.observed_at>30;
    const panel=node('article','resource-panel inference-panel'+(stale?' stale':''));
    const header=node('div','resource-heading');
    header.append(node('h2','',row.account+' · Inference'),node('span','resource-age',stale?'Stale · '+inferenceAge(row.observed_at):row.online?'Live':'Endpoint offline'));
    panel.append(header);
    // Left: what the hardware is doing now and the headline numbers. Right: load over the horizon.
    const body=node('div','inference-body'), current=node('div','inference-now');
    const gpus=row.gpus||[];
    if(gpus.length){
      // A host collector reports real device utilization; the bar says whether the GPU is idle or working.
      for(const gpu of gpus){
        const detail=[gpu.power_watts==null?null:fmt(gpu.power_watts,' W'),gpu.temperature_c==null?null:fmt(gpu.temperature_c,' °C')].filter(Boolean);
        current.append(activityBar(row,gpu.utilization_percent,gpu.name||'GPU','busy',detail.join(' · ')||'Power not reported'));
      }
    }else{
      // Polling reaches only the endpoint, which never reports hardware; KV cache is the one occupancy share it does give.
      current.append(activityBar(row,row.online?row.metrics.kv_cache_percent:null,'KV cache','used',row.online?'No host GPU telemetry':'Endpoint offline'));
    }
    const stats=node('div','inference-stats');
    const show=(value,label)=>{const stat=node('div','inference-stat');stat.append(node('strong','',value),node('span','',label));stats.append(stat);};
    if(row.online){
      show(row.metrics.generation_tps==null?'–':Math.round(row.metrics.generation_tps)+'','tok/s');
      show((row.metrics.running??'–')+'/'+(row.metrics.queued??'–'),'run/queue');
    }
    // An APU keeps its weights in shared memory, so that is the pool worth watching when it exists.
    const gib=bytes=>Math.round(bytes/1073741824);
    for(const gpu of gpus){
      const [used,total]=gpu.shared_total_bytes!=null?[gpu.shared_used_bytes,gpu.shared_total_bytes]:[gpu.memory_used_bytes,gpu.memory_total_bytes];
      if(used!=null&&total!=null)show(gib(used)+'/'+gib(total),'GiB');
    }
    current.append(stats);body.append(current);
    const history=state.history?.[row.account];
    if(history) body.append(trendChart(history,'output_tps','Output',' tok/s'));
    panel.append(body);
    const line=tokenLine(history?.tokens);if(line)panel.append(line);
    fragment.append(panel);
  }
  // One row to watch everything, one disclosure to open the detail grid beneath it.
  const container=$('resources');
  const strip=node('div','resource-strip');
  for(const item of view){
    const seg=node('span','strip-seg agent-'+item.provider+(item.stale?' stale':''));
    seg.append(node('b','strip-name',agentName({agent:item.provider})+(item.account==='primary'?'':' · '+item.account)));
    for(const window of item.windows){
      seg.append(miniBar(agentName({agent:item.provider}),window));
      seg.append(node('b','strip-pct',window.remaining===null?'?':Math.floor(window.remaining)+'%'));
    }
    if(item.unavailable)seg.append(node('b','out-label','out · '+item.unavailable));
    strip.append(seg);
  }
  for(const item of inference){
    const stale=now-item.observed_at>30;
    const seg=node('span','strip-seg inference-seg'+(stale?' stale':''));
    const spark=miniSpark(state.history?.[item.account]);
    seg.setAttribute('title',inferenceSummary(item,stale));
    seg.append(node('b','strip-name',item.account),spark.node);
    if(!item.online)seg.append(node('span','strip-detail','offline'));
    else if(stale)seg.append(node('span','strip-detail','stale '+inferenceAge(item.observed_at)));
    else{
      const series=spark.name ? state.history?.[item.account]?.series?.[spark.name] : null;
      const value=spark.name==='output_tps' ? item.metrics?.generation_tps : series?.length ? series[series.length-1][1] : null;
      seg.append(node('b','strip-pct',value==null?'–':Math.round(value)+(spark.name==='output_tps'?' tok/s':'%')));
    }
    strip.append(seg);
  }
  const compact=resourcesCompact();
  const toggle=node('button','strip-toggle');toggle.type='button';
  const chevron=node('span','collapse-chevron',compact?'▸':'▾');
  toggle.append(chevron);
  const labelState=expanded=>toggle.setAttribute('aria-label',expanded?'Collapse resource details':'Expand resource details');
  toggle.setAttribute('aria-expanded',String(!compact));labelState(!compact);
  toggle.addEventListener('click',()=>{
    const next=!container.classList.contains('compact');
    container.classList.toggle('compact',next);chevron.textContent=next?'▸':'▾';
    toggle.setAttribute('aria-expanded',String(!next));labelState(!next);
    try{localStorage.setItem(COMPACT_KEY,next?'1':'0');}catch{}
  });
  strip.append(toggle);
  const panels=document.createElement('div');panels.className='resource-panels';
  // Phones give inference panels their own full rows, so only the quota panels share the first.
  panels.style.setProperty('--resource-count',String(Math.max(1,fragment.children.length)));
  panels.style.setProperty('--quota-count',String(Math.max(1,view.length)));
  panels.append(fragment);
  container.classList.toggle('compact',compact);
  container.replaceChildren(strip,panels);
}
function statusName(value) { return Object.hasOwn(LABEL, value) ? value : 'lost_contact'; }
function status(value) {
  const key = statusName(value), span = node('span', 'status-label '+key);
  span.append(node('i', 'status-dot '+key), document.createTextNode(LABEL[key]));
  return span;
}
function relative(at) {
  const seconds = Math.max(0, Math.floor(Date.now()/1000-Number(at)));
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return Math.floor(seconds/60)+'m ago';
  if (seconds < 86400) return Math.floor(seconds/3600)+'h ago';
  return Math.floor(seconds/86400)+'d ago';
}
function fmt(value, suffix='') { return value==null ? 'Unknown' : Number(value).toFixed(1)+suffix; }
function activityBar(row, used, label, verb, note) {
  // Activity, not headroom: the bar fills as the device works, so idle reads as empty at a glance.
  const busy = used==null ? null : Math.max(0, Math.min(100, used));
  const box = node('div','resource-window'), caption = node('div','quota-label');
  caption.append(node('span','',label), node('strong','',busy===null?'Unknown':Math.round(busy)+'% '+verb));
  const bar = node('div','quota-bar activity-bar'+(busy===null?' unknown':busy<5?' idle':''));
  bar.setAttribute('role','progressbar'); bar.setAttribute('aria-label',row.account+' '+label+' '+verb);
  bar.setAttribute('aria-valuemin','0'); bar.setAttribute('aria-valuemax','100');
  if (busy!==null) { bar.setAttribute('aria-valuenow',String(busy)); const fill = node('span','quota-fill'); fill.style.width = busy+'%'; bar.append(fill); }
  else bar.setAttribute('aria-valuetext','Unknown');
  box.append(caption, bar, node('div','quota-reset',note));
  return box;
}
function trendChart(history, name, label, unit) {
  const chart=node('figure','trend');
  const points=(history.series[name]||[]).filter(point=>point[1]!=null);
  const span='last '+horizonName(history.range);
  if(!points.length){chart.append(node('figcaption','trend-label',label),node('div','trend-empty','No samples'),node('div','trend-note',span));return chart;}
  const end=Math.max(...Object.values(history.series).flatMap(series=>series.map(point=>point[0]))), start=end-history.range;
  const peak=Math.max(...points.map(point=>point[1])), top=peak>0 ? peak*1.1 : 1;
  const value=v=>(v>=100||Number.isInteger(v) ? Math.round(v) : v.toFixed(1))+unit;
  // A long span's last step is an average, and the live reading sits beside the chart, so the chart leads with its peak.
  const resting='peak '+value(peak);
  const caption=node('figcaption','trend-label'), reading=node('strong','',resting);
  caption.append(node('span','',label),reading);
  const W=240, H=48, x=at=>(at-start)/(end-start||1)*W, y=v=>H-1-Math.min(v,top)/top*(H-2);
  // Break the line where Prometheus has no sample instead of drawing across the gap.
  const runs=[];let run=[];
  for (const [i,point] of points.entries()) {
    if (i && point[0]-points[i-1][0]>history.step*1.5) {runs.push(run);run=[];}
    run.push(point);
  }
  runs.push(run);
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);svg.setAttribute('preserveAspectRatio','none');
  svg.setAttribute('role','img');svg.setAttribute('aria-label',`${label}, ${span}: ${resting}`);
  const draw=(tag,cls,attrs)=>{const el=document.createElementNS('http://www.w3.org/2000/svg',tag);el.setAttribute('class',cls);for(const k in attrs)el.setAttribute(k,attrs[k]);svg.append(el);return el;};
  draw('line','trend-base',{x1:0,x2:W,y1:H-.5,y2:H-.5});
  for (const part of runs) {
    const line=part.map(point=>x(point[0]).toFixed(1)+','+y(point[1]).toFixed(1)).join(' ');
    draw('polygon','trend-area',{points:`${x(part[0][0]).toFixed(1)},${H} ${line} ${x(part[part.length-1][0]).toFixed(1)},${H}`});
    draw('polyline','trend-line',{points:part.length>1 ? line : line+' '+(x(part[0][0])+1).toFixed(1)+','+y(part[0][1]).toFixed(1)});
  }
  const cursor=draw('line','trend-cursor',{x1:0,x2:0,y1:0,y2:H});
  const note=node('div','trend-note',span);
  svg.addEventListener('pointermove',event=>{
    const box=svg.getBoundingClientRect(), at=start+(event.clientX-box.left)/box.width*(end-start);
    const point=points.reduce((best,p)=>Math.abs(p[0]-at)<Math.abs(best[0]-at)?p:best);
    cursor.setAttribute('x1',x(point[0]));cursor.setAttribute('x2',x(point[0]));chart.classList.add('hover');
    reading.textContent=value(point[1]);note.textContent=history.range>86400 ? new Date(point[0]*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : clock(point[0]);
  });
  svg.addEventListener('pointerleave',()=>{chart.classList.remove('hover');reading.textContent=resting;note.textContent=span;});
  chart.append(caption,svg,note);
  return chart;
}
function tokenCount(n) { return n>=1e9 ? (n/1e9).toFixed(1)+'B' : n>=1e6 ? (n/1e6).toFixed(1)+'M' : n>=1e3 ? Math.round(n/1e3)+'k' : String(Math.round(n)); }
function tokenLine(tokens) {
  // Sent up includes cached context, which dominates agent sessions, so its share is said rather than hidden.
  if (!tokens || tokens.input==null && tokens.output==null) return null;
  const line=node('div','token-line'), span=$('period').value==='all' ? 'any time' : horizonName(Number($('period').value));
  line.append(node('span','token-label','Tokens · '+span),node('strong','','↑ '+tokenCount(tokens.input||0)+'  ↓ '+tokenCount(tokens.output||0)));
  if (tokens.cached) line.title=Math.round(tokens.cached/Math.max(1,tokens.input)*100)+'% of tokens sent were cached'+(tokens.sessions ? ' · '+tokens.sessions+' sessions' : '');
  return line;
}
function horizonName(seconds) { return seconds>=86400 ? seconds/86400+'d' : seconds>=3600 ? seconds/3600+'h' : seconds/60+'m'; }
// Trends come from the server's Prometheus; a server without one answers 404 and the panel keeps its live readings only.
// Agent token totals ride the same once-a-minute refresh.
async function loadHistory() {
  const accounts=state.historyOff ? [] : (state.resources||[]).filter(row=>row.provider==='inference').map(row=>row.account);
  state.historyAt=Date.now();
  const range=$('period').value==='all' ? 2592000 : Number($('period').value), history={};
  for (const account of accounts) {
    try { history[account]=await api('/resources/history',new URLSearchParams({account,range})); }
    catch (error) { if (/ 404\./.test(error.message)) state.historyOff=true; }
  }
  try { state.tokens=(await api('/resources/tokens',new URLSearchParams({since:$('period').value==='all' ? 0 : Math.floor(Date.now()/1000)-range}))).tokens; }
  catch { state.tokens=null; }
  state.history=history;state.historyStamp=state.historyAt;renderResources(state.resources);
}
function inferenceAge(at) {
  // Inference turns stale in 30s, too soon for relative()'s minute floor to read as stale.
  const seconds = Math.max(0, Math.floor(Date.now()/1000-Number(at)));
  return seconds < 60 ? seconds+'s ago' : relative(at);
}
const COMPACT_KEY='axon:resources-compact';
function resourcesCompact() {
  // The strip is the resting state on every screen; width decides how much of each segment fits.
  try { const saved=localStorage.getItem(COMPACT_KEY); if(saved!==null) return saved==='1'; } catch {}
  return true;
}
function miniBar(label,window) {
  // Tiny bar for the strip: fill is amount left, identity rides on the agent color.
  const bar=node('span','mini-bar'+(window.remaining===null?' unknown':''));
  if(window.remaining!==null){const fill=node('i');fill.style.width=window.remaining+'%';bar.append(fill);}
  const detail=label+' '+window.label+': '+(window.remaining===null?'unknown':Math.floor(window.remaining)+'% left');
  bar.setAttribute('title',detail);bar.setAttribute('role','progressbar');bar.setAttribute('aria-label',detail);
  bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');
  if(window.remaining!==null)bar.setAttribute('aria-valuenow',String(window.remaining));else bar.setAttribute('aria-valuetext','Unknown');
  return bar;
}
function miniSpark(history) {
  // The strip's inference mark: same glance language as the quota bars, but over time.
  const name=['gpu_busy_percent','output_tps','kv_cache_percent'].find(key=>(history?.series?.[key]||[]).some(point=>point[1]!=null));
  if(!name){const bar=node('span','mini-bar unknown');bar.setAttribute('aria-label','Inference history unavailable');return {node:bar,name};}
  const take=(history.series[name].filter(point=>point[1]!=null)).slice(-48);
  const W=56,H=12,max=Math.max(...take.map(point=>point[1]),1e-9);
  const x=index=>index*(W/Math.max(1,take.length-1)), y=value=>H-1-(value/max)*(H-2);
  const line=take.map((point,index)=>(index?'L':'M')+x(index).toFixed(1)+' '+y(point[1]).toFixed(1)).join('');
  const element=document.createElementNS('http://www.w3.org/2000/svg','svg');
  element.setAttribute('viewBox',`0 0 ${W} ${H}`);element.setAttribute('class','mini-spark');element.setAttribute('aria-hidden','true');
  const area=document.createElementNS('http://www.w3.org/2000/svg','path');
  area.setAttribute('class','mini-spark-area');area.setAttribute('d',line+`L ${W} ${H} L 0 ${H} Z`);
  const stroke=document.createElementNS('http://www.w3.org/2000/svg','path');
  stroke.setAttribute('class','mini-spark-line');stroke.setAttribute('d',line);
  element.append(area,stroke);
  return {node:element,name};
}
function inferenceSummary(row,stale) {
  // The collapsed one-liner: status plus the few numbers worth a glance on a phone.
  const parts=[stale?'stale '+inferenceAge(row.observed_at):row.online?'live':'offline'];
  const metrics=row.metrics||{};
  if(row.online){
    if(metrics.generation_tps!=null)parts.push(Math.round(metrics.generation_tps)+' tok/s');
    parts.push((metrics.running??'–')+'/'+(metrics.queued??'–')+' rq');
  }
  const gpus=row.gpus||[];
  const gib=bytes=>Math.round(bytes/1073741824);
  gpus.forEach((gpu,index)=>{
    if(gpu.utilization_percent!=null)parts.push((gpus.length>1?'GPU'+(index+1)+' ':'gpu ')+Math.round(gpu.utilization_percent)+'%');
    const [used,total]=gpu.shared_total_bytes!=null?[gpu.shared_used_bytes,gpu.shared_total_bytes]:[gpu.memory_used_bytes,gpu.memory_total_bytes];
    if(used!=null&&total!=null)parts.push(gib(used)+'/'+gib(total)+'G');
  });
  if(!gpus.length&&row.online&&metrics.kv_cache_percent!=null)parts.push('kv '+Math.round(metrics.kv_cache_percent)+'%');
  return parts.join(' · ');
}
function clock(at) { return new Date(Number(at)*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }
function date(at) { return new Date(Number(at)*1000).toLocaleDateString([], {month:'long', day:'numeric', year:'numeric'}); }
function key(row) { return row.source+'/'+row.session; }
function agentClass(row) { return ['codex','claude','hermes','pi','opencode'].includes(row.agent) ? 'agent-'+row.agent : 'agent-other'; }
function agentName(row) { return ({codex:'Codex',claude:'Claude',hermes:'Hermes',pi:'Pi',opencode:'OpenCode'})[row.agent] || row.agent || 'Agent'; }
function hostName(row) { return row.machine || 'Host not reported'; }
const HOST_KINDS = ['laptop', 'desktop', 'server'];
const INPUT_AGENTS = ['claude','codex','hermes'];
const TOKEN_KEY = 'axon-input-token';
// Input tokens are URL-safe ([A-Za-z0-9_-]); anything else — spaces, newlines, curly quotes and
// zero-width characters smuggled in by mobile keyboards or paste apps — is dropped at both ends,
// so a mangled paste can never poison the Authorization header with a character fetch rejects.
function storedToken() { try { return (localStorage.getItem(TOKEN_KEY) || '').replace(/[^A-Za-z0-9_-]/g,''); } catch { return ''; } }
function storeToken(value) { const clean=(value||'').replace(/[^A-Za-z0-9_-]/g,''); try { clean ? localStorage.setItem(TOKEN_KEY,clean) : localStorage.removeItem(TOKEN_KEY); } catch {} }
function listeningLeft(row) {
  const left=Number(row.listening_until||0)-Date.now()/1000;
  return row.effective_state==='waiting' && left>0 ? left : 0;
}
function duration(seconds) { const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60); return h ? h+'h '+m+'m' : Math.max(1,m)+'m'; }
function hostIcon(kind) {
  if (!HOST_KINDS.includes(kind)) return null;
  const icon=node('i','host-icon kind-'+kind);
  icon.title=kind[0].toUpperCase()+kind.slice(1);icon.setAttribute('aria-hidden','true');
  return icon;
}
// Older reporters omit the kind; any session from the same host can supply it.
function hostKind(sessions) { return [...sessions].sort((a,b)=>Number(b.updated_at)-Number(a.updated_at)).find(row=>row.machine_kind)?.machine_kind; }
// Minute buckets avoid trading places over near-simultaneous progress reports.
function activityBucket(row) { return Math.floor(Number(row.activity_at ?? row.updated_at)/60); }
function sessionOrder(a,b) {
  return Number(ACTIVE.includes(b.effective_state))-Number(ACTIVE.includes(a.effective_state))
    || activityBucket(b)-activityBucket(a) || key(a).localeCompare(key(b));
}
function boardOrder(a,b) {
  return hostName(a).localeCompare(hostName(b)) || sessionOrder(a,b);
}
function agentOrder([nameA,rowsA],[nameB,rowsB]) {
  const live=rows=>rows.some(row=>ACTIVE.includes(row.effective_state));
  const latest=rows=>Math.max(...rows.map(activityBucket));
  return Number(live(rowsB))-Number(live(rowsA)) || latest(rowsB)-latest(rowsA) || nameA.localeCompare(nameB);
}

function options(id, values, label) {
  const select=$(id), current=select.value || '';
  const unique=[...new Set(values.filter(Boolean))];
  if (id!=='project') unique.sort();
  if (current && !unique.includes(current)) unique.push(current);
  const signature=JSON.stringify([...unique].sort());
  if (select.dataset.signature===signature) return;
  select.dataset.signature=signature;
  select.replaceChildren(new Option(label,''),...unique.map(value=>new Option(id==='agent'?agentName({agent:value}):value,value)));
  select.value=current;
}
function query(extra={}) {
  const params = new URLSearchParams(extra);
  for (const name of ['project','agent']) if ($(name).value) params.set(name, $(name).value);
  if ($('host').value) params.set('machine',$('host').value);
  if ($('search').value.trim()) params.set('q', $('search').value.trim());
  if ($('period').value!=='all') params.set('since', String(Math.floor(Date.now()/1000-Number($('period').value))));
  return params;
}
function route() {
  const params = new URLSearchParams({view:state.view});
  for (const id of ['host','project','agent','period','search']) if ($(id).value) params.set(id, $(id).value);
  if (state.filter) params.set('state', state.filter);
  if ($('heartbeats').checked) params.set('heartbeats','1');
  if (state.selected) { params.set('source',state.selected.source); params.set('session',state.selected.session); }
  history.replaceState(null,'','#'+params.toString());
}
async function api(path, params) {
  const response = await fetch(path+(params ? '?'+params : ''), {cache:'no-store',signal:AbortSignal.timeout(8000)});
  observeVersion(response);
  if (!response.ok) throw new Error('The journal returned '+response.status+'.');
  return response.json();
}
function empty(title, detail) {
  const box = node('div','empty'); box.append(node('span','empty-symbol','◌'),node('h2','',title),node('p','',detail)); return box;
}
function card(row, grouped=false) {
  const button = node('button','session-card '+agentClass(row)); button.type='button'; button.dataset.sessionKey=key(row);
  const top=node('div','card-top'), identity=node('h2','agent-identity');
  identity.append(node('span','agent-label',agentName(row)),node('span','on-label',' on '),node('span','host-name',hostName(row)));
  top.append(identity);
  const activity=row.activity_at ?? row.updated_at;
  const time=node('time','card-time',relative(activity)); time.dataset.at=activity; time.title='Last activity: '+new Date(activity*1000).toLocaleString()+' · Last report: '+new Date(row.updated_at*1000).toLocaleString();
  const project=node('div','project-line');project.append(node('span','project-name',row.project || 'Unassigned project'),time);
  const label=statusName(row.effective_state), bottom=node('div','card-bottom');
  const detail=label==='blocked' ? row.blocker || row.step : label==='finished' ? row.outcome : row.step || row.activity;
  bottom.append(status(label));
  if (listeningLeft(row)) bottom.append(node('span','listening-label','Listening · '+duration(listeningLeft(row))));
  bottom.append(node('span','session-id','#'+row.session.slice(0,8)));
  if (!grouped) button.append(top);
  button.classList.toggle('grouped-card',grouped);
  button.setAttribute('aria-label',agentName(row)+' on '+hostName(row)+': '+(row.project || 'Unassigned project')+' — '+(row.goal || row.outcome || 'Work session')+' — '+LABEL[label]+' — '+row.session.slice(0,8));
  button.append(project,node('h3','work-title',row.goal || row.outcome || 'Work session'),node('p','card-step',detail || 'No further update reported.'),bottom);
  button.addEventListener('click',()=>openSession(row));
  const shell=node('div','card-shell'+(state.compose===key(row)?' composing':''));shell.dataset.composeKey=key(row);
  const frame=node('div','card-frame'), tools=node('div','card-tools');
  const archive=node('button','archive-button',row.archived_at?'Restore':'Archive');archive.type='button';
  archive.setAttribute('aria-label',(row.archived_at?'Restore':'Archive')+' session '+(row.project||row.session.slice(0,8)));
  archive.addEventListener('click',()=>archiveSession(row,!row.archived_at));tools.append(archive);
  if (INPUT_AGENTS.includes(row.agent)) tools.append(chatToggle(row));
  frame.append(button,tools);shell.append(frame);
  if (state.compose===key(row)) shell.append(composer(row));
  return shell;
}
let pendingArchive=null;
async function archiveSession(row, archived) {
  if (!storedToken()) {
    pendingArchive={row,archived};$('archive-token-status').textContent='Use the same token as session messaging.';
    $('archive-auth').showModal();$('archive-token').focus();return;
  }
  try {
    const response=await fetch(sessionPath(row)+'/archive',{method:'POST',cache:'no-store',signal:AbortSignal.timeout(8000),
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+storedToken()},body:JSON.stringify({archived})});
    if (response.status===401) {storeToken('');return archiveSession(row,archived);}
    if (!response.ok) throw new Error(response.status===403?'Configure the server input token to archive sessions.':'Could not update session ('+response.status+').');
    state.lastArchived=archived?row:null;
    $('archive-notice').hidden=!archived;
    $('archive-notice-text').textContent='Session archived.';
    state.compose=null;await refresh();
  } catch(error) {$('error').textContent=error.message;$('error').hidden=false;}
}
$('archive-token-form').addEventListener('submit',event=>{
  event.preventDefault();const token=$('archive-token').value.trim();if (!token) return;
  storeToken(token);$('archive-token').value='';$('archive-auth').close();
  const pending=pendingArchive;pendingArchive=null;if(pending) archiveSession(pending.row,pending.archived);
});
$('archive-cancel').addEventListener('click',()=>$('archive-auth').close());
$('archive-undo').addEventListener('click',()=>{if(state.lastArchived) archiveSession(state.lastArchived,false);});
function chatToggle(row) {
  const listening=listeningLeft(row), queued=Number(row.queued_messages||0), toggle=node('button','chat-toggle'+(listening?' listening':''));
  toggle.type='button';toggle.setAttribute('aria-expanded',String(state.compose===key(row)));
  const label='Message '+agentName(row)+' on '+hostName(row)+(listening ? ' (listening, wakes immediately)' : '')+(queued ? ', '+queued+' queued' : '');
  toggle.setAttribute('aria-label',label);toggle.title=label;
  toggle.append(node('i','chat-icon'));
  if (queued) toggle.append(node('span','chat-badge',queued));
  toggle.addEventListener('click',event=>{event.stopPropagation();state.compose=state.compose===key(row)?null:key(row);render();
    if (state.compose) document.querySelector('.card-composer textarea, .card-composer input')?.focus();});
  return toggle;
}
function inputNote(row, enabled=true) {
  return !enabled ? 'Session input is disabled on this server.'
    : listeningLeft(row) ? 'Idle and listening: a message wakes it immediately.'
    : row.effective_state==='working' ? 'Working: the message arrives at its next tool call.'
    // Codex hooks cannot start a turn, so an idle Codex thread reads input when it next runs.
    : row.agent==='codex' && ACTIVE.includes(row.effective_state) ? 'Idle: Codex reads this when its next turn starts.'
    : ACTIVE.includes(row.effective_state) ? 'Not listening: the message waits until the session next runs.'
    : 'Session has ended or lost contact: the message waits if it resumes.';
}
function composer(row) {
  const id=key(row), box=node('form','card-composer');
  if (!storedToken()) {
    const input=node('input');input.type='password';input.autocomplete='off';input.placeholder="Paste the server's input token";input.setAttribute('aria-label','Input token');
    const save=node('button','send-button','Save');save.type='submit';box.append(input,save);
    box.addEventListener('submit',event=>{event.preventDefault();if(input.value.trim()){storeToken(input.value.trim());render();document.querySelector('.card-composer textarea')?.focus();}});
    return box;
  }
  const text=node('textarea');text.rows=2;text.maxLength=4000;text.placeholder='Message '+agentName(row)+'…';text.setAttribute('aria-label','Message');text.value=state.drafts[id]||'';
  const send=node('button','send-button','Send'), note=node('span','composer-note',state.sent?.[id] || inputNote(row));send.type='submit';
  const actions=node('div','composer-actions');actions.append(note,send);box.append(text,actions);
  text.addEventListener('input',()=>{state.drafts[id]=text.value;});
  text.addEventListener('keydown',event=>{
    if (event.key==='Escape') {text.blur();state.compose=null;render();}
    else if (event.key==='Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault();box.requestSubmit();}
  });
  box.addEventListener('submit',async event=>{
    event.preventDefault();if (!text.value.trim()) return;
    send.disabled=true;note.textContent='Sending…';
    const result=await postMessage(row,text.value.trim());
    note.textContent=result.message;send.disabled=false;
    if (result.ok) {text.value='';delete state.drafts[id];state.sent={...state.sent,[id]:result.message};refresh();}
    else if (result.retoken) render();
  });
  return box;
}
async function postMessage(row, text) {
  try {
    const response=await fetch(sessionPath(row)+'/messages',{method:'POST',cache:'no-store',signal:AbortSignal.timeout(8000),
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+storedToken()},body:JSON.stringify({text})});
    if (response.status===401) { storeToken('');return {ok:false,retoken:true,message:'Token rejected. Paste it again.'}; }
    if (response.status===403) return {ok:false,message:'Session input is disabled on this server.'};
    if (!response.ok) return {ok:false,message:'Not sent: the journal returned '+response.status+'.'};
    return {ok:true,message:listeningLeft(row) ? 'Sent · waking it now.' : 'Sent · queued for its next hook.'};
  } catch (error) { return {ok:false,message:'Not sent: '+error.message}; }
}
function eventRow(event, compact=false) {
  const session=state.sessions.find(row=>key(row)===key(event));
  const identity={...session,agent:event.data.agent || session?.agent};
  const item=node('article','event '+agentClass(identity)), time=node('time','event-time',clock(event.at)), track=node('div','event-track');
  const mapped={start:'working', progress:'working', heartbeat:'working', waiting:'waiting', blocked:'blocked', finished:'finished', stopped:'stopped', interrupted:'interrupted'};
  time.title=new Date(event.at*1000).toLocaleString(); track.append(node('i','status-dot '+(mapped[event.kind] || '')));
  const content=node('div','event-content'), heading=node('div','event-heading');
  if (!compact) {
    const actor=node('span','event-actor');actor.append(node('span','agent-label',agentName(identity)),node('span','on-label',' on '),node('span','event-host',hostName(session || event.data)));
    heading.append(actor,node('span','project-name',event.data.project || session?.project || 'Session'));
  }
  heading.append(node('span','event-kind',event.kind.replaceAll('_',' ')));
  const d=event.data, detail=d.outcome || d.blocker || d.step || d.goal || d.activity || (event.kind==='heartbeat' ? 'Session lease renewed' : 'Session state updated');
  const button=node('button','event-detail',detail); button.type='button'; button.addEventListener('click',()=>openSession(event));
  content.append(heading,button);
  if (!compact) content.append(node('div','event-meta','#'+event.session.slice(0,8)));
  item.append(time,track,content); return item;
}
function filteredSessions() {
  let rows=state.sessions.filter(row=>!$('host').value || row.machine===$('host').value)
    .filter(row=>!$('project').value || row.project===$('project').value)
    .filter(row=>!$('agent').value || row.agent===$('agent').value);
  const search=$('search').value.trim().toLowerCase();
  if (search) rows=rows.filter(row=>JSON.stringify(row).toLowerCase().includes(search));
  if ($('period').value!=='all') rows=rows.filter(row=>row.updated_at>=Date.now()/1000-Number($('period').value));
  return rows;
}
function hostGroups(rows) {
  const fragment=document.createDocumentFragment();
  const hosts=new Map();
  for (const row of rows) {const host=hostName(row);if(!hosts.has(host)) hosts.set(host,[]);hosts.get(host).push(row);}
  for (const [host,sessions] of hosts) {
    const section=node('section','host-group'), heading=node('div','host-heading');
    const title=node('h2','host-title'), icon=hostIcon(hostKind(sessions));
    if (icon) title.append(icon);
    title.append(document.createTextNode(host));
    heading.append(title,node('span','host-count',sessions.filter(row=>ACTIVE.includes(row.effective_state)).length+' live · '+sessions.filter(row=>!ACTIVE.includes(row.effective_state)).length+' recent'));section.append(heading);
    const columns=node('div','agent-columns'), agents=new Map();
    for (const row of sessions) {const name=agentName(row);if(!agents.has(name)) agents.set(name,[]);agents.get(name).push(row);}
    for (const [name,items] of [...agents].sort(agentOrder)) {
      const group=node('section','agent-group '+agentClass(items[0]));
      const label=node('div','agent-group-heading');label.append(node('span','agent-dot'),node('h3','',name),node('span','agent-count',items.length));group.append(label);
      items.sort(sessionOrder).forEach(row=>group.append(card(row,true)));columns.append(group);
    }
    section.append(columns);fragment.append(section);
  }
  return fragment;
}
function render() {
  const counts=Object.fromEntries(ACTIVE.map(value=>[value,filteredSessions().filter(s=>!s.archived_at && s.effective_state===value).length]));
  for (const value of ACTIVE) $('count-'+value).textContent=counts[value];
  $('live-badge').textContent=state.sessions.filter(row=>!row.archived_at && ACTIVE.includes(row.effective_state)).length;
  document.querySelectorAll('[data-view]').forEach(button=>{button.classList.toggle('selected',button.dataset.view===state.view);button.setAttribute('aria-current',button.dataset.view===state.view ? 'page' : 'false');});
  document.querySelectorAll('[data-state]').forEach(button=>{button.classList.toggle('active',button.dataset.state===state.filter);button.setAttribute('aria-pressed',String(button.dataset.state===state.filter));button.setAttribute('aria-label',LABEL[button.dataset.state]+': '+counts[button.dataset.state]);button.title=LABEL[button.dataset.state];});
  $('resume-live').hidden=!(state.view==='timeline' && state.events.length>PAGE);
  $('clear-state').hidden=!state.filter || state.view!=='now';
  $('heartbeat-control').hidden=state.view!=='timeline';
  $('nav-search').classList.toggle('has-query',!!$('search').value.trim());
  $('page-title').textContent=state.view==='now' ? 'Live board' : 'Timeline';
  $('page-subtitle').textContent=state.view==='now' ? 'Hosts → agents → work.' : 'Reported work, across hosts, agents and projects.';
  $('result-count').hidden=state.view==='now';
  document.querySelector('.section-heading').hidden=state.view==='now' && !state.filter;
  const container=$('activity');container.classList.toggle('board',state.view==='now');
  const focused=document.activeElement?.dataset?.sessionKey;
  if (state.view==='now' && document.activeElement?.closest?.('.card-composer')) return; // never wipe a draft mid-typing
  container.replaceChildren();
  if (state.view==='now') {
    const archived=filteredSessions().filter(row=>row.archived_at);
    let rows=filteredSessions().filter(row=>!row.archived_at);
    if (state.filter) rows=rows.filter(row=>row.effective_state===state.filter);
    const active=rows.filter(row=>ACTIVE.includes(row.effective_state)), visible=rows.sort(boardOrder);
    $('result-count').textContent=active.length+' live · '+(visible.length-active.length)+' recent';
    container.append(hostGroups(visible));
    if (!visible.length) container.append(empty('Nothing in this view','Change your filters, or let an agent report its next bit of work.'));
    if (archived.length) {
      const section=node('details','quiet-group archived-group');section.open=!!state.archivedOpen;
      section.addEventListener('toggle',()=>{state.archivedOpen=section.open;});
      section.append(node('summary','','Archived · '+archived.length));
      const grid=node('div','quiet-grid');archived.forEach(row=>grid.append(card(row)));section.append(grid);container.append(section);
    }
    $('load-more').hidden=true;
  } else {
    $('result-count').textContent=state.events.length+' events';
    let day='';
    for (const event of state.events) {
      const next=date(event.at);
      if (day!==next) {day=next;container.append(node('div','day-heading',day));}
      container.append(eventRow(event));
    }
    if (!state.events.length) container.append(empty('A quiet stretch','No reported events match these filters. Heartbeats are hidden by default.'));
    $('load-more').hidden=!state.more;
  }
  if (focused) [...container.querySelectorAll('[data-session-key]')].find(el=>el.dataset.sessionKey===focused)?.focus({preventScroll:true});
}
function timelineParams(extra={}) {
  const params=query({order:'desc',limit:String(PAGE),...extra});
  if (!$('heartbeats').checked) params.set('exclude_kind','heartbeat');
  return params;
}
async function refresh() {
  const request=++state.request;
  state.refreshing=true;
  try {
    const [sessions, events, resources] = await Promise.all([api('/sessions'),state.view==='timeline' ? api('/events',timelineParams()) : Promise.resolve(null),api('/resources').catch(()=>null)]);
    if (request!==state.request) return;
    state.sessions=sessions.sessions;
    if(resources) state.resources=resources.resources;
    renderResources(state.resources);
    if (!state.historyAt) loadHistory();
    options('host',state.sessions.map(s=>s.machine),'All hosts'); options('project',[...state.sessions].sort((a,b)=>b.updated_at-a.updated_at).map(s=>s.project),'All projects'); options('agent',state.sessions.map(s=>s.agent),'All agents');
    if (events) { state.events=events.events;state.before=events.next_before;state.more=events.events.length===PAGE; }
    state.synced=Date.now(); $('error').hidden=true; $('sync-dot').classList.remove('offline'); $('sync-status').textContent=state.live ? 'Live' : 'Polling · just synced';
    render(); route();
    if (state.selected) await loadDetail(false);
  } catch (error) {
    if (request!==state.request) return;
    $('error').textContent='Connection interrupted. '+error.message+' Showing cached activity where available; retrying automatically.';
    $('error').hidden=false; $('sync-dot').classList.add('offline'); $('sync-status').textContent='Disconnected';
  } finally { if (request===state.request) state.refreshing=false; }
}
async function loadMore() {
  $('load-more').disabled=true;
  try {
    const request=state.request, data=await api('/events',timelineParams({before:String(state.before)}));
    if (request!==state.request) return;
    state.events.push(...data.events);state.before=data.next_before;state.more=data.events.length===PAGE;
    render();$('sync-status').textContent='History paused';
  } catch (error) { $('error').textContent='Could not load earlier events. '+error.message;$('error').hidden=false; }
  finally { $('load-more').disabled=false; }
}
function evidence(values) {
  const list=node('ul','evidence');
  for (const value of values || []) {
    const li=node('li'); let url;
    try { url=new URL(value); } catch {}
    if (url && ['https:','http:'].includes(url.protocol)) {
      const link=node('a','',value);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';li.append(link);
    } else li.textContent=value;
    list.append(li);
  }
  return list;
}
function detailSection(title,text) {
  const section=node('section','detail-section');section.append(node('h3','',title),node('p','',text));return section;
}
function renderDetail(row) {
  const content=$('detail-content');content.replaceChildren();
  const title=node('h2','',row.goal || row.outcome || row.project || 'Work session');title.id='detail-title';content.append(title);
  const statusBox=node('div','detail-status');statusBox.append(status(row.effective_state || 'lost_contact'));
  if (listeningLeft(row)) statusBox.append(node('span','listening-label','Listening for input · '+duration(listeningLeft(row))+' left'));
  content.append(statusBox);
  const facts=node('dl','detail-facts');
  for (const [label,value] of [['Host',row.machine],['Project',row.project],['Agent',row.agent],['Source',row.source],['Session',row.session],['Branch',row.branch],['Updated',row.updated_at ? new Date(row.updated_at*1000).toLocaleString() : null]]) {
    if (value) facts.append(node('dt','',label),node('dd','',value));
  }
  content.append(facts);
  if (row.parent && row.parent.includes('/')) {
    const index=row.parent.indexOf('/'), parent=node('button','text-button','Open parent session ↗');
    parent.addEventListener('click',()=>openSession({source:row.parent.slice(0,index),session:row.parent.slice(index+1)}));content.append(parent);
  }
  for (const [label,value] of [['Latest step',row.step],['Blocker',row.blocker],['Outcome',row.outcome]]) if (value) content.append(detailSection(label,value));
  if (row.evidence?.length) { const section=node('section','detail-section');section.append(node('h3','','EVIDENCE'),evidence(row.evidence));content.append(section); }
  const section=node('section','detail-section detail-events');section.append(node('h3','','SESSION ACTIVITY'));
  if (!state.detailEvents.length) section.append(node('p','','No retained events for this session.'));
  state.detailEvents.forEach(event=>section.append(eventRow(event,true)));content.append(section);
  if (state.detailMore) {const button=node('button','load-more','Load earlier session events ↓');button.addEventListener('click',()=>loadDetail(true));content.append(button);}
}
async function openSession(row) {
  state.selected={source:row.source,session:row.session};state.detailEvents=[];state.detailBefore=null;state.detailMore=false;state.detailRendered='';
  state.thread='';$('message-thread').replaceChildren();$('message-text').value=restoredDraft && key(restoredDraft)===key(row) ? restoredDraft.text : '';restoredDraft=null;$('message-status').textContent='';$('session-input').hidden=true;
  $('detail-content').replaceChildren(node('h2','','Loading session…'));
  if (!$('session-dialog').open) $('session-dialog').showModal();
  route();await loadDetail(false);
}
async function loadDetail(older) {
  const selected=state.selected;if (!selected) return;
  const request=++state.detailRequest;
  const params=new URLSearchParams({...selected,order:'desc',limit:'100',exclude_kind:'heartbeat'});
  if (older && state.detailBefore) params.set('before',String(state.detailBefore));
  try {
    const [sessions,events,inbox]=await Promise.all([api('/sessions',new URLSearchParams(selected)),api('/events',params),
      api(sessionPath(selected)+'/messages').catch(()=>null)]);
    if (request!==state.detailRequest || !state.selected || key(selected)!==key(state.selected)) return;
    if (!older && state.detailEvents.length>100) return; // keep an explicitly paged history stable
    state.detailEvents=older ? state.detailEvents.concat(events.events) : events.events;
    state.detailBefore=events.next_before;state.detailMore=events.events.length===100;
    const row=sessions.sessions[0] || {...selected,goal:'Session not in current projection',effective_state:'lost_contact'};
    const snapshot=JSON.stringify({row,events:state.detailEvents});
    if (snapshot!==state.detailRendered) { state.detailRendered=snapshot;renderDetail(row); }
    renderInput(row,inbox);
  } catch (error) {
    if (request!==state.detailRequest) return;
    $('detail-content').replaceChildren(node('h2','','Could not load this session'),node('p','detail-error',error.message));
  }
}
function sessionPath(row) { return '/sessions/'+encodeURIComponent(row.source)+'/'+encodeURIComponent(row.session); }
function renderInput(row, inbox) {
  const box=$('session-input');
  box.hidden=!INPUT_AGENTS.includes(row.agent) || !inbox;
  if (box.hidden) return;
  const signature=JSON.stringify(inbox.messages);
  if (signature!==state.thread) {
    state.thread=signature;
    $('message-thread').replaceChildren(...inbox.messages.map(message=>{
      const item=node('li','message'+(message.delivered_at?' delivered':''));
      item.append(node('p','message-text',message.text),node('span','message-meta',clock(message.at)+' · '+(message.delivered_at ? 'Delivered '+clock(message.delivered_at) : 'Queued')));
      return item;
    }));
  }
  $('input-note').textContent=inputNote(row,inbox.input);
  const token=!!storedToken();
  $('message-form').hidden=!inbox.input || !token;$('token-form').hidden=!inbox.input || token;
}
async function sendMessage(event) {
  event.preventDefault();
  const selected=state.selected, text=$('message-text').value.trim();
  if (!selected || !text) return;
  $('message-send').disabled=true;$('message-status').textContent='Sending…';
  const row=state.sessions.find(item=>key(item)===key(selected)) || selected, result=await postMessage(row,text);
  $('message-status').textContent=result.message;$('message-send').disabled=false;
  if (result.ok) $('message-text').value='';
  await loadDetail(false);
}
$('message-form').addEventListener('submit',sendMessage);
$('message-text').addEventListener('keydown',event=>{if(event.key==='Enter' && !event.shiftKey && !event.isComposing) sendMessage(event);});
$('token-form').addEventListener('submit',event=>{event.preventDefault();const value=$('token-input').value.trim();if(!value) return;storeToken(value);$('token-input').value='';$('message-status').textContent='';loadDetail(false);});
function filtersChanged() { state.events=[];state.before=null;route();refresh(); }
$('filters').addEventListener('submit',event=>event.preventDefault());
$('nav-search').addEventListener('toggle',()=>{if($('nav-search').open) $('search').focus();});
$('nav-search').addEventListener('keydown',event=>{if(event.key==='Escape') {event.preventDefault();$('nav-search').open=false;$('nav-search').querySelector('summary').focus();}});
$('search').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(filtersChanged,250);});
for (const id of ['host','project','agent','period','heartbeats']) $(id).addEventListener('change',filtersChanged);
$('period').addEventListener('change',loadHistory);
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{state.view=button.dataset.view;state.filter='';filtersChanged();}));
document.querySelectorAll('[data-state]').forEach(button=>button.addEventListener('click',()=>{state.view='now';state.filter=state.filter===button.dataset.state?'':button.dataset.state;route();render();}));
$('clear-state').addEventListener('click',()=>{state.filter='';route();render();});
$('resume-live').addEventListener('click',()=>{state.events=[];refresh();});
$('load-more').addEventListener('click',loadMore);
$('close-detail').addEventListener('click',()=>$('session-dialog').close());
$('session-dialog').addEventListener('close',()=>{state.selected=null;++state.detailRequest;route();});
$('session-dialog').addEventListener('click',event=>{if(event.target===$('session-dialog') && event.clientX<$('session-dialog').getBoundingClientRect().left) $('session-dialog').close();});
function readRoute() {
  const params=new URLSearchParams(location.hash.slice(1));
  // One-tap setup: any #...&token=... link stores the token and vanishes from the URL (fragments
  // never reach the server, so it leaves no trail) — no typing required on phones.
  if (params.get('token')) { storeToken(params.get('token')); history.replaceState(null,'',location.pathname); }
  state.view=params.get('view')==='timeline'?'timeline':'now';state.filter=ACTIVE.includes(params.get('state'))?params.get('state'):'';
  $('search').value=params.get('search')||'';
  const horizon=params.get('period')||'3600';
  $('period').value=[...$('period').options].some(option=>option.value===horizon)?horizon:'3600';
  for (const id of ['host','project','agent']) {
    const value=params.get(id)||'';
    if (value && ![...$(id).options].some(option=>option.value===value)) $(id).append(new Option(value,value));
    $(id).value=value;
  }
  $('heartbeats').checked=params.get('heartbeats')==='1';
  if (params.get('source') && params.get('session')) openSession({source:params.get('source'),session:params.get('session')});
  else if ($('session-dialog').open) $('session-dialog').close();
  state.events=[];refresh();
}
window.addEventListener('hashchange',readRoute);
const historyPaused=()=>state.view==='timeline' && state.events.length>PAGE;
// The journal pushes a signal whenever anything changes; bursts of hook events collapse into one refetch.
let streamTimer;
function connectStream() {
  if (!window.EventSource) return;
  const stream=new EventSource('/stream');
  stream.onopen=()=>{state.live=true;checkVersion();};
  stream.onerror=()=>{state.live=false;};
  stream.onmessage=()=>{
    clearTimeout(streamTimer);
    streamTimer=setTimeout(()=>{if (!document.hidden && !historyPaused()) refresh();},150);
  };
}
document.addEventListener('visibilitychange',()=>{if (!document.hidden) {checkVersion();if (!historyPaused()) refresh();}});
setInterval(()=>{
  renderResources(state.resources);
  if (!state.versionChecked || Date.now()-state.versionChecked>=60000) checkVersion();
  if (!document.hidden && state.historyAt && Date.now()-state.historyAt>=60000) loadHistory();
  document.querySelectorAll('time[data-at]').forEach(el=>{el.textContent=relative(el.dataset.at);});
  if (document.hidden || historyPaused() || state.refreshing) return;
  // Poll while the stream is down; when live, a slow refresh still catches lease expiry, which no event announces.
  if (!state.live || Date.now()-state.synced>=60000) refresh();
},5000);
renderResources();
connectStream();
readRoute();
