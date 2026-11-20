const msg = document.getElementById('msg');
const view = document.getElementById('view');
let windows = [];

async function api(path, opts={}) {
  const r = await fetch(path, { headers:{'Content-Type':'application/json'}, ...opts });
  const j = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}
function setMsg(t, err=false){ msg.textContent=t||''; msg.className = err?'err':'muted'; }
async function loadWindows(){ windows = (await api('/api/windows')).items || []; }

function renderWindows(){
  view.innerHTML = `<div class="wall">${windows.map(w=>`
    <article class="card">
      <h3>${w.table_name}</h3>
      <p class="muted">${w.start_date} → ${w.end_date}</p>
      <span class="tag">${w.status}</span>
      <p class="muted">切片 ${w.slice_count} · 已落库 ${w.loaded_count}</p>
    </article>`).join('')}</div>`;
}

function winOptions(){ return windows.map(w=>`<option value="${w.id}">#${w.id} ${w.table_name}</option>`).join(''); }

function renderPlan(){
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">生成切片计划</button>
    <pre id="out" class="muted"></pre>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const wid = document.getElementById('wid').value;
      const j = await api(`/api/windows/${wid}/plan`, {method:'POST', body:'{}'});
      document.getElementById('out').textContent = JSON.stringify(j.slices, null, 2);
      setMsg(`已生成 ${j.slices.length} 片`);
      await loadWindows();
    }catch(e){ setMsg(e.message,true); }
  };
}

async function renderReplay(){
  await loadWindows();
  const wid = windows[0]?.id;
  const slices = wid ? (await api(`/api/windows/${wid}/slices`)).items : [];
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <div id="list" class="wall" style="margin-top:0.8rem"></div>
  </div>`;
  const draw = async ()=>{
    const id = document.getElementById('wid').value;
    const items = (await api(`/api/windows/${id}/slices`)).items || [];
    document.getElementById('list').innerHTML = items.map(s=>`
      <article class="card">
        <h3>${s.slice_date}</h3>
        <span class="tag">${s.status}</span>
        <p class="muted">行数 ${s.row_count}</p>
        <button data-id="${s.id}" class="primary" type="button" ${s.status!=='待重放'?'disabled':''}>重放</button>
      </article>`).join('') || '<p class="muted">先生成切片计划</p>';
    document.querySelectorAll('#list button[data-id]').forEach(btn=>{
      btn.onclick = async ()=>{
        try{
          const j = await api(`/api/slices/${btn.dataset.id}/replay`, {method:'POST', body:'{}'});
          setMsg(`已落库 ${j.slice.slice_date} · ${j.slice.row_count} 行`);
          await draw(); await loadWindows();
        }catch(e){ setMsg(e.message,true); }
      };
    });
  };
  document.getElementById('wid').onchange = draw;
  await draw();
}

async function renderCoverage(){
  const items = (await api('/api/coverage-reports')).items || [];
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">跑覆盖对账</button>
    <div class="wall" style="margin-top:0.8rem">${items.map(r=>`
      <article class="card"><h3>窗口 #${r.window_id}</h3><p>${r.summary}</p></article>`).join('')}</div>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const j = await api('/api/coverage-reports', {method:'POST', body: JSON.stringify({window_id: Number(document.getElementById('wid').value)})});
      setMsg(j.report.summary);
      await renderCoverage();
    }catch(e){ setMsg(e.message,true); }
  };
}


async function render_queue(){
  const j = await api('/api/overlap/panels');
  view.innerHTML = `<div class="wall">
    <article class="card">
      <h3>挂窗登记</h3>
      <label>目标表<input id="f-table" value="dw_orders_d" /></label>
      <label>开始日<input id="f-start" type="date" value="2026-11-11" /></label>
      <label>结束日<input id="f-end" type="date" value="2026-11-13" /></label>
      <label>落库策略
        <select id="f-mode">
          <option value="hard_unique"${j.mode==='hard_unique'?' selected':''}>唯一约束拦截（相交不落库）</option>
          <option value="conflict_ledger"${j.mode==='conflict_ledger'?' selected':''}>旁路冲突账册（先记账再撤回）</option>
        </select>
      </label>
      <button class="primary" id="h" type="button">挂窗</button>
      <p id="hang-chip" class="chip-box"></p>
    </article>
    ${(j.queue||[]).map(r=>`<article class="card ${r.rejected?'reject':''}">
      <p>${r.note}</p>
      ${r.rejected?'<span class="tag chip-reject">拒登</span>':'<span class="tag">accepted '+r.accepted+'</span>'}
    </article>`).join('')||'<article class="card"><p class="muted">冲突队列为空</p></article>'}
  </div>`;
  document.getElementById('f-mode').onchange = async (e)=>{
    try{ await api('/api/overlap/mode',{method:'POST',body:JSON.stringify({mode:e.target.value})}); setMsg('落库策略已切换为 '+e.target.value); }
    catch(err){ setMsg(err.message,true); }
  };
  document.getElementById('h').onclick = async ()=>{
    const chip = document.getElementById('hang-chip');
    chip.textContent=''; chip.className='chip-box';
    try{
      const payload = {
        table_name: document.getElementById('f-table').value,
        start_date: document.getElementById('f-start').value,
        end_date: document.getElementById('f-end').value,
        mode: document.getElementById('f-mode').value,
      };
      const r = await api('/api/overlap/hang',{method:'POST',body:JSON.stringify(payload)});
      chip.textContent = r.chip || '已挂';
      chip.className = 'chip-box chip-ok';
      setMsg('挂窗成功：'+(r.day_bound_live||''));
      await render_queue();
    }catch(err){
      // 409 拒登：点亮拒登芯片
      chip.textContent = '拒登';
      chip.className = 'chip-box chip-reject';
      setMsg(err.message, true);
      await render_queue();
      const chip2 = document.getElementById('hang-chip');
      chip2.textContent = '拒登'; chip2.className = 'chip-box chip-reject';
    }
  };
}
async function render_bound(){
  const j = await api('/api/overlap/panels');
  view.innerHTML = `<div class="wall">${(j.windows||[]).map(w=>`<article class="card">
    <h3>${w.table_name}</h3><p>${w.start_date}→${w.end_date}</p>
    <span class="tag">${w.status||'在册'}</span>
  </article>`).join('')||'<p class="muted">库存无窗行</p>'}
  <article class="card"><p class="muted">库存窗行 ${(j.windows||[]).length} 条，相交对数 ${j.waterline.overlap_rows}</p></article></div>`;
}
async function render_card(){
  const j = await api('/api/overlap/panels');
  const aligned = j.card.aligned;
  view.innerHTML = `<div class="card">
    <h3>日界双界（同源派生）</h3>
    <p>现行日界 <strong>${j.card.live||'—'}</strong></p>
    <p>可挂门槛 <strong>${j.card.gate||'—'}</strong></p>
    <p class="${aligned?'ok-text':'muted'}">${aligned?'双界已对齐，同源库存最大结束日':'库存为空，暂不设门槛'}</p>
    <p class="muted">当前落库策略：${j.mode}</p>
  </div>`;
}
async function render_water(){
  const j = await api('/api/overlap/panels');
  const bad = (j.waterline.overlap_rows||0) + (j.waterline.slice_overlap_rows||0);
  view.innerHTML = `<div class="card">
    <h3>回灌水线</h3>
    <p>库存叠日行（相交窗对） <strong class="${j.waterline.overlap_rows?'err':'ok-text'}">${j.waterline.overlap_rows}</strong></p>
    <p>切片叠日行（跨窗同日落库） <strong class="${j.waterline.slice_overlap_rows?'err':'ok-text'}">${j.waterline.slice_overlap_rows}</strong></p>
    ${bad?'<span class="tag chip-reject">拒登</span>':'<span class="tag chip-ok">水线干净</span>'}
  </div>`;
}

async function show(tab){
  setMsg(''); await loadWindows();
  if (tab==='queue') await render_queue();
  if (tab==='bound') await render_bound();
  if (tab==='card') await render_card();
  if (tab==='water') await render_water();
}
document.querySelectorAll('.chips button').forEach(btn=>{
  btn.onclick=()=>{ document.querySelectorAll('.chips button').forEach(b=>b.classList.remove('on')); btn.classList.add('on'); show(btn.dataset.tab); };
});
document.getElementById('btn-logout').onclick = async ()=>{ await api('/api/logout',{method:'POST',body:'{}'}); location.href='/login'; };
show('queue');
