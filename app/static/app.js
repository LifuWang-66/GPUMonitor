const bootstrap = window.GPU_MONITOR_BOOTSTRAP || { sessionUsername: '', accessibleHosts: [] };
const currentGrid = document.getElementById('current-gpu-grid');
const statusSummary = document.getElementById('status-summary');
const gpuHistoryGrid = document.getElementById('gpu-history-grid');
const userTableWrapper = document.getElementById('user-table-wrapper');
const windowSelect = document.getElementById('window-select');
const refreshButton = document.getElementById('refresh-button');
const logoutButton = document.getElementById('logout-button');

function metricRow(label, value, progress = null) {
  const wrapper = document.createElement('div');
  wrapper.innerHTML = `<div class="metric-row"><span>${label}</span><strong>${value}</strong></div>`;
  if (progress !== null) {
    const bar = document.createElement('div');
    bar.className = 'progress';
    bar.innerHTML = `<span style="width:${Math.min(progress, 100)}%"></span>`;
    wrapper.appendChild(bar);
  }
  return wrapper;
}

function renderSummary(cards) {
  statusSummary.innerHTML = '';
  const total = cards.length;
  const busy = cards.filter(card => card.process_count > 0).length;
  const idle = cards.filter(card => card.is_idle).length;
  const avgUtil = total ? (cards.reduce((sum, card) => sum + card.utilization_gpu, 0) / total).toFixed(1) : '0.0';
  const values = [
    ['总 GPU 数', total],
    ['占用中', busy],
    ['空闲', idle],
    ['平均 util', `${avgUtil}%`],
  ];
  for (const [label, value] of values) {
    const tile = document.createElement('div');
    tile.className = 'stat-tile';
    tile.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
    statusSummary.appendChild(tile);
  }
}

function renderCurrent(cards) {
  currentGrid.innerHTML = '';
  if (!cards.length) {
    currentGrid.textContent = '暂无数据。请先验证连接并运行采集器。';
    currentGrid.classList.add('empty-state');
    return;
  }
  currentGrid.classList.remove('empty-state');
  renderSummary(cards);
  const template = document.getElementById('gpu-card-template');
  for (const card of cards) {
    const node = template.content.cloneNode(true);
    node.querySelector('h3').textContent = `${card.host_name} · GPU ${card.gpu_index}`;
    node.querySelector('.muted').textContent = `${card.gpu_name} · ${card.host_address}`;
    const badge = node.querySelector('.badge');
    badge.textContent = card.process_count > 0 ? '占用中' : '无进程';
    badge.classList.add(card.is_idle ? 'idle' : 'busy');
    const metrics = node.querySelector('.metrics');
    metrics.appendChild(metricRow('GPU util', `${card.utilization_gpu.toFixed(1)}%`, card.utilization_gpu));
    const memoryPercent = card.memory_total_mb ? (card.memory_used_mb / card.memory_total_mb) * 100 : 0;
    metrics.appendChild(metricRow('显存', `${card.memory_used_mb.toFixed(0)} / ${card.memory_total_mb.toFixed(0)} MB`, memoryPercent));
    metrics.appendChild(metricRow('活动用户', card.active_users.length ? card.active_users.join(', ') : '无人'));
    metrics.appendChild(metricRow('进程数', String(card.process_count)));
    metrics.appendChild(metricRow('更新时间', new Date(card.last_seen_at).toLocaleString('zh-CN')));
    currentGrid.appendChild(node);
  }
}

function renderGpuHistory(items) {
  gpuHistoryGrid.innerHTML = '';
  if (!items.length) {
    gpuHistoryGrid.textContent = '暂无历史聚合数据。先运行一次采集器，等待形成日聚合后这里会展示结果。';
    gpuHistoryGrid.classList.add('empty-state');
    return;
  }
  gpuHistoryGrid.classList.remove('empty-state');
  for (const item of items) {
    const card = document.createElement('article');
    card.className = 'history-card';
    card.innerHTML = `
      <h3>${item.host_name} · GPU ${item.gpu_index}</h3>
      <p class="muted">${item.gpu_name} · ${item.host_address}</p>
      <ul>
        <li><span>占用率</span><strong>${item.occupancy_rate}%</strong></li>
        <li><span>有效利用率</span><strong>${item.effective_utilization_rate}%</strong></li>
        <li><span>平均 GPU util</span><strong>${item.average_gpu_utilization}%</strong></li>
        <li><span>平均显存</span><strong>${item.average_memory_used_mb} MB</strong></li>
      </ul>
      <div class="sparkline"></div>
    `;
    const sparkline = card.querySelector('.sparkline');
    for (const point of item.trend) {
      const bar = document.createElement('span');
      bar.title = `${point.label}: ${point.average_gpu_utilization}%`;
      bar.style.height = `${Math.max(point.average_gpu_utilization, 8)}%`;
      sparkline.appendChild(bar);
    }
    gpuHistoryGrid.appendChild(card);
  }
}

function renderUsers(items) {
  userTableWrapper.innerHTML = '';
  if (!items.length) {
    userTableWrapper.textContent = '暂无用户聚合数据。';
    userTableWrapper.classList.add('empty-state');
    return;
  }
  userTableWrapper.classList.remove('empty-state');
  const table = document.createElement('table');
  table.className = 'table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>用户</th>
        <th>服务器</th>
        <th>GPU 使用时长</th>
        <th>非空闲时长</th>
        <th>平均 util</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector('tbody');
  for (const item of items) {
    const row = document.createElement('tr');
    row.innerHTML = `
      <td>${item.username}</td>
      <td>${item.host_name} · ${item.host_address}</td>
      <td>${item.gpu_hours} 小时</td>
      <td>${item.non_idle_hours} 小时</td>
      <td>${item.average_gpu_utilization}%</td>
    `;
    tbody.appendChild(row);
  }
  userTableWrapper.appendChild(table);
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed: ${response.status}`);
  }
  return response.json();
}

async function refreshAll() {
  if (!bootstrap.accessibleHosts.length) {
    return;
  }
  const windowDays = Number(windowSelect.value);
  const [current, gpuHistory, users] = await Promise.all([
    fetchJson('/api/status/current'),
    fetchJson(`/api/history/gpus?days=${windowDays}`),
    fetchJson(`/api/history/users?days=${windowDays}`),
  ]);
  renderCurrent(current);
  renderGpuHistory(gpuHistory);
  renderUsers(users);
}

refreshButton?.addEventListener('click', async () => {
  refreshButton.disabled = true;
  try {
    await fetchJson('/api/collector/run', { method: 'POST' });
    await refreshAll();
  } catch (error) {
    alert(`刷新失败：${error.message}`);
  } finally {
    refreshButton.disabled = false;
  }
});

windowSelect?.addEventListener('change', () => {
  refreshAll().catch(error => alert(`加载历史失败：${error.message}`));
});

logoutButton?.addEventListener('click', async () => {
  await fetchJson('/api/session/logout', { method: 'POST' });
  window.location.reload();
});

if (bootstrap.accessibleHosts.length) {
  refreshAll().catch(error => {
    currentGrid.textContent = `加载失败：${error.message}`;
  });
}
