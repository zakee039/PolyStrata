import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import "./style.css";

type Prefs={autostart:boolean;auto_load_model:boolean;watchdog:boolean;config_path:string;project_path:string;port:number};
type Model={name:string;path:string;model:string;context:number|null;quantization:string|null;engine:string|null;tokenizer:string|null;draft_model:string|null;vision_projector:string|null;kv_type:string|null;vram_reserve_mib:number|null;idle_unload_s:number|null;conversation_cache_mib:string|null};
type Metrics={engine?:Record<string,unknown>;live?:Record<string,unknown>;hardware?:Record<string,unknown>;totals?:Record<string,number>;requests?:Array<Record<string,number>>;conversation_cache?:{requests?:number;requests_reused?:number;reused_tokens?:number}};
type Status={phase:string;managed:boolean;desired:boolean;error:string;restarts:number;settings:Prefs;metrics:Metrics|null;host_gpu?:Record<string,number>|null};
let prefs:Prefs|null=null, status:Status|null=null, models:Model[]=[], tab="overview", busy=false, toast="";
const root=document.getElementById("app")!;
const esc=(x:unknown)=>String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]||c));
const fmt=(v:unknown,d=0)=>typeof v==="number"&&Number.isFinite(v)?v.toLocaleString("zh-CN",{maximumFractionDigits:d}):"—";
const short=(v:unknown)=>typeof v==="number"?(v>=1e6?(v/1e6).toFixed(2)+"M":v>=1e3?(v/1e3).toFixed(1)+"K":String(Math.round(v))):"—";
const gb=(v:unknown)=>typeof v==="number"?(v/1024**3).toFixed(1):"—";
const label=(p:string)=>p.split(/[\\/]/).pop()||p;
const names:Record<string,string>={overview:"总览",models:"配置文件",workflows:"工作流",settings:"设置"};
const activeConfig=()=>models.find(m=>m.path.toLowerCase()===prefs?.config_path.toLowerCase());
const ctxText=(n:number|null|undefined)=>typeof n==="number" ? n.toLocaleString("zh-CN")+" Token" : "未指定";
const phases:Record<string,[string,string]>={idle:["运行中","good"],reading:["Prefill","good"],generating:["生成中","good"],unloaded:["空闲卸载","warn"],starting:["启动中","warn"],stopping:["停止中","warn"],stopped:["已停止","dim"],external:["外部进程","warn"],error:["启动失败","bad"]};
function mark(s:string){toast=s;const e=document.getElementById("notice");if(e)e.textContent=s;}
function badge(){const [s,c]=phases[status?.phase||"stopped"]||["异常","bad"];return `<span class="badge ${c}"><i></i>${s}</span>`;}
function render(){
 root.innerHTML=`<header class="head"><div class="brand"><div class="brand-symbol"><b>P</b></div><div><h1>PolyStrata</h1><small>LOCAL MODEL MANAGER</small></div></div><span id="phase">${badge()}</span></header>
 <nav class="tabs">${Object.keys(names).map(t=>`<button class="tab ${tab===t?"selected":""}" data-tab="${t}">${names[t]}</button>`).join("")}</nav>
 <main id="content"></main><footer><span id="notice">${esc(toast||"本机服务管理 · Windows")}</span><span>v0.1.2</span></footer>`;
 document.querySelectorAll<HTMLButtonElement>("[data-tab]").forEach(b=>b.onclick=()=>{tab=b.dataset.tab!;render();});
 content();
}
function metric(title:string,val:string,unit:string,sub:string){return `<div class="metric"><div class="hint-label">${title}</div><div class="figure">${val}<small>${unit}</small></div><div class="metric-sub">${sub}</div></div>`;}
function overview(){
 const m=status?.metrics, t=m?.totals||{}, hw=m?.hardware||status?.host_gpu||{}, r=m?.requests?.[0];
 const live=m?.live||{}, owned=!!status?.managed, phase=status?.phase||"stopped";
 const cache=m?.conversation_cache, checked=cache?.requests||0, hits=cache?.requests_reused||0;
 const hitPct=checked>0?Math.min(100,Math.max(0,100*hits/checked)):0;
 const prefill=live.state==="reading"?live.prefill_tok_s_mean:(r?.prompt_ms?((r.prompt_tokens||0)-(r.reused||0))*1000/r.prompt_ms:null);
 const decode=live.state==="generating"?live.tok_s:r?.decode_tok_s;
 const selected=activeConfig(), model=selected?.name|| (typeof m?.engine?.model==="string"?m.engine.model:"未找到当前配置");
 const shownModel=String(model).startsWith("G4-MeroMero-26B-A4B")?"G4-MeroMero 26B · "+(selected?.quantization||"Q4_K_M"):String(model);
 const currentFile=label(prefs?.config_path||"");
 const modelHint=phase==="error"?(status?.error||"启动异常，请查看运行日志"):phase==="external"?"检测到外部服务，请先停止旧版批处理再由 Manager 启动":phase==="unloaded"?"GPU / RAM 已自动释放；API 保持在线":owned?"由 Manager 托管 · 127.0.0.1:"+prefs?.port:"服务未启动 · 127.0.0.1:"+prefs?.port;
 return `<section><div class="model-card"><div class="model-head">当前配置文件 <button class="chip chip-action" id="home-config-detail" ${selected?'':'disabled'}>查看详情</button></div>
   <h2 title="${esc(model)}">${esc(shownModel)}</h2>
   <div class="current-config-meta"><span title="${esc(prefs?.config_path)}">${esc(currentFile)}</span><span>${selected?.context?esc(ctxText(selected.context)):"上下文未指定"}</span></div>
   <p>${esc(modelHint)}</p></div>
 <div class="metrics">
 ${metric("GPU 显存",gb(hw.gpu_mem_used)+" <em>/ "+gb(hw.gpu_mem_total)+"</em>","GB","整卡占用 · 独立监控")}
 ${metric("Decode",fmt(decode,1),"tok/s",live.state==="generating"?"实时吐字":"上次请求")}
 ${metric("Prefill",fmt(prefill),"tok/s",live.state==="reading"?"正在处理输入":"上次请求")}
 ${metric("Token 统计",m?short((t.prompt_tokens||0)+(t.output_tokens||0)):"—","", "输入 "+short(t.prompt_tokens)+" / 输出 "+short(t.output_tokens))}
 </div>
 <div class="cache" title="最近最多 500 次已完成请求中，至少复用 1 个 Token 的请求占比"><div class="cache-title"><b>缓存命中率</b><strong>${checked?hitPct.toFixed(1)+"%":"—"}</strong></div>
  <div class="track"><div style="width:${hitPct}%" class="fill"></div></div>
  <div class="note"><span>近 ${checked||"—"} 次请求，命中 ${checked?hits:"—"} 次</span><span>已复用 ${short(t.reused)} Token</span></div></div>
 <div class="actions"><button id="btn-service" class="primary" ${busy||phase==="starting"||phase==="stopping"||phase==="external"?"disabled":""}>${owned?"■ 停止服务":"▶ 启动服务"}</button><button class="secondary" id="btn-chat">打开 Chat ↗</button></div>
 <div class="subactions"><button id="btn-load" ${!owned||phase!=="unloaded"?"disabled":""}>立即加载模型</button><button id="btn-log">查看运行日志</button></div></section>`;
}
function modelPage(){
 return `<section><div class="intro"><h2>配置文件</h2><p>一个模型可以有不同的上下文和资源配置。切换前需要停止服务。</p></div>
 <div class="model-list">${models.map(m=>{
  const selected=m.path.toLowerCase()===prefs?.config_path.toLowerCase();
  return `<div class="model-item ${selected?"is-selected":""}">
   <div class="model-text">
    <b title="${esc(m.path)}">${esc(label(m.path))}</b>
    <small title="${esc(m.name)}">${esc(m.name)}</small>
    <span class="config-context">${esc(ctxText(m.context))} ${m.quantization?" · "+esc(m.quantization):""}</span>
   </div>
   <div class="config-actions">
    <button class="view-config" data-view-config="${esc(encodeURIComponent(m.path))}">查看</button>
    <button class="choice ${selected?"active":""}" data-model="${esc(encodeURIComponent(m.path))}" ${selected||status?.managed?"disabled":""}>${selected?"使用中":"使用"}</button>
   </div></div>`;
 }).join("")||'<div class="empty">没有发现有效的部署配置</div>'}</div>
 <div class="setting-card deployment-card">
  <h3>部署连接</h3>
  <label class="field-label" for="root-field">PolyStrata 部署目录</label>
  <input class="field" id="root-field" value="${esc(prefs?.project_path)}" spellcheck="false">
  <label class="field-label" for="path-field">当前配置文件 JSON 路径</label>
  <input class="field" id="path-field" value="${esc(prefs?.config_path)}" spellcheck="false">
  <label class="field-label" for="port-field">API 端口</label>
  <input class="field short-field" id="port-field" type="number" min="1024" max="65535" value="${prefs?.port||18081}">
  <button class="mini-primary" id="save-path" ${status?.managed?"disabled":""}>保存部署配置</button>
  <p class="hint">路径、端口和当前配置文件只在此页管理；服务运行时不允许切换。</p>
 </div>
 </section>`;
}
function workflowPage(){return `<section><div class="intro"><h2>Workflow Prefix Cache</h2><p>为每个模块注册固定提示词并预热 KV。</p></div>
 <div class="placeholder"><div class="placeholder-icon">◇</div><b>工作流功能预留</b><p>Miku Harness 将通过注册接口预计算前缀；后续调用自动复用缓存。当前尚未部署工作流 API。</p>
 <div class="checkline">✓ 工作流版本与 Token 指纹</div><div class="checkline">✓ 独立 KV 存储与调度</div><div class="checkline">✓ Miku Harness 注册并预热</div>
 <div class="planned">设计阶段 · 现在不会创建任何缓存</div></div></section>`;}
function toggle(key:keyof Prefs,name:string,desc:string){return `<label class="toggle-row"><div><b>${name}</b><small>${desc}</small></div><input type="checkbox" data-setting="${key}" ${prefs?.[key]?"checked":""}><span class="switch"></span></label>`;}
function settingsPage(){return `<section class="setting-page"><div class="intro"><h2>设置</h2><p>启动行为与服务守护</p></div>
 <div class="setting-card"><h3>启动与守护</h3>
 ${toggle("autostart","开机自启","登录 Windows 时启动托盘管理器")}
 ${toggle("auto_load_model","启动后自动加载模型","管理器启动后，自动启动服务并加载权重")}
 ${toggle("watchdog","服务守护","服务意外退出时自动重启，最多三次")}
 </div><div class="setting-card"><h3>管理说明</h3><p class="hint">模型路径、上下文窗口、资源参数和 API 端口已集中到「配置文件」页。开机自启只启动 Manager，是否自动加载模型由上方选项决定。</p></div></section>`;}
function content(){
 const el=document.getElementById("content")!;
 el.innerHTML=tab==="overview"?overview():tab==="models"?modelPage():tab==="workflows"?workflowPage():settingsPage();
 document.getElementById("btn-service")?.addEventListener("click",()=>void action(status?.managed?"stop_service":"start_service",status?.managed?"服务已停止，KV 历史已保留":"已发送启动指令"));
 document.getElementById("btn-chat")?.addEventListener("click",()=>void invoke("open_chat").catch(e=>mark(String(e))));
 document.getElementById("btn-load")?.addEventListener("click",()=>void action("load_model","模型加载成功"));
 document.getElementById("btn-log")?.addEventListener("click",()=>void showLogs());
 document.getElementById("home-config-detail")?.addEventListener("click",()=>{const c=activeConfig();if(c)showConfig(c);});
 document.querySelectorAll<HTMLButtonElement>("[data-view-config]").forEach(b=>b.onclick=()=>{
  const c=models.find(m=>m.path===decodeURIComponent(b.dataset.viewConfig!));if(c)showConfig(c);
 });
 document.querySelectorAll<HTMLButtonElement>("[data-model]").forEach(b=>b.onclick=()=>{if(!prefs)return;void save({...prefs,config_path:decodeURIComponent(b.dataset.model!)});});
 document.querySelectorAll<HTMLInputElement>("[data-setting]").forEach(b=>b.onchange=()=>{if(!prefs)return;const k=b.dataset.setting as "autostart"|"auto_load_model"|"watchdog";void save({...prefs,[k]:b.checked});});
 document.getElementById("save-path")?.addEventListener("click",()=>{if(!prefs)return;const config_path=(document.getElementById("path-field") as HTMLInputElement).value.trim();
 const project_path=(document.getElementById("root-field") as HTMLInputElement).value.trim();
 const port=Number((document.getElementById("port-field") as HTMLInputElement).value);
 if(!Number.isInteger(port)||port<1024||port>65535){mark("端口必须是 1024–65535 之间的整数");return;}void save({...prefs,port,config_path,project_path});});
}
async function save(v:Prefs){try{prefs=await invoke<Prefs>("save_settings",{incoming:v});mark("设置已保存");await refreshModels();await poll();content();}catch(e){mark("保存失败："+String(e));content();}}
async function action(name:string,done:string){if(busy)return;busy=true;content();try{await invoke(name);mark(done);}catch(e){mark("操作失败："+String(e));}finally{busy=false;await poll();content();}}
async function refreshModels(){try{models=await invoke<Model[]>("list_models");}catch(e){console.warn(e);}}
async function poll(){try{status=await invoke<Status>("get_status");if(!prefs)prefs=status.settings;
 const el=document.getElementById("phase");if(el)el.innerHTML=badge();
 if(tab==="overview"&&!busy)content();
 }catch(e){mark("状态获取失败："+String(e));}}
function showConfig(config:Model){
 const row=(name:string,value:string|null|undefined)=>`<div class="detail-row"><span>${esc(name)}</span><strong title="${esc(value||"未设置")}">${esc(value||"未设置")}</strong></div>`;
 const idle=typeof config.idle_unload_s==="number" ? (config.idle_unload_s===0?"已关闭":config.idle_unload_s+" 秒（"+Math.round(config.idle_unload_s/60)+" 分钟）") : "未在配置中指定";
 const rows=[
  ["模型名称",config.name],["模型权重",config.model],["上下文窗口",ctxText(config.context)],
  ["量化格式",config.quantization],["KV 精度",config.kv_type],
  ["推理引擎",config.engine],["Tokenizer",config.tokenizer],
  ["视觉投影文件",config.vision_projector],["草稿模型",config.draft_model],
  ["显存预留",typeof config.vram_reserve_mib==="number"?config.vram_reserve_mib+" MiB":null],
  ["RAM 对话缓存",config.conversation_cache_mib?(config.conversation_cache_mib==="auto"?"自动预算":config.conversation_cache_mib+" MiB"):null],
  ["空闲自动卸载",idle],["配置文件完整路径",config.path]
 ] as Array<[string,string|null|undefined]>;
 const bg=document.createElement("div");bg.className="modal-backdrop";
 bg.innerHTML=`<div class="modal config-modal" role="dialog" aria-modal="true" aria-label="配置文件详情">
 <div class="modal-top"><div><strong>配置文件详情</strong><small>${esc(label(config.path))}</small></div><button id="close-config" aria-label="关闭">×</button></div>
 <div class="detail-list">${rows.map(r=>row(r[0],r[1])).join("")}</div>
 </div>`;
 document.body.appendChild(bg);
 const close=()=>bg.remove();
 bg.querySelector("#close-config")?.addEventListener("click",close);
 bg.addEventListener("click",e=>{if(e.target===bg)close();});
}
async function showLogs(){const bg=document.createElement("div");bg.className="modal-backdrop";bg.innerHTML='<div class="modal"><div class="modal-top"><strong>运行日志</strong><button id="close-log">×</button></div><pre id="log-text">正在读取…</pre></div>';document.body.appendChild(bg);
 const close=()=>bg.remove();document.getElementById("close-log")!.addEventListener("click",close);
 bg.addEventListener("click",e=>{if(e.target===bg)close();});
 try{const raw=await invoke<string>("get_log");const el=document.getElementById("log-text")!;el.textContent=raw||"暂无日志";el.scrollTop=el.scrollHeight;}catch(e){document.getElementById("log-text")!.textContent=String(e);}}
async function init(){try{prefs=await invoke<Prefs>("get_settings");await refreshModels();}catch(e){toast="初始化异常："+String(e);}
 render();
 void listen<string>("manager-error", event => {
   mark("服务管理异常：" + String(event.payload));
   void poll();
 }).catch(console.error);
 await poll();setInterval(()=>void poll(),2000);}
void init();
