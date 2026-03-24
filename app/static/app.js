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

function groupByHost(items) {
  return items.reduce((acc, item) => {
    const key = `${item.host_address}`;
    if (!acc[key]) {
      acc[key] = {
        hostName: item.host_name,
        hostAddress: item.host_address,
        items: [],
      };
    }
    acc[key].items.push(item);
    return acc;
  }, {});
}

function mbToGb(mb) {
  return (mb / 1024).toFixed(1);
}

function getHostSummary(cards) {
  const totalCards = cards.length;
  const hasProcessCount = cards.some(card => typeof card.process_count === 'number');
  const busyCards = hasProcessCount
    ? cards.filter(card => (card.process_count || 0) > 0).length
    : cards.filter(card => (card.occupancy_rate || 0) > 0).length;
  const models = [...new Set(cards.map(card => card.gpu_name))];
  const modelLabel = models.length === 1 ? models[0] : `Mixed (${models.length})`;
  const memoryTotals = [...new Set(cards.map(card => card.memory_total_mb))].filter(Boolean);
  const memoryLabel = memoryTotals.length === 1 ? `${mbToGb(memoryTotals[0])} GB/卡` : '多规格';
  return { totalCards, busyCards, modelLabel, memoryLabel };
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

function createServerSection(hostName, hostAddress, cards, { collapsible = true } = {}) {
  const section = document.createElement('section');
  section.className = 'server-section';

  const summary = getHostSummary(cards);
  const summaryBadges = `
    <span class="server-summary-badge">型号：${summary.modelLabel}</span>
    <span class="server-summary-badge">显存：${summary.memoryLabel}</span>
    <span class="server-summary-badge">总卡：${summary.totalCards}</span>
    <span class="server-summary-badge">占用：${summary.busyCards}</span>
  `;

  const content = `
    <div class="server-section-head">
      <div>
        <div class="server-title-row">
          <span class="server-chip">SERVER</span>
          <h3>${hostName}</h3>
        </div>
        <p class="muted">${hostAddress}</p>
      </div>
      <div class="server-summary-list">${summaryBadges}</div>
    </div>
    <div class="server-card-grid"></div>
  `;

  if (collapsible) {
    section.innerHTML = `
      <details class="server-details">
        <summary class="server-summary">
          <div class="server-summary-main">${hostName} · ${hostAddress}</div>
          <div class="server-summary-list">${summaryBadges}</div>
        </summary>
        <div class="server-body">${content}</div>
      </details>
    `;
  } else {
    section.innerHTML = content;
  }

  return section;
}

function buildGpuCardNode(card) {
  const template = document.getElementById('gpu-card-template');
  let node;
  if (template?.content) {
    node = template.content.cloneNode(true);
  } else {
    const fallback = document.createElement('article');
    fallback.className = 'gpu-card';
    fallback.innerHTML = `
      <div class="gpu-card-head">
        <div>
          <h3></h3>
          <p class="muted"></p>
        </div>
      </div>
      <div class="metrics"></div>
    `;
    node = document.createDocumentFragment();
    node.appendChild(fallback);
  }

  node.querySelector('h3').textContent = `GPU ${card.gpu_index}`;
  node.querySelector('.muted').textContent = `${card.gpu_name}`;
  const metrics = node.querySelector('.metrics');
  metrics.appendChild(metricRow('GPU util', `${card.utilization_gpu.toFixed(1)}%`, card.utilization_gpu));
  const memoryPercent = card.memory_total_mb ? (card.memory_used_mb / card.memory_total_mb) * 100 : 0;
  metrics.appendChild(metricRow('显存', `${card.memory_used_mb.toFixed(0)} / ${card.memory_total_mb.toFixed(0)} MB`, memoryPercent));
  metrics.appendChild(metricRow('活动用户', card.active_users.length ? card.active_users.join(', ') : '无人'));

  return node;
}

function renderCurrent(cards) {
  currentGrid.innerHTML = '';
  if (!cards.length) {
    currentGrid.textContent = '暂无数据。请先验证连接，并等待自动采集或手动刷新当前状态。';
    currentGrid.classList.add('empty-state');
    return;
  }
  currentGrid.classList.remove('empty-state');
  renderSummary(cards);
  const grouped = groupByHost(cards);
  for (const group of Object.values(grouped)) {
    const section = createServerSection(group.hostName, group.hostAddress, group.items, { collapsible: true });
    const grid = section.querySelector('.server-card-grid');
    for (const card of group.items.sort((a, b) => a.gpu_index - b.gpu_index)) {
      const node = buildGpuCardNode(card);
      grid.appendChild(node);
    }
    currentGrid.appendChild(section);
  }
}

function renderGpuHistory(items) {
  gpuHistoryGrid.innerHTML = '';
  if (!items.length) {
    gpuHistoryGrid.textContent = '暂无历史聚合数据。先运行自动采集，等待形成日聚合后这里会展示结果。';
    gpuHistoryGrid.classList.add('empty-state');
    return;
  }
  gpuHistoryGrid.classList.remove('empty-state');
  const grouped = groupByHost(items);
  for (const group of Object.values(grouped)) {
    const section = createServerSection(group.hostName, group.hostAddress, group.items, { collapsible: true });
    const grid = section.querySelector('.server-card-grid');
    for (const item of group.items.sort((a, b) => a.gpu_index - b.gpu_index)) {
      const card = document.createElement('article');
      card.className = 'history-card';
      card.innerHTML = `
        <h3>GPU ${item.gpu_index}</h3>
        <p class="muted">${item.gpu_name}</p>
        <ul>
          <li><span>占用率</span><strong>${item.occupancy_rate}%</strong></li>
          <li><span>有效利用率</span><strong>${item.effective_utilization_rate}%</strong></li>
          <li><span>平均 GPU util</span><strong>${item.average_gpu_utilization}%</strong></li>
          <li><span>平均显存</span><strong>${item.average_memory_used_mb} MB</strong></li>
        </ul>
      `;
      grid.appendChild(card);
    }
    gpuHistoryGrid.appendChild(section);
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

  const wrapper = document.createElement('div');
  wrapper.className = 'user-list';

  for (const item of items) {
    const block = document.createElement('article');
    block.className = 'user-card';
    block.innerHTML = `
      <div class="user-card-head">
        <div>
          <h3>${item.username}</h3>
          <p class="muted">涉及服务器：${item.host_names.join(', ')}</p>
        </div>
        <div class="user-summary-list">
          <span class="server-summary-badge">总时长：${item.gpu_hours} 小时</span>
          <span class="server-summary-badge">日均：${item.daily_average_gpu_hours} 小时</span>
          <span class="server-summary-badge">非空闲：${item.non_idle_hours} 小时</span>
          <span class="server-summary-badge">平均 util：${item.average_gpu_utilization}%</span>
        </div>
      </div>
      <details class="user-details">
        <summary>查看各服务器详情</summary>
        <table class="table compact-table">
          <thead>
            <tr>
              <th>服务器</th>
              <th>GPU 使用时长</th>
              <th>日均使用时长</th>
              <th>非空闲时长</th>
              <th>平均 util</th>
            </tr>
          </thead>
          <tbody>
            ${item.server_breakdown
              .map(
                server => `
                  <tr>
                    <td>${server.host_name}<div class="table-subtext">${server.host_address}</div></td>
                    <td>${server.gpu_hours} 小时</td>
                    <td>${server.daily_average_gpu_hours} 小时</td>
                    <td>${server.non_idle_hours} 小时</td>
                    <td>${server.average_gpu_utilization}%</td>
                  </tr>
                `
              )
              .join('')}
          </tbody>
        </table>
      </details>
    `;
    wrapper.appendChild(block);
  }

  userTableWrapper.appendChild(wrapper);
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
    const response = await fetchJson('/api/status/refresh', { method: 'POST' });
    renderCurrent(response.current_status || []);
    if (response.errors?.length) {
      alert(`部分服务器刷新失败：\n${response.errors.join('\n')}`);
    }
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
