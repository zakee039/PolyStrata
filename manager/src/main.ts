import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import "./style.css";

type Prefs={autostart:boolean;auto_load_model:boolean;watchdog:boolean;lan_access:boolean;cf_enabled:boolean;cf_binary:string;cf_hostname:string;config_path:string;project_path:string;port:number};
type Model={name:string;path:string;model:string;context:number|null;quantization:string|null;engine:string|null;tokenizer:string|null;draft_model:string|null;vision_projector:string|null;kv_type:string|null;vram_reserve_mib:number|null;idle_unload_s:number|null;conversation_cache_mib:string|null};
type Metrics={engine?:Record<string,unknown>;live?:Record<string,unknown>;hardware?:Record<string,unknown>;totals?:Record<string,number>;requests?:Array<Record<string,number>>;conversation_cache?:{requests?:number;requests_reused?:number;reused_tokens?:number}};
type Status={phase:string;managed:boolean;desired:boolean;error:string;restarts:number;settings:Prefs;metrics:Metrics|null;host_gpu?:Record<string,number>|null;cf_running:boolean;cf_error:string;cf_token_present:boolean;api_key_present:boolean};
let prefs:Prefs|null=null, status:Status|null=null, models:Model[]=[], tab="overview", busy=false, toast="";
const root=document.getElementById("app")!;
const esc=(x:unknown)=>String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]||c));
const fmt=(v:unknown,d=0)=>typeof v==="number"&&Number.isFinite(v)?v.toLocaleString("zh-CN",{maximumFractionDigits:d}):"—";
const short=(v:unknown)=>typeof v==="number"?(v>=1e6?(v/1e6).toFixed(2)+"M":v>=1e3?(v/1e3).toFixed(1)+"K":String(Math.round(v))):"—";
const gb=(v:unknown)=>typeof v==="number"?(v/1024**3).toFixed(1):"—";
const label=(p:string)=>p.split(/[\\/]/).pop()||p;
const names:Record<string,string>={overview:"总览",api:"API",workflows:"工作流",settings:"设置"};
const activeConfig=()=>models.find(m=>m.path.toLowerCase()===prefs?.config_path.toLowerCase());
const ctxText=(n:number|null|undefined)=>typeof n==="number" ? n.toLocaleString("zh-CN")+" Token" : "未指定";
const phases:Record<string,[string,string]>={idle:["运行中","good"],reading:["Prefill","good"],generating:["生成中","good"],unloaded:["空闲卸载","warn"],starting:["启动中","warn"],stopping:["停止中","warn"],stopped:["已停止","dim"],external:["外部进程","warn"],error:["启动失败","bad"]};
function mark(s:string){toast=s;const e=document.getElementById("notice");if(e)e.textContent=s;}
function badge(){const [s,c]=phases[status?.phase||"stopped"]||["异常","bad"];return `<span class="badge ${c}"><i></i>${s}</span>`;}
function render(){
 root.innerHTML=`<header class="head"><div class="brand"><div class="brand-symbol"><b>P</b></div><div><h1>PolyStrata</h1><small>LOCAL MODEL MANAGER</small></div></div><span id="phase">${badge()}</span></header>
 <nav class="tabs">${Object.keys(names).map(t=>`<button class="tab ${tab===t?"selected":""}" data-tab="${t}">${names[t]}</button>`).join("")}</nav>
 <main id="content"></main><footer><span id="notice">${esc(toast||"本机服务管理 · Windows")}</span><span>v0.1.3</span></footer>`;
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
 return `<section><div class="model-card"><div class="model-head"><span>当前配置文件</span><div class="config-head-actions"><button id="choose-config" class="chip chip-action">选择配置文件 ▾</button><button class="chip chip-action" id="home-config-detail" ${selected?'':'disabled'}>查看</button></div></div>
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
 return `<div class="config-picker-intro"><p>选择已注册的部署配置。模型、上下文和资源参数均从 JSON 自动读取。</p></div>
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
 <div class="config-picker-bottom"><button id="register-config" class="mini-primary">＋ 注册配置文件</button>
 <p class="hint">注册只保存 JSON 文件路径，不复制模型。服务运行时不允许切换正在使用的配置。</p></div>`;
}
function workflowPage(){return `<section><div class="intro"><h2>Workflow Prefix Cache</h2><p>为每个模块注册固定提示词并预热 KV。</p></div>
 <div class="placeholder"><div class="placeholder-icon">◇</div><b>工作流功能预留</b><p>Miku Harness 将通过注册接口预计算前缀；后续调用自动复用缓存。当前尚未部署工作流 API。</p>
 <div class="checkline">✓ 工作流版本与 Token 指纹</div><div class="checkline">✓ 独立 KV 存储与调度</div><div class="checkline">✓ Miku Harness 注册并预热</div>
 <div class="planned">设计阶段 · 现在不会创建任何缓存</div></div></section>`;}
function toggle(key:keyof Prefs,name:string,desc:string){return `<label class="toggle-row"><div><b>${name}</b><small>${desc}</small></div><input type="checkbox" data-setting="${key}" ${prefs?.[key]?"checked":""}><span class="switch"></span></label>`;}
function apiPage(){return `<section class="setting-page api-page"><div class="intro"><h2>API 接口</h2><p>端口、安全密钥与访问方式</p></div>
 <div class="setting-card"><h3>服务端口</h3>
 <label class="field-label" for="port-field">API 端口</label>
 <div class="port-actions"><input class="field short-field" id="port-field" type="number" min="1024" max="65535" value="${prefs?.port||18081}">
 <button class="mini-primary" id="save-api-port" ${status?.managed?"disabled":""}>保存端口</button></div>
 <p class="hint">本机地址：127.0.0.1:${prefs?.port||18081}；修改端口前需停止服务。修改后的端口也用于 Cloudflare Tunnel 回源。</p>
 </div>
 <div class="setting-card"><h3>API Key 安全管理</h3>
 <div class="setting-info"><span>密钥状态</span><strong>${status?.api_key_present?"已生成 · Windows 加密保存":"未生成，首次启动会自动生成"}</strong></div>
 <div class="secret-row"><input id="api-key-field" class="field secret-input" type="password" readonly value="" placeholder="点击「查看」显示 API Key" autocomplete="off"><button id="reveal-key" class="outline-small">查看</button></div>
 <div class="setting-buttons"><button id="edit-key" class="outline-small">更换</button><button id="generate-key" class="outline-small">生成新 Key</button><button id="copy-key" class="outline-small" disabled>复制</button></div>
 <div id="edit-key-actions" hidden><button id="confirm-key" class="mini-primary">保存新 API Key</button><button id="cancel-key" class="outline-small">取消</button></div>
 <p class="hint">API Key 仅在点击查看时显示，使用 Windows DPAPI 加密保存。更换需先停止服务；本地 Chat 也需要填写此 Key。</p>
 </div>
 <div class="setting-card"><h3>网络访问</h3>
 ${toggle("lan_access","局域网访问","监听 0.0.0.0；仅建议在可信局域网内使用")}
 <div class="warning">局域网连接使用 HTTP，API Key 在传输时不加密。请勿将此端口直接映射到公网；Windows 防火墙规则需要单独配置。</div>
 </div>
 <div class="setting-card"><h3>Cloudflare Tunnel</h3>
 ${toggle("cf_enabled","启用命名 Tunnel","由 Manager 托管 cloudflared；仍然使用 API Key 鉴权")}
 <div class="setting-info"><span>连接器进程</span><strong>${status?.cf_running?"运行中（不代表公网已可访问）":status?.cf_error?"异常："+esc(status.cf_error):prefs?.cf_enabled?"等待启动服务":"未启用"}</strong></div>
 <label class="field-label" for="cf-binary">cloudflared 可执行文件</label>
 <input class="field" id="cf-binary" spellcheck="false" value="${esc(prefs?.cf_binary||"cloudflared.exe")}" placeholder="cloudflared.exe 或完整路径">
 <label class="field-label" for="cf-hostname">公开域名（展示用，须在 Cloudflare 后台配置）</label>
 <input class="field" id="cf-hostname" spellcheck="false" value="${esc(prefs?.cf_hostname)}" placeholder="如 llm.example.com">
 <label class="field-label" for="cf-token">Tunnel Token</label>
 <input type="password" id="cf-token" class="field" autocomplete="off" value="" placeholder="${status?.cf_token_present?"Token 已保存；留空表示不修改":"粘贴从 Cloudflare 获取的 eyJ... Token"}">
 <div class="setting-buttons"><button id="save-cf-token" class="outline-small">保存 Token</button><button id="save-cf-details" class="mini-primary">保存 Tunnel 设置</button></div>
 <p class="hint">Cloudflare 控制台的 Public Hostname → Service 请填 <strong>http://127.0.0.1:${prefs?.port||18081}</strong>。此处保存的域名不会自动修改 Cloudflare DNS。Token 加密保存，不显示明文。</p>
 <p class="hint">Tunnel 启用后，下次启动服务时自动启动；停止服务时一起停止。无需启用“局域网访问”，避免多余的端口暴露。</p>
 </div>
 </section>`;}
function settingsPage(){return `<section class="setting-page"><div class="intro"><h2>设置</h2><p>启动行为与服务守护</p></div>
 <div class="setting-card"><h3>启动与守护</h3>
 ${toggle("autostart","开机自启","登录 Windows 时启动托盘管理器")}
 ${toggle("auto_load_model","启动后自动加载模型","启动 Manager 后自动载入模型")}
 ${toggle("watchdog","服务守护","异常退出最多自动重启三次")}
 </div></section>`;}
function content(){
 const el=document.getElementById("content")!;
 el.innerHTML=tab==="overview"?overview():tab==="api"?apiPage():tab==="workflows"?workflowPage():settingsPage();
 document.getElementById("btn-service")?.addEventListener("click",()=>void action(status?.managed?"stop_service":"start_service",status?.managed?"服务已停止，KV 历史已保留":"已发送启动指令"));
 document.getElementById("btn-chat")?.addEventListener("click",()=>void invoke("open_chat").catch(e=>mark(String(e))));
 document.getElementById("btn-load")?.addEventListener("click",()=>void action("load_model","模型加载成功"));
 document.getElementById("btn-log")?.addEventListener("click",()=>void showLogs());
 document.getElementById("home-config-detail")?.addEventListener("click",()=>{const c=activeConfig();if(c)showConfig(c);});
 document.getElementById("choose-config")?.addEventListener("click",()=>void openConfigPicker());

 document.querySelectorAll<HTMLInputElement>("[data-setting]").forEach(b=>b.onchange=()=>{if(!prefs)return;const k=b.dataset.setting as "autostart"|"auto_load_model"|"watchdog"|"lan_access"|"cf_enabled";void save({...prefs,[k]:b.checked});});

 document.getElementById("reveal-key")?.addEventListener("click",async()=>{
   const field=document.getElementById("api-key-field") as HTMLInputElement;
   const btn=document.getElementById("reveal-key") as HTMLButtonElement;
   if(field.type==="text" && field.readOnly){field.value="";field.type="password";btn.textContent="查看";(document.getElementById("copy-key") as HTMLButtonElement).disabled=true;return;}
   try{field.value=await invoke<string>("reveal_api_key");field.type="text";btn.textContent="隐藏";(document.getElementById("copy-key") as HTMLButtonElement).disabled=false;}
   catch(e){mark("读取 API Key 失败："+String(e));}
 });
 document.getElementById("edit-key")?.addEventListener("click",()=>{
   const field=document.getElementById("api-key-field") as HTMLInputElement;
   field.readOnly=false;field.type="text";field.value="";field.placeholder="输入至少 24 位新 API Key";field.focus();
   (document.getElementById("edit-key-actions") as HTMLElement).hidden=false;
 });
 document.getElementById("cancel-key")?.addEventListener("click",()=>{content();});
 document.getElementById("confirm-key")?.addEventListener("click",async()=>{
   const field=document.getElementById("api-key-field") as HTMLInputElement;
   try{await invoke("replace_api_key",{newKey:field.value});mark("API Key 已更新；请为所有客户端更新密钥");content();}
   catch(e){mark("更换失败："+String(e));}
 });
 document.getElementById("generate-key")?.addEventListener("click",async()=>{
   if(!window.confirm("生成新 API Key 将使旧 Key 在下次启动服务后失效。确定更换吗？"))return;
   try{const key=await invoke<string>("generate_api_key");mark("已生成新 Key，请更新客户端");content();
   const field=document.getElementById("api-key-field") as HTMLInputElement;field.type="text";field.value=key;
   (document.getElementById("reveal-key") as HTMLButtonElement).textContent="隐藏";
   (document.getElementById("copy-key") as HTMLButtonElement).disabled=false;
   }catch(e){mark("生成失败："+String(e));}
 });
 document.getElementById("copy-key")?.addEventListener("click",async()=>{
   const field=document.getElementById("api-key-field") as HTMLInputElement;
   if(!field.value)return;
   try{await navigator.clipboard.writeText(field.value);mark("API Key 已复制到剪贴板");}
   catch(e){field.select();mark("请按 Ctrl+C 复制 Key："+String(e));}
 });
 document.getElementById("save-cf-token")?.addEventListener("click",async()=>{
   const field=document.getElementById("cf-token") as HTMLInputElement;
   if(!field.value.trim()){mark("请先填写 Tunnel Token");return;}
   try{await invoke("save_tunnel_token",{token:field.value});field.value="";mark("Tunnel Token 已加密保存");await poll();content();}
   catch(e){mark("Token 保存失败："+String(e));}
 });
 document.getElementById("save-cf-details")?.addEventListener("click",()=>{
   if(!prefs)return;
   const cf_binary=(document.getElementById("cf-binary") as HTMLInputElement).value.trim();
   const cf_hostname=(document.getElementById("cf-hostname") as HTMLInputElement).value.trim();
   void save({...prefs,cf_binary,cf_hostname});
 });
 document.getElementById("save-api-port")?.addEventListener("click",()=>{
  if(!prefs)return;
  const port=Number((document.getElementById("port-field") as HTMLInputElement).value);
  if(!Number.isInteger(port)||port<1024||port>65535){mark("端口必须是 1024–65535 之间的整数");return;}
  void save({...prefs,port});
 });
}
async function save(v:Prefs){try{prefs=await invoke<Prefs>("save_settings",{incoming:v});mark("设置已保存");await refreshModels();await poll();content();}catch(e){mark("保存失败："+String(e));content();}}
async function action(name:string,done:string){if(busy)return;busy=true;content();try{await invoke(name);mark(done);}catch(e){mark("操作失败："+String(e));}finally{busy=false;await poll();content();}}
async function refreshModels(){try{models=await invoke<Model[]>("list_models");}catch(e){console.warn(e);}}
async function poll(){try{status=await invoke<Status>("get_status");if(!prefs)prefs=status.settings;
 const el=document.getElementById("phase");if(el)el.innerHTML=badge();
 if(tab==="overview"&&!busy)content();
 }catch(e){mark("状态获取失败："+String(e));}}
async function openConfigPicker(){
 await refreshModels();
 const bg=document.createElement("div");bg.className="modal-backdrop";
 bg.innerHTML=`<div class="modal config-picker-modal" role="dialog" aria-modal="true" aria-label="选择配置文件">
 <div class="modal-top"><div><strong>选择配置文件</strong><small>已注册 ${models.length} 个部署配置</small></div><button id="close-picker" aria-label="关闭">×</button></div>
 <div class="config-picker-scroll">${modelPage()}</div></div>`;
 document.body.appendChild(bg);
 const close=()=>bg.remove();
 bg.querySelector("#close-picker")?.addEventListener("click",close);
 bg.addEventListener("click",e=>{if(e.target===bg)close();});
 bg.querySelectorAll<HTMLButtonElement>("[data-view-config]").forEach(b=>b.onclick=()=>{
  const c=models.find(m=>m.path===decodeURIComponent(b.dataset.viewConfig!));if(c)showConfig(c);
 });
 bg.querySelectorAll<HTMLButtonElement>("[data-model]").forEach(b=>b.onclick=async()=>{
  if(!prefs)return;
  try {
   prefs=await invoke<Prefs>("save_settings",{incoming:{...prefs,config_path:decodeURIComponent(b.dataset.model!)}});
   close();await refreshModels();await poll();content();mark("已切换配置文件，下次启动服务将使用所选配置");
  } catch(e){mark("切换配置失败："+String(e));}
 });
 bg.querySelector("#register-config")?.addEventListener("click",async()=>{
  try{
   const path=await open({multiple:false,directory:false,filters:[{name:"PolyStrata JSON 配置",extensions:["json"]}]});
   if(typeof path!=="string")return;
   const result=await invoke<Model>("register_config",{path});
   close();await refreshModels();void openConfigPicker();
   mark("已注册配置文件："+label(result.path));
  }catch(e){mark("注册失败："+String(e));}
 });
}
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
