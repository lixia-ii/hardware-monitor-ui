"use strict";
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = (v, suffix = "", digits = 0) => typeof v === "number" && Number.isFinite(v) ? v.toFixed(digits) + suffix : "—";
const rate = v => { if (v == null) return "—"; let i = 0; const units = ["B/s","KB/s","MB/s","GB/s"]; while (v >= 1024 && i < 3) {v /= 1024; i++;} return num(v, " " + units[i], i ? 1 : 0); };
const memory = (used, total) => total ? `${num(used / 1024 ** 3, "", 1)} / ${num(total / 1024 ** 3, " GB", 1)}` : "—";
const duration = v => v == null ? "—" : `${Math.floor(v/86400) ? Math.floor(v/86400)+"天 " : ""}${Math.floor(v%86400/3600)}时 ${Math.floor(v%3600/60)}分`;
const clock = t => new Date(t * 1000).toLocaleTimeString("zh-CN", {hour12:false});
const clone = v => JSON.parse(JSON.stringify(v));
const pages = {overview:["总览","运行总览","掌握电脑与设备的每一刻状态。"],devices:["设备","连接的设备","集中查看与管理你的路由器和远程电脑。"],history:["历史","历史趋势","回看最近 24 小时的运行轨迹。"],settings:["设置","偏好与设置","管理采集、告警与显示偏好。"],info:["信息","硬件与诊断","查看硬件概览与最近的告警记录。"]};
const labels = {cpu_temp:"CPU 温度",gpu_temp:"显卡温度",cpu_load:"CPU 利用率",gpu_load:"显卡利用率",mem_load:"内存占用",gpu_mem_load:"显存占用",cpu_fan:"CPU 风扇",gpu_fan:"显卡风扇",net_up:"上传",net_down:"下载"};
let snapshot = null, state = null, page = "overview", tab = "monitor", dirty = false, active = false;
let historyData = null, hours = 1, chartInterval = "auto", chartGpu = "", lastHistory = 0;
let hardware = null, editorDevice = null, editingRevision = null, polling = false, toastTimer;
let access = null, loginTimer = null;
let alertData = null, alertPage = 1, alertRevision = null, alertRequest = 0, alertsLoading = false;

async function api(path, body) {
  const options = {cache:"no-store", headers:{}, signal:AbortSignal.timeout(15000)};
  if (body !== undefined) {options.method = "POST"; options.headers = {"Content-Type":"application/json"}; options.body = JSON.stringify(body);}
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    // Signed out elsewhere (removed on the computer or another device): back to pairing.
    if (response.status === 401 && active) showLogin(null);
    const error = new Error(result.error || "请求失败"); error.status = response.status; throw error;
  }
  return result;
}
function toast(message) {$("#toast").textContent = message; $("#toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $("#toast").hidden = true, 4500);}
function notice(message) {$("#notice").textContent = message; $("#notice").hidden = !message;}
async function start() {
  snapshot = await api("/api/settings");
  active = true; dirty = false;
  await navigate(location.hash.slice(1) || "overview", true);
  poll();
}
async function navigate(next, force = false) {
  next = pages[next] ? next : "overview";
  if (!force && next === page) return;
  if (dirty && !confirm("还有未保存的修改，放弃修改并切换页面？")) {history.replaceState(null,"",`#${page}`); return;}
  dirty = false; page = next; history.replaceState(null,"",`#${page}`);
  $$("[data-page]").forEach(a => {a.classList.toggle("active", a.dataset.page === page); if (a.dataset.page === page) a.setAttribute("aria-current","page"); else a.removeAttribute("aria-current");});
  [$("#page-label").textContent,$("#page-title").textContent,$("#page-description").textContent] = pages[page];
  notice(""); $("#content").innerHTML = '<div class="empty">正在载入…</div>';
  try {
    if (["settings","devices"].includes(page)) snapshot = await api("/api/settings");
    if (page === "history") {lastHistory = 0; await loadHistory();}
    else if (page === "info") {hardware = await api("/api/hardware"); await loadAlerts(1); renderPage();}
    else renderPage();
  } catch (error) {notice(error.message);}
}
function pref(key) {return snapshot?.settings.display[key] || {};}
function title(key, fallback) {return esc(pref(key).name || labels[key] || fallback || key);}
function visible(key) {return pref(key).visible !== false;}
function kv(name, value) {return `<div class="kv"><span>${esc(name)}</span><b>${esc(value)}</b></div>`;}
function metric(key, value, suffix, raw = false) {return visible(key) ? `<div class="kv"><span>${title(key)}</span><b>${raw ? esc(value) : num(value,suffix)}</b></div>` : "";}
function meter(value, blue = false) {return `<div class="meter${blue ? " blue" : ""}"><span style="width:${Math.min(100,Math.max(0,Number(value)||0))}%"></span></div>`;}
function sortedKeys(keys) {return keys.filter(visible).sort((a,b) => (pref(a).order ?? 100) - (pref(b).order ?? 100));}
function sensorGroups(data, prefix = "") {
  const groups = [
    ["case","机箱风扇",(data.case_fans || []).map(([n,v],i) => [n,v," RPM",`sensor:case:${data.sensor_ids?.case_fans?.[i] || "legacy:"+n}`])],
    ["pump","水冷泵",(data.pumps || []).map(([n,v],i) => [n,v," RPM",`sensor:pump:${data.sensor_ids?.pumps?.[i] || "legacy:"+n}`])],
    ...[["storage","硬盘温度","storage"],["memory","内存温度","memory"],["network_temp","网卡温度","network"],["other","其他温度","other"]].map(([key,name,kind]) => [key,name,(data.extra_temps || []).flatMap(([k,n,v],i) => k === kind ? [[n,v," °C",`sensor:${key}:${data.sensor_ids?.extra_temps?.[i] || "legacy:"+n}`]] : [])])
  ];
  return groups.sort((a,b) => (pref(a[0]).order??100)-(pref(b[0]).order??100)).map(([key,name,rows]) => {
    if (!prefix && !visible(key)) return "";
    rows = rows.filter(r => prefix || visible(r[3])).sort((a,b)=>(pref(a[3]).order??100)-(pref(b[3]).order??100));
    if (!rows.length) return "";
    return `<details class="card" data-fold="${esc(prefix+key)}"><summary><h3>${prefix ? esc(name) : title(key,name)}</h3><small>${rows.length} 项</small></summary>${rows.map(([n,v,u,id])=>kv(prefix?n:pref(id).name||n,num(v,u))).join("")}</details>`;
  }).join("");
}
function renderOverview() {
  const d = state?.data || {}, c = $("#content");
  const cpuRows = {cpu_load:metric("cpu_load",d.cpu_load," %")+ (visible("cpu_load")?meter(d.cpu_load):""),cpu_fan:metric("cpu_fan",d.cpu_fan," RPM")};
  const gpuRows = {gpu_load:metric("gpu_load",d.gpu_load," %")+(visible("gpu_load")?meter(d.gpu_load,true):""),gpu_fan:metric("gpu_fan",d.gpu_fan != null?num(d.gpu_fan," RPM"):num(d.gpu_fan_pct," %"),"",true),gpu_mem_load:metric("gpu_mem_load",d.gpu_mem_load," %")};
  const gpuCard = `<article class="card"><div class="card-head"><h2>${visible("gpu_temp")?title("gpu_temp"):"显卡"}</h2><span class="muted" title="${esc(d.gpu_name)}">${esc(d.gpu_name || "GPU")}</span></div>${visible("gpu_temp")?`<div class="hero-row"><div class="hero-number ${d.gpu_temp>=80?"hot":"blue"}">${num(d.gpu_temp)}<small>°C</small></div></div>`:""}${sortedKeys(Object.keys(gpuRows)).map(k=>gpuRows[k]).join("")}${visible("gpu_mem_load")?kv("显存容量",memory(d.gpu_mem_used,d.gpu_mem_total)):""}</article>`;
  const memoryCard = `<article class="card"><div class="card-head"><h2>${title("mem_load")}</h2><span class="muted">RAM</span></div><div class="hero-row"><div class="hero-number">${num(d.mem_load)}<small>%</small></div></div>${meter(d.mem_load)}${kv("已用 / 总量",memory(d.mem_used,d.mem_total))}</article>`;
  const networkCard = `<article class="card"><div class="card-head"><h2>${title("network","网络")}</h2><span class="muted">实时速率</span></div><div class="metric-pair"><div><span class="card-label">↑ 上传</span><div class="hero-number green">${esc(rate(d.net_up))}</div></div><div><span class="card-label">↓ 下载</span><div class="hero-number blue">${esc(rate(d.net_down))}</div></div></div></article>`;
  c.innerHTML = `<div class="grid overview"><article class="card"><div class="card-head"><h2>${visible("cpu_temp")?title("cpu_temp"):"CPU"}</h2><span class="muted">处理器</span></div>${visible("cpu_temp")?`<div class="hero-row"><div class="hero-number ${d.cpu_temp>=80?"hot":"green"}">${num(d.cpu_temp)}<small>°C</small></div></div>`:""}${sortedKeys(Object.keys(cpuRows)).map(k=>cpuRows[k]).join("")}</article>${gpuCard}${visible("mem_load")?memoryCard:""}${visible("network")?networkCard:""}</div><div class="section-title"><h2>传感器详情</h2><span class="muted">按已保存的显示偏好展示</span></div><div class="stack">${sensorGroups(d)||'<div class="card empty">暂无其他可见传感器</div>'}</div><div class="section-title"><h2>连接的设备</h2><a href="#devices" class="muted">管理设备 ↗</a></div><div class="grid">${deviceCards(false)}</div>`;
}
function remoteMetrics(d) {
  return ["cpu_temp","cpu_load","gpu_temp","gpu_load","mem_load","gpu_mem_load","cpu_fan"].map(k=>kv(labels[k],num(d[k],k.endsWith("temp")?" °C":k.endsWith("fan")?" RPM":" %"))).join("")+kv("显卡风扇",d.gpu_fan!=null?num(d.gpu_fan," RPM"):num(d.gpu_fan_pct," %"))+kv("内存",memory(d.mem_used,d.mem_total))+kv("显存",memory(d.gpu_mem_used,d.gpu_mem_total))+kv("上传 / 下载",`${rate(d.net_up)} / ${rate(d.net_down)}`);
}
function deviceCards(editable) {
  const devices = snapshot?.settings.routers || [], latest = state?.data?.routers || [];
  const items = editable ? devices : devices.filter(d=>visible("router:"+d.id));
  if (!items.length) return '<div class="card empty span-all">暂无设备，可在「设备」中添加路由器或远程电脑。</div>';
  return [...items].sort((a,b)=>(pref("router:"+a.id).order??100)-(pref("router:"+b.id).order??100)).map(cfg=>{
    const d = latest.find(x=>x.id===cfg.id), kind = cfg.type || "openwrt", online = cfg.enabled && d && !d.offline;
    const status = !cfg.enabled?"监控已关闭":!d?"正在连接":d.offline?(d.status || "连接中断"):"已连接";
    let body = "";
    if (cfg.enabled && d && !d.offline) {
      if (kind === "hwmonitor") body = remoteMetrics(d);
      else if (kind === "tplink") body = kv("WAN 联网",d.wan_connected===true?"已联网":d.wan_connected===false?"未联网":"未知")+kv("WAN IP",d.wan_ip || "—")+kv("WAN 在线时长",duration(d.wan_uptime))+kv("在线设备",num(d.clients))+kv("上传 / 下载",`${rate(d.up)} / ${rate(d.down_rate)}`);
      else body = kv("温度",num(d.temp," °C"))+kv("CPU 利用率",num(d.cpu_load," %"))+kv("内存",`${num(d.mem_load," %")} · ${memory(d.mem_used,d.mem_total)}`)+kv("负载",num(d.load1,"",2))+kv("运行时长",duration(d.uptime))+kv("WAN 网卡",d.nic || "—")+kv("上传 / 下载",`${rate(d.up)} / ${rate(d.down_rate)}`);
      if (kind === "tplink") body += `<details data-fold="${esc(cfg.id)}-clients"><summary><h3>终端流量排行</h3></summary>${d.terminals == null?'<div class="empty">终端信息暂不可用</div>':`<div class="table-wrap"><table><thead><tr><th>终端 / 地址</th><th>上传</th><th>下载</th></tr></thead><tbody>${d.terminals.map(t=>`<tr><td>${esc(t.name || t.id)}<small>${esc(t.ip)} · ${esc(t.id)}</small></td><td>${esc(rate(t.up))}</td><td>${esc(rate(t.down_rate))}</td></tr>`).join("")}</tbody></table></div>`}</details>`;
      if (kind === "hwmonitor") body += sensorGroups(d,cfg.id+":");
    } else body = '<p class="muted">'+(cfg.enabled?'设备连接后显示实时读数。':'启用监控后开始采集此设备的数据。')+'</p>';
    return `<article class="card"><div class="section-title"><div class="device-title"><span class="device-icon">${kind==="hwmonitor"?"▣":"⌘"}</span><div><h2>${title("router:"+cfg.id,cfg.name)}</h2><small>${esc({openwrt:"OpenWrt",tplink:"TP-Link",hwmonitor:"远程电脑"}[kind])} · ${esc(cfg.url)}</small></div></div><span class="device-status ${online?"":"off"}">${esc(status)}</span></div><div class="device-body">${body}</div>${editable?`<div class="device-actions"><button data-action="edit-device" data-id="${esc(cfg.id)}">编辑设备</button><button data-action="toggle-device" data-id="${esc(cfg.id)}">${cfg.enabled?"暂停监控":"启用监控"}</button></div>`:""}</article>`;
  }).join("");
}
function renderDevices() {$("#content").innerHTML = `<div class="section-title"><span class="muted">${snapshot.settings.routers.length} 台设备 · 最多 32 台</span><button class="primary" data-action="add-device">＋ 添加设备</button></div><div class="grid">${deviceCards(true)}</div>`;}
function inputRow(label, key, value, options = {}) {
  const {type = "number", min, max, step = 1, description = "", choices} = options;
  let control;
  if (choices) control = `<select name="${esc(key)}">${choices.map(([v,n])=>`<option value="${esc(v)}" ${String(value??"")===String(v)?"selected":""}>${esc(n)}</option>`).join("")}</select>`;
  else control = `<input name="${esc(key)}" type="${type}" ${type==="checkbox"?(value?"checked":""):`value="${esc(value)}"`} ${min!=null?`min="${min}"`:""} ${max!=null?`max="${max}"`:""} ${type==="number"?`step="${step}"`:""} ${type==="number"?"required":""}>`;
  return `<div class="form-row"><label for="f-${esc(key)}">${esc(label)}${description?`<small>${esc(description)}</small>`:""}</label>${control.replace('name=',`id="f-${esc(key)}" name=`)}</div>`;
}
function dataApiRows(w, desktop) {
  const host=w.lan?location.hostname:"127.0.0.1";
  return inputRow("传感器数据接口","web.enabled",w.enabled,{type:"checkbox",description:"与 LibreHardwareMonitor 兼容的只读 /data.json，无登录认证"})+inputRow("允许局域网访问","web.lan",w.lan,{type:"checkbox",description:desktop?"自动添加仅允许本地子网的防火墙规则":"关闭时仅服务器本机可访问"})+inputRow("数据接口端口","web.port",w.port,{min:1024,max:65535,description:"1024–65535，须与管理端口不同"})+kv("数据接口地址",w.enabled?`http://${host}:${w.port}/data.json`:"未开启")+(snapshot.web_error?`<p class="error">${esc(snapshot.web_error)}</p>`:"");
}
function renderSettings() {
  const s = snapshot.settings;
  const tabs = {monitor:"监控",alerts:"告警",floating:"悬浮窗",display:"显示",general:"常规",access:"登录设备"};
  const desktop = snapshot?.capabilities?.desktop !== false;
  if (!desktop) { delete tabs.floating; if (tab === "floating") tab = "monitor"; }
  let body = "";
  if (tab === "monitor") {
    const gpus = [["","自动选择"],...snapshot.gpus];
    if (s.gpu_id && !gpus.some(([id])=>id===s.gpu_id)) gpus.push([s.gpu_id,s.gpu_id+"（未连接）"]);
    const nics = new Map(snapshot.nics);
    (s.nic_ids || []).forEach(id=>{if (!nics.has(id)) nics.set(id,id+"（未连接）");});
    body = inputRow("刷新间隔","interval",s.interval,{min:.5,max:30,step:.5,description:"秒 · 控制本机采集节奏"})+inputRow("默认显卡","gpu_id",s.gpu_id,{choices:gpus})+inputRow("网卡统计方式","nic_mode",s.nic_ids===null?"auto":"manual",{choices:[["auto","自动选择统计接口"],["manual","手动选择网卡"]]})+`<div class="form-row"><label>手动网卡<small>仅在手动模式生效；可以全部取消</small></label><div class="checkbox-list">${[...nics].map(([id,name])=>`<label><input type="checkbox" name="nic" value="${esc(id)}" ${(s.nic_ids||[]).includes(id)?"checked":""}>${esc(name)}</label>`).join("")||'<small>尚未检测到网卡</small>'}</div></div>`;
  } else if (tab === "alerts") {
    const a=s.alerts;
    body = inputRow("温度告警","alerts.enabled",a.enabled,{type:"checkbox"})+inputRow("CPU 温度阈值","alerts.cpu_temp",a.cpu_temp,{min:40,max:110,description:"°C"})+inputRow("显卡温度阈值","alerts.gpu_temp",a.gpu_temp,{min:40,max:110,description:"°C"})+inputRow("持续超温时间","alerts.duration",a.duration,{min:0,max:300,description:"秒"})+inputRow("恢复温差","alerts.hysteresis",a.hysteresis,{min:1,max:20,description:"°C"})+inputRow("提醒间隔","alerts.cooldown",a.cooldown,{min:30,max:7200,description:"秒"})+inputRow("风扇异常检测","alerts.fan_enabled",a.fan_enabled,{type:"checkbox",description:"对应设备高温且风扇低速时提醒"})+inputRow("最低风扇转速","alerts.fan_min",a.fan_min,{min:1,max:5000,description:"RPM"})+inputRow("风扇异常持续时间","alerts.fan_duration",a.fan_duration,{min:5,max:600,description:"秒"})+`<p class="muted">告警由监控服务持续检测，可在「信息」分页查看最近 1000 条记录。当前版本未提供手机锁屏推送。</p>`;
  } else if (tab === "floating") {
    const f=s.floating;
    body = inputRow("显示电脑桌面悬浮窗","floating.enabled",f.enabled,{type:"checkbox"})+inputRow("悬浮窗置顶","floating.topmost",f.topmost,{type:"checkbox"})+inputRow("贴边自动隐藏","floating.autohide",f.autohide,{type:"checkbox"})+inputRow("显示内存","floating.show_mem",f.show_mem,{type:"checkbox"})+inputRow("显示网速","floating.show_net",f.show_net,{type:"checkbox"})+inputRow("不透明度","floating.opacity",f.opacity,{min:.3,max:1,step:.05,description:"0.3–1"});
  } else if (tab === "display") {
    const names={...snapshot.names};
    Object.keys(s.display).forEach(k=>{if(!names[k]) names[k]=k+"（已断连）";});
    body=`<p class="muted">名称、顺序和显示状态保存到监控服务；顺序数值越小越靠前。</p><div class="display-row display-head"><span>项目</span><span>自定义名称</span><span>顺序</span><span>显示</span></div>`+Object.entries(names).map(([key,name])=>`<div class="display-row" data-pref="${esc(key)}"><label>${esc(name)}</label><input aria-label="${esc(name)}名称" data-prop="name" maxlength="60" value="${esc(s.display[key]?.name||"")}" placeholder="默认名称"><input aria-label="${esc(name)}顺序" data-prop="order" type="number" min="0" max="10000" step="1" required value="${s.display[key]?.order??100}"><input aria-label="显示${esc(name)}" data-prop="visible" type="checkbox" ${s.display[key]?.visible===false?"":"checked"}></div>`).join("");
  } else {
    body=dataApiRows(s.web,desktop)+inputRow("电脑主窗口置顶","topmost",s.topmost,{type:"checkbox"})+inputRow("关闭电脑主窗口时","close_action",s.close_action,{choices:[["ask","每次询问"],["tray","最小化到托盘"],["exit","退出软件"]]})+inputRow("保存历史到电脑","history_enabled",s.history_enabled,{type:"checkbox",description:"关闭后仍保留本次运行的内存历史"})+`<div class="notice">管理端的端口和开关在电脑端配置。开机启动及驱动安装请在电脑端操作。</div>${kv("当前管理地址",location.origin)}${kv("连接协议",location.protocol.replace(":", "").toUpperCase())}`;
  }
  if (!desktop && tab === "general") body=dataApiRows(s.web,desktop)+inputRow("保存历史到服务器","history_enabled",s.history_enabled,{type:"checkbox",description:"关闭后仍保留本次运行的内存历史"})+`<div class="notice">管理端的监听地址、端口和自启动通过服务器命令行或 systemd 配置。</div>${kv("当前管理地址",location.origin)}`;
  const tabBar=`<div class="tabs" role="tablist">${Object.entries(tabs).map(([id,label])=>`<button role="tab" aria-selected="${tab===id}" class="${tab===id?"selected":""}" data-tab="${id}">${label}</button>`).join("")}</div>`;
  if (tab === "access") {$("#content").innerHTML=tabBar+accessMarkup(desktop); return;}
  $("#content").innerHTML=`${tabBar}<form id="settings-form"><div class="card">${body}</div><div class="form-actions"><p class="muted" id="save-description">修改后保存并应用</p><button type="button" data-action="reload-settings">重新载入</button><button type="submit" class="primary">保存设置</button></div><p id="settings-error" class="error" role="alert"></p></form>`;
}
function ago(t) {const m=Math.max(0,Math.round((Date.now()/1000-t)/60)); return m<1?"刚刚":m<60?`${m} 分钟前`:m<1440?`${Math.round(m/60)} 小时前`:`${Math.round(m/1440)} 天前`;}
function accessMarkup(desktop) {
  if (!access) return '<div class="card empty">正在载入…</div>';
  const now=Date.now()/1000, where=desktop?"电脑端「设置 → 网页登录」":"服务器上运行 sh start-linux.sh mail ";
  const pending=access.pending.length?access.pending.map(p=>`<div class="access-row"><div><span class="pairing-code">${esc(p.code)}</span><small>${esc(p.name)} · ${esc(p.ip)} · ${Math.max(1,Math.ceil((p.expires-now)/60))} 分钟内有效</small></div><div class="actions"><button data-action="reject" data-id="${esc(p.id)}">拒绝</button><button class="primary" data-action="approve" data-id="${esc(p.id)}">批准登录</button></div></div>`).join(""):'<div class="empty">暂无待处理的配对请求</div>';
  const devices=access.devices.map(d=>`<div class="access-row"><div>${esc(d.name||"未知设备")}${d.current?' <span class="badge">本设备</span>':""}<small>${esc(d.ip)} · 最近使用 ${esc(ago(d.last_seen))} · 配对于 ${esc(new Date(d.created*1000).toLocaleDateString("zh-CN"))}</small></div><div class="actions">${d.current?'<button data-action="logout">退出登录</button>':`<button class="danger" data-action="revoke" data-id="${esc(d.id)}">移除</button>`}</div></div>`).join("");
  return `<div class="stack"><article class="card"><div class="section-title"><h2>待处理的配对请求</h2><span class="muted">确认是你本人的设备后再批准</span></div>${pending}</article><article class="card"><div class="section-title"><h2>已登录的设备</h2><button class="quiet danger" data-action="revoke-others" ${access.devices.length>1?"":"disabled"}>移除其他设备</button></div>${devices}</article><article class="card">${kv("配对码邮件",access.mail.configured?"发送到 "+access.mail.to:"未配置")}<p class="muted">邮件地址只能在${esc(where)}修改，网页端不能更改。移除的设备需要重新配对。</p></article></div>`;
}
async function loadAccess() {access=await api("/api/auth/devices"); if(page==="settings"&&tab==="access") renderSettings();}
function formChanges(form) {
  const changes = {}, s=snapshot.settings;
  if (tab === "display") {
    // Submit only rows whose effective values changed, so untouched defaults and
    // preferences of removed devices are never written back.
    changes.display = {};
    $$("[data-pref]",form).forEach(row=>{
      const key=row.dataset.pref, p=s.display[key]||{};
      const value={name:$("[data-prop=name]",row).value.trim(),order:Number($("[data-prop=order]",row).value),visible:$("[data-prop=visible]",row).checked};
      if(value.name!==(p.name||"")||value.order!==(p.order??100)||value.visible!==(p.visible!==false)) changes.display[key]={...p,...value};
    });
  } else {
    $$('[name]',form).forEach(input=>{
      if (["nic","nic_mode"].includes(input.name)) return;
      const [group,key] = input.name.split(".");
      const value=input.type==="checkbox"?input.checked:input.type==="number"?Number(input.value):input.value||null;
      if (key) {(changes[group] ||= {})[key]=value;}
      else changes[group]=value;
    });
    if (tab==="monitor") changes.nic_ids=form.elements.nic_mode.value==="auto"?null:$$('[name=nic]:checked',form).map(x=>x.value);
  }
  for (const key of Object.keys(changes)) {
    if (["floating","alerts","display","web"].includes(key)) {
      for (const sub of Object.keys(changes[key])) if (JSON.stringify(changes[key][sub])===JSON.stringify(s[key][sub])) delete changes[key][sub];
      if (!Object.keys(changes[key]).length) delete changes[key];
    } else if (JSON.stringify(changes[key])===JSON.stringify(s[key])) delete changes[key];
  }
  return changes;
}
async function saveChanges(changes, revision = snapshot.revision) {
  snapshot = await api("/api/settings",{revision,changes});
  dirty=false; toast(changes.web&&snapshot.web_error?"已保存，但"+snapshot.web_error:"已保存并应用"); notice("");
}
function deviceFields(cfg) {
  const kind=cfg.type||"openwrt";
  $("#device-fields").innerHTML=inputRow("设备名称","name",cfg.name,{type:"text"})+inputRow("设备类型","type",kind,{choices:[["openwrt","OpenWrt 路由器"],["tplink","TP-Link 原厂路由器"],["hwmonitor","远程电脑"]]})+inputRow("设备地址","url",cfg.url,{type:"url",description:kind==="tplink"?"例如 http://192.168.0.1":kind==="hwmonitor"?"例如 http://192.168.1.2:8085/data.json":"例如 http://192.168.1.1:9100/metrics"})+inputRow("启用监控","enabled",cfg.enabled,{type:"checkbox"})+(kind==="tplink"?inputRow("管理员密码","password","",{type:"password",description:cfg.password_saved?"已保存，留空保留原密码；修改地址后需重新输入":"仅写入，加密保存在监控主机"})+inputRow("设备在线状态变更提醒","notify_clients",cfg.notify_clients!==false,{type:"checkbox"})+'<p class="muted">断线后先等待 60 秒，再检查管理页面是否稳定；登录失败或结果不明时暂停自动登录。手动重试请点击“保存并重新连接”：保存修改并启用监控，密码留空使用已保存密码；两次密码登录至少间隔 60 秒，请勿反复点击。</p>':kind==="openwrt"?inputRow("WAN 网卡","nic",cfg.nic||"",{type:"text",description:"留空自动选择，例如 pppoe-wan"}):"");
  $("#device-reconnect").hidden=kind!=="tplink";
  $("#device-form [name=name]").maxLength=60;
  $("#device-form [name=name]").required=true;
  $("#device-form [name=url]").required=true;
  const password=$("#device-form [name=password]"); if(password) {password.autocomplete="new-password";password.maxLength=256;}
}
function openDevice(id) {
  editorDevice=id?snapshot.settings.routers.find(d=>d.id===id):{id:"web-"+[...crypto.getRandomValues(new Uint8Array(16))].map(v=>v.toString(16).padStart(2,"0")).join(""),name:"新设备",type:"openwrt",url:"http://192.168.1.1:9100/metrics",enabled:false,nic:null};
  editingRevision=snapshot.revision;
  $("#delete-device").hidden=!id; $("#device-title").textContent=id?"编辑设备":"添加设备";
  $("#device-error").textContent=""; deviceFields(editorDevice); $("#device-dialog").showModal();
}
function historyQuery(csv=false) {
  const p=new URLSearchParams({hours:String(hours)});
  if(chartInterval!=="auto") p.set("interval",chartInterval);
  if(chartGpu) p.set("gpu",chartGpu);
  return `/api/history${csv?".csv":""}?${p}`;
}
async function loadHistory() {historyData=await api(historyQuery()); lastHistory=Date.now(); if(page==="history") renderHistory();}
function chartMarkup(key) {
  const h=historyData, runs=h.charts[key], points=runs.flat(), peak=h.peaks[key];
  const isNet=key.startsWith("net_"), unit=isNet?"":key.endsWith("temp")?" °C":" %";
  const max=key.endsWith("load")?100:Math.max(key.endsWith("temp")?40:1,...points.map(p=>p[1]))*1.1;
  const x=t=>40+(t-h.start)/(h.end-h.start)*540, y=v=>155-v/max*130;
  let netDivisor=1, netUnit="B/s"; if(isNet) for(const unitName of ["KB/s","MB/s","GB/s"]) {if(max/netDivisor<1024) break; netDivisor*=1024; netUnit=unitName;}
  const paths=runs.map(run=>run.length===1?`<circle class="curve-dot" cx="${x(run[0][0])}" cy="${y(run[0][1])}" r="2"/>`:`<path class="curve" d="${run.map(([t,v],i)=>`${i?"L":"M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ")}"/>`).join("");
  return `<article class="card"><div class="section-title"><h2>${labels[key]}</h2><span class="chart-peak">最高 ${isNet?rate(peak):num(peak,unit)}</span></div><svg class="chart" data-chart="${key}" viewBox="0 0 600 185" role="img" aria-label="${labels[key]}历史曲线">${[0,.5,1].map(p=>`<line class="grid-line" x1="40" x2="580" y1="${y(max*p)}" y2="${y(max*p)}"/><text x="0" y="${y(max*p)+4}">${esc(isNet?num(max*p/netDivisor,"",1):num(max*p))}</text>`).join("")}${paths}${[0,.5,1].map(p=>`<text x="${40+540*p}" y="178" text-anchor="${p===0?"start":p===1?"end":"middle"}">${clock(h.start+(h.end-h.start)*p).slice(0,5)}</text>`).join("")}<line class="hover-line" x1="0" x2="0" y1="20" y2="155" visibility="hidden"/></svg><p class="chart-tip" data-tip="${key}">${points.length?isNet?"纵轴单位："+netUnit+" · 触摸查看读数":"触摸曲线查看读数":"该时段暂无数据"}</p></article>`;
}
function renderHistory() {
  if (!historyData) return;
  const keys=["cpu_temp","gpu_temp","cpu_load","gpu_load","mem_load","gpu_mem_load","net_up","net_down"];
  $("#content").innerHTML=`<div class="chart-controls"><select id="history-hours" aria-label="时间范围"><option value="1" ${hours===1?"selected":""}>最近 1 小时</option><option value="24" ${hours===24?"selected":""}>最近 24 小时</option></select><select id="history-interval" aria-label="显示间隔">${[["auto","自动间隔"],["0","原始数据"],["30","30 秒"],["60","1 分钟"],["300","5 分钟"],["900","15 分钟"]].map(([v,n])=>`<option value="${v}" ${chartInterval===v?"selected":""}>${n}</option>`).join("")}</select><select id="history-gpu" aria-label="历史显卡">${historyData.gpus.map(([id,name])=>`<option value="${esc(id)}" ${historyData.gpu===id?"selected":""}>${esc(name)}</option>`).join("")||'<option value="">暂无显卡历史</option>'}</select><a href="${esc(historyQuery(true))}">导出 CSV ↓</a></div><p class="muted">${historyData.count} 条采样 · 缺失数据处断开 · 网络曲线使用当前网卡选择</p><div class="grid chart-grid" style="margin-top:18px">${keys.map(chartMarkup).join("")}</div>`;
}
// Fetch only; callers render. The response carries the revision of exactly that page.
async function loadAlerts(next = alertPage) {
  const request = ++alertRequest;
  alertsLoading = true;
  try {
    const result = await api(`/api/alerts?page=${next}&page_size=20`);
    if (request !== alertRequest) return false;
    alertData = result; alertPage = result.page; alertRevision = result.revision;
    return true;
  } finally {
    if (request === alertRequest) alertsLoading = false;
  }
}
function alertMarkup() {
  const a = alertData;
  if (!a) return '<div class="empty">正在载入告警…</div>';
  return `${a.items.length?a.items.map(event=>kv(event.time,event.message)).join(""):'<div class="empty">暂无告警，监控服务会持续监测。</div>'}<div class="alert-pagination"><span class="muted">共 ${a.total} 条 · 每页 ${a.page_size} 条</span><div class="alert-page-actions"><button class="quiet" data-action="alert-page" data-alert-page="${a.page-1}" ${a.page<=1?"disabled":""}>上一页</button><label class="muted">第 <select id="alert-page" aria-label="告警页码">${Array.from({length:a.pages},(_,i)=>`<option value="${i+1}" ${a.page===i+1?"selected":""}>${i+1}</option>`).join("")}</select> / ${a.pages} 页</label><button class="quiet" data-action="alert-page" data-alert-page="${a.page+1}" ${a.page>=a.pages?"disabled":""}>下一页</button><button class="quiet" data-action="latest-alerts">查看最新</button></div></div>`;
}
function renderInfo() {
  const h=hardware;
  $("#content").innerHTML=`<div class="grid"><article class="card"><div class="section-title"><h2>硬件概览</h2><button class="quiet" data-action="refresh-info">刷新</button></div>${!h||h.loading?'<div class="empty">正在读取硬件信息…</div>':h.error?`<div class="empty">${esc(h.error)}</div>`:`${kv("整机",h.model)}${kv("操作系统",h.os)}${(h.details||[]).map(([name,lines])=>`<details data-fold="info-${esc(name)}"><summary><h3>${esc(name)}</h3></summary>${lines.map(line=>`<p class="muted" style="padding:8px 0">${esc(line)}</p>`).join("")}</details>`).join("")}`}</article><article class="card"><div class="section-title"><h2>诊断与运行状态</h2><button class="quiet" data-action="diagnostics">导出 JSON ↓</button></div>${kv("软件版本",state?.version||"—")}${kv("采集耗时",num(state?.data?.collection_ms," ms",1))}${kv("采集时间",state?.data?.sample_time?clock(state.data.sample_time):"—")}<p class="muted">报告包括硬件概览、当前读数和最近告警。配置密码不会导出。</p></article><article class="card span-all"><div class="section-title"><h2>最近告警</h2><span class="muted">保留最近 1000 条 · 重启后恢复</span></div>${alertMarkup()}</article></div>`;
}
function renderPage() {
  // Preserve expanded groups and scrollable terminal lists across live refreshes.
  const opened=new Set($$("details[open][data-fold]").map(x=>x.dataset.fold));
  const scrolls=$$(".table-wrap").map(x=>x.scrollTop);
  ({overview:renderOverview,devices:renderDevices,settings:renderSettings,history:renderHistory,info:renderInfo}[page])();
  $$("details[data-fold]").forEach(x=>x.open=opened.has(x.dataset.fold));
  $$(".table-wrap").forEach((x,i)=>x.scrollTop=scrolls[i]||0);
}
async function poll() {
  if (!active || polling) return;
  polling=true;
  try {
    state=await api("/api/state");
    if (!active) return;
    const stale=!state.data?.sample_time || state.time-state.data.sample_time>Math.max(10,(snapshot?.settings.interval||1)*3);
    $("#connection").textContent=stale?"等待采集":"● 已连接";
    $("#connection").classList.toggle("warn",stale);
    $("#last-update").textContent=state.data?.sample_time?"采集于 "+clock(state.data.sample_time):"等待首份数据";
    $("#version").textContent="v"+state.version;
    if(state.revision!==snapshot?.revision) {
      if(dirty || $("#device-dialog").open) notice("电脑端或其他页面已修改设置。请先重新载入，避免覆盖新的修改。");
      else {snapshot=await api("/api/settings"); if(page==="settings") renderSettings();}
    }
    const pairing=state.pairing?`有 ${state.pairing} 个设备正在申请登录，可在「设置 → 登录设备」查看配对码或批准。`:"";
    if(!dirty && !$("#device-dialog").open) notice(pairing || state.data?.error || state.storage_error || (stale?"采集尚未就绪，当前可能显示上次读数。":""));
    if(page==="settings" && tab==="access") await loadAccess();
    if(["overview","devices"].includes(page)) renderPage();
    if(page==="history" && Date.now()-lastHistory>15000) await loadHistory();
    if(page==="info") {if(hardware?.loading) hardware=await api("/api/hardware"); if(!alertsLoading && alertPage===1 && alertRevision!==state.alerts_revision) await loadAlerts(); renderPage();}
  } catch(error) {
    if(active) {$("#connection").textContent="连接中断";$("#connection").classList.add("warn");notice("连接中断，当前为上次收到的数据。正在自动重试…");}
  } finally {polling=false;if(active) setTimeout(poll,Math.max(1000,(snapshot?.settings.interval||1)*1000));}
}
document.addEventListener("click", async event=>{
  const nav=event.target.closest('a[href^="#"]');
  if(nav && active) {event.preventDefault();await navigate(nav.hash.slice(1));return;}
  const t=event.target.closest("[data-tab]");
  if(t) {if(dirty&&!confirm("放弃未保存的修改并切换设置？"))return;dirty=false;tab=t.dataset.tab;renderSettings();if(tab==="access")try{await loadAccess();}catch(error){notice(error.message);}return;}
  const b=event.target.closest("[data-action]"); if(!b)return;
  try {
    switch(b.dataset.action) {
      case "close-dialog": if(!dirty||confirm("放弃设备修改？")){$("#device-dialog").close();dirty=false;} break;
      case "add-device": openDevice(); break;
      case "edit-device": openDevice(b.dataset.id); break;
      case "toggle-device": {b.disabled=true; const routers=clone(snapshot.settings.routers); const d=routers.find(x=>x.id===b.dataset.id);d.enabled=!d.enabled;await saveChanges({routers});renderDevices();break;}
      case "reload-settings": if(!dirty||confirm("放弃修改并重新载入设置？")){snapshot=await api("/api/settings");dirty=false;notice("");renderSettings();}break;
      case "refresh-info": hardware=await api("/api/hardware?refresh=1");renderInfo();break;
      case "alert-page": if(await loadAlerts(Number(b.dataset.alertPage)) && page==="info") renderPage(); break;
      case "latest-alerts": if(await loadAlerts(1) && page==="info") renderPage(); break;
      case "approve": case "reject": b.disabled=true; await api(`/api/auth/${b.dataset.action}`,{id:b.dataset.id}); toast(b.dataset.action==="approve"?"已批准，对方页面会自动登录":"已拒绝该请求"); await loadAccess(); break;
      case "revoke": if(confirm("移除该设备？它需要重新配对才能访问。")){b.disabled=true;await api("/api/auth/revoke",{id:b.dataset.id});await loadAccess();} break;
      case "revoke-others": if(confirm("移除除本设备外的所有设备？")){b.disabled=true;await api("/api/auth/revoke",{id:"others"});toast("已移除其他设备");await loadAccess();} break;
      case "logout": if(confirm("退出登录？再次访问需要重新配对。")){await api("/api/auth/logout",{});showLogin(null);} break;
      case "diagnostics": {const data=await api("/api/diagnostics");const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download="hardware-diagnostics.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);break;}
    }
  } catch(error) {notice(error.message);b.disabled=false;}
});
$("#content").addEventListener("input",event=>{if(event.target.closest("#settings-form")){dirty=true;$("#save-description").textContent="有未保存的修改";}});
$("#content").addEventListener("change",async event=>{
  const t=event.target;
  if(t.id==="alert-page") {try{if(await loadAlerts(Number(t.value)) && page==="info") renderPage();}catch(error){notice(error.message);}return;}
  if(t.id.startsWith("history-")) {if(t.id==="history-hours")hours=Number(t.value);if(t.id==="history-interval")chartInterval=t.value;if(t.id==="history-gpu")chartGpu=t.value;try{await loadHistory();}catch(error){notice(error.message);}}
});
$("#content").addEventListener("submit",async event=>{
  if(event.target.id!=="settings-form")return;event.preventDefault();const button=$('[type=submit]',event.target);button.disabled=true;
  try{await saveChanges(formChanges(event.target));renderSettings();}catch(error){$("#settings-error").textContent=error.message;button.disabled=false;}
});
$("#device-form").addEventListener("input",()=>dirty=true);
$("#device-form").addEventListener("change",event=>{
  if(event.target.name!=="type")return;
  const kind=event.target.value,name=event.target.form.elements.name.value;
  const url={openwrt:"http://192.168.1.1:9100/metrics",tplink:"http://192.168.0.1",hwmonitor:"http://192.168.1.2:8085/data.json"}[kind];
  deviceFields({...editorDevice,name,type:kind,url,enabled:false,password_saved:false});
});
$("#device-dialog").addEventListener("cancel",event=>{if(dirty&&!confirm("放弃设备修改？"))event.preventDefault();else dirty=false;});
// Some browsers omit SubmitEvent.submitter; the click still tells us which button
// submitted, so the reconnect action must not silently degrade to an ordinary save.
let reconnectClicked=false;
$("#device-reconnect").addEventListener("click",()=>{reconnectClicked=true;});
$("#device-form").addEventListener("submit",async event=>{
  event.preventDefault();const f=event.target,buttons=$$('[type=submit]',f);buttons.forEach(b=>b.disabled=true);
  const d={id:editorDevice.id,name:f.elements.name.value.trim(),type:f.elements.type.value,url:f.elements.url.value.trim(),enabled:f.elements.enabled.checked,nic:f.elements.nic?.value.trim()||null};
  const reconnect=d.type==="tplink"&&(event.submitter?.name==="reconnect"||reconnectClicked);
  reconnectClicked=false;
  if(d.type==="tplink"){d.password=f.elements.password.value;d.notify_clients=f.elements.notify_clients.checked;d.reconnect=reconnect;if(reconnect)d.enabled=true;}
  const routers=clone(snapshot.settings.routers),index=routers.findIndex(x=>x.id===d.id);if(index>=0)routers[index]=d;else routers.push(d);
  try{await saveChanges({routers},editingRevision);$("#device-dialog").close();renderDevices();if(reconnect)toast("已安排重新连接；可能需等待登录冷却，结果请查看设备卡片");}catch(error){$("#device-error").textContent=error.message;}finally{buttons.forEach(b=>b.disabled=false);}
});
$("#delete-device").addEventListener("click",async event=>{
  if(!confirm(`删除设备「${editorDevice.name}」？`))return;event.target.disabled=true;
  try{await saveChanges({routers:snapshot.settings.routers.filter(x=>x.id!==editorDevice.id)},editingRevision);$("#device-dialog").close();renderDevices();}catch(error){$("#device-error").textContent=error.message;}finally{event.target.disabled=false;}
});
$("#content").addEventListener("pointermove",event=>{
  const svg=event.target.closest("[data-chart]");if(!svg||!historyData)return;
  const key=svg.dataset.chart,rect=svg.getBoundingClientRect(),x=(event.clientX-rect.left)/rect.width*600;
  const t=historyData.start+(x-40)/540*(historyData.end-historyData.start),points=historyData.charts[key].flat();if(!points.length)return;
  let nearest=points[0];for(const point of points)if(Math.abs(point[0]-t)<Math.abs(nearest[0]-t))nearest=point;
  const line=$(".hover-line",svg),px=40+(nearest[0]-historyData.start)/(historyData.end-historyData.start)*540;
  line.setAttribute("x1",px);line.setAttribute("x2",px);line.setAttribute("visibility","visible");
  $(`[data-tip="${key}"]`).textContent=clock(nearest[0])+" · "+(key.startsWith("net_")?rate(nearest[1]):num(nearest[1],key.endsWith("temp")?" °C":" %",1));
});
window.addEventListener("hashchange",()=>{if(active)navigate(location.hash.slice(1));});
window.addEventListener("beforeunload",event=>{if(dirty){event.preventDefault();event.returnValue="";}});
function loginError(message = "") {$("#login-error").textContent = message;}
// Loads the new image before swapping it in, so a refused refresh keeps the current one readable.
// `message` is shown alongside: success clears any stale load error, failure appends the reason.
async function refreshCaptcha(message = "") {
  $("#captcha-input").value = ""; loginError(message);
  try {
    const response = await fetch("/api/auth/captcha", {cache:"no-store", signal:AbortSignal.timeout(15000)});
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || "验证码加载失败");
    const image = await response.blob();
    $("#captcha").src = await new Promise((resolve, reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(image);
    });
  } catch (error) {
    const reason = `验证码刷新失败（${error.message || "网络错误"}），请稍后点击图片重试。`;
    loginError(message ? `${message}；${reason}` : reason);
  }
}
function showLogin(status) {
  active = false; access = null; dirty = false;
  if ($("#device-dialog").open) $("#device-dialog").close();
  $("#app").hidden = true; $("#login").hidden = false;
  if (status?.pending) codeStep(status.pending); else captchaStep();
}
function captchaStep(message = "") {
  clearTimeout(loginTimer);
  $("#captcha-form").hidden = false; $("#code-form").hidden = true;
  refreshCaptcha(message);
}
function codeStep(pending) {
  $("#captcha-form").hidden = true; $("#code-form").hidden = false;
  const local = "也可以在电脑主界面（服务器为日志）查看，或在已登录的设备上批准。";
  $("#code-hint").textContent = ({sending:"正在发送到你的邮箱，10 分钟内有效。",sent:"已发送到你的邮箱，10 分钟内有效。",failed:"邮件发送失败。",unconfigured:"未配置邮件。"}[pending.mail] || "") + local;
  clearTimeout(loginTimer); loginTimer = setTimeout(checkPairing, 3000);
}
async function checkPairing() {
  try {
    const status = await api("/api/auth/status");
    if (status.authenticated) return enter();
    if (!status.pending) return captchaStep("配对码已过期，请重新获取。");
    codeStep(status.pending);
  } catch {loginTimer = setTimeout(checkPairing, 5000);}
}
async function enter() {
  clearTimeout(loginTimer); loginError("");
  $("#login").hidden = true; $("#app").hidden = false;
  try {await start();}
  catch {notice("无法连接管理服务，请检查地址和电脑端运行状态。正在重试…"); setTimeout(connect, 5000);}
}
$("#captcha").addEventListener("click", () => refreshCaptcha());
$("#restart-login").addEventListener("click", () => captchaStep());
$("#captcha-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = $("[type=submit]", event.target); button.disabled = true;
  try {const result = await api("/api/auth/request", {captcha: $("#captcha-input").value.trim()}); loginError(""); $("#code-input").value = ""; codeStep(result.pending); $("#code-input").focus();}
  catch (error) {refreshCaptcha(error.message);}
  finally {button.disabled = false;}
});
$("#code-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = $("[type=submit]", event.target); button.disabled = true;
  try {await api("/api/auth/verify", {code: $("#code-input").value.trim()});}
  catch (error) {
    $("#code-input").value = "";
    // A used-up or expired code cannot be retried: start over with a new captcha.
    try {const status = await api("/api/auth/status"); if (!status.pending) captchaStep(error.message); else loginError(error.message);}
    catch {loginError(error.message);}
    return;
  } finally {button.disabled = false;}
  await enter();
});
async function connect() {
  try {
    const status = await api("/api/auth/status");
    if (status.authenticated) await enter(); else showLogin(status);
  } catch {$("#login").hidden = true; $("#app").hidden = false; notice("无法连接管理服务，请检查地址和电脑端运行状态。正在重试…"); setTimeout(connect, 5000);}
}
connect();
