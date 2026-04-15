const bootstrap = window.GPU_MONITOR_BOOTSTRAP || { sessionUsername: '', accessibleHosts: [] };
const currentGrid = document.getElementById('current-gpu-grid');
const statusSummary = document.getElementById('status-summary');
const gpuHistoryGrid = document.getElementById('gpu-history-grid');
const userTableWrapper = document.getElementById('user-table-wrapper');
const storageWrapper = document.getElementById('storage-wrapper');
const storageRefreshButton = document.getElementById('storage-refresh-button');
const killJobsWrapper = document.getElementById('kill-jobs-wrapper');
const jobsRefreshButton = document.getElementById('jobs-refresh-button');
const windowSelect = document.getElementById('window-select');
const userWindowSelect = document.getElementById('user-window-select');
const refreshButton = document.getElementById('refresh-button');
const logoutButton = document.getElementById('logout-button');
const ADMIN_USERS = new Set(['lifu', 'panzhou']);

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

function normalizeGpuModel(name) {
  const raw = (name || '').trim();
  const upper = raw.toUpperCase();
  if (upper.includes('RTX PRO 6000')) {
    return 'NVIDIA RTX Pro 6000';
  }
  if (upper.includes('L40S')) {
    return 'NVIDIA L40S';
  }
  return raw || 'Unknown model';
}

function officialMemoryByModel(model) {
  const upper = model.toUpperCase();
  if (upper.includes('RTX PRO 6000')) return 96;
  if (upper.includes('L40S')) return 48;
  return null;
}

function getHostSummary(cards) {
  const totalCards = cards.length;
  const hasProcessCount = cards.some(card => typeof card.process_count === 'number');
  const busyCards = hasProcessCount
    ? cards.filter(card => (card.process_count || 0) > 0).length
    : cards.filter(card => (card.occupancy_rate || 0) > 0).length;
  const models = [...new Set(cards.map(card => normalizeGpuModel(card.gpu_name)))];
  const modelLabel = models.length === 1 ? models[0] : `Mixed (${models.length})`;
  const officialMemory = models.length === 1 ? officialMemoryByModel(modelLabel) : null;
  const memoryTotals = [...new Set(cards.map(card => card.memory_total_mb))].filter(Boolean);
  const memoryLabel = officialMemory
    ? `${officialMemory} GB/card`
    : (memoryTotals.length === 1 ? `${mbToGb(memoryTotals[0])} GB/card` : 'Mixed specs');
  return { totalCards, busyCards, modelLabel, memoryLabel };
}

function getHistoryHostSummary(cards) {
  const totalCards = cards.length;
  const models = [...new Set(cards.map(card => normalizeGpuModel(card.gpu_name)))];
  const modelLabel = models.length === 1 ? models[0] : `Mixed (${models.length})`;
  const officialMemory = models.length === 1 ? officialMemoryByModel(modelLabel) : null;
  const memoryLabel = officialMemory ? `${officialMemory} GB/card` : '--';
  return { totalCards, modelLabel, memoryLabel };
}

function renderSummary(cards) {
  statusSummary.innerHTML = '';
  const total = cards.length;
  const busy = cards.filter(card => card.process_count > 0).length;
  const idle = cards.filter(card => card.is_idle).length;
  const avgUtil = total ? (cards.reduce((sum, card) => sum + card.utilization_gpu, 0) / total).toFixed(1) : '0.0';
  const values = [
    ['Total GPUs', total],
    ['Busy', busy],
    ['Idle', idle],
    ['Avg util', `${avgUtil}%`],
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
    <span class="server-summary-badge">Model: ${summary.modelLabel}</span>
    <span class="server-summary-badge">Memory: ${summary.memoryLabel}</span>
    <span class="server-summary-badge">Cards: ${summary.totalCards}</span>
    <span class="server-summary-badge">Busy: ${summary.busyCards}</span>
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
  const collapsibleBody = `<div class="server-card-grid"></div>`;

  if (collapsible) {
    section.innerHTML = `
      <details class="server-details">
        <summary class="server-summary">
          <div class="server-summary-main">${hostName} · ${hostAddress}</div>
          <div class="server-summary-list">${summaryBadges}</div>
        </summary>
        <div class="server-body">${collapsibleBody}</div>
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
      <div class="gpu-card-index"></div>
      <div class="gpu-card-content">
        <div class="metrics"></div>
      </div>
    `;
    node = document.createDocumentFragment();
    node.appendChild(fallback);
  }

  node.querySelector('.gpu-card-index').textContent = `GPU ${card.gpu_index}`;
  const metrics = node.querySelector('.metrics');
  metrics.appendChild(metricRow('GPU util', `${card.utilization_gpu.toFixed(1)}%`, card.utilization_gpu));
  const memoryPercent = card.memory_total_mb ? (card.memory_used_mb / card.memory_total_mb) * 100 : 0;
  metrics.appendChild(metricRow('Memory', `${card.memory_used_mb.toFixed(0)} / ${card.memory_total_mb.toFixed(0)} MB`, memoryPercent));
  metrics.appendChild(metricRow('Active users', card.active_users.length ? card.active_users.join(', ') : 'None'));

  return node;
}

function renderCurrent(cards) {
  currentGrid.innerHTML = '';
  if (!cards.length) {
    currentGrid.textContent = 'No current data yet. Complete access validation, then wait for auto collection or refresh manually.';
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
    gpuHistoryGrid.textContent = 'No historical aggregates yet. Run collection and wait for daily aggregation to appear here.';
    gpuHistoryGrid.classList.add('empty-state');
    return;
  }
  gpuHistoryGrid.classList.remove('empty-state');
  const grouped = groupByHost(items);
  for (const group of Object.values(grouped)) {
    const summary = getHistoryHostSummary(group.items);
    const section = document.createElement('section');
    section.className = 'server-section history-lite';
    section.innerHTML = `
      <details class="server-details">
        <summary class="server-summary">
          <div class="server-summary-main">${group.hostName} · ${group.hostAddress}</div>
          <div class="server-summary-list">
            <span class="server-summary-badge">Model: ${summary.modelLabel}</span>
            <span class="server-summary-badge">Memory: ${summary.memoryLabel}</span>
            <span class="server-summary-badge">Cards: ${summary.totalCards}</span>
          </div>
        </summary>
        <div class="server-body">
          <div class="server-card-grid"></div>
        </div>
      </details>
    `;
    const grid = section.querySelector('.server-card-grid');
    for (const item of group.items.sort((a, b) => a.gpu_index - b.gpu_index)) {
      const card = document.createElement('article');
      card.className = 'history-card';
      card.innerHTML = `
        <div class="gpu-card-index">GPU ${item.gpu_index}</div>
        <div class="gpu-card-content">
          <ul>
            <li><span>Occupancy</span><strong>${item.occupancy_rate}%</strong></li>
            <li><span>Effective utilization</span><strong>${item.effective_utilization_rate}%</strong></li>
            <li><span>Avg GPU util</span><strong>${item.average_gpu_utilization}%</strong></li>
            <li><span>Avg memory</span><strong>${item.average_memory_used_mb} MB</strong></li>
          </ul>
        </div>
      `;
      grid.appendChild(card);
    }
    gpuHistoryGrid.appendChild(section);
  }
}

function renderUsers(items) {
  userTableWrapper.innerHTML = '';
  if (!items.length) {
    userTableWrapper.textContent = 'No user aggregates yet.';
    userTableWrapper.classList.add('empty-state');
    return;
  }
  userTableWrapper.classList.remove('empty-state');

  const wrapper = document.createElement('div');
  wrapper.className = 'user-list';

  for (const item of items) {
    const breakdown = (item.server_breakdown || []).map(server => ({
      ...server,
      gpu_type: server.gpu_type || 'Unknown model',
    }));
    const block = document.createElement('article');
    block.className = 'user-card';
    block.innerHTML = `
      <div class="user-card-head">
        <div>
          <h3>${item.username}</h3>
          <p class="muted">GPU types: ${breakdown.map(server => server.gpu_type).join(', ')}</p>
        </div>
        <div class="user-summary-list">
          <span class="server-summary-badge">Total: ${item.gpu_hours} h</span>
          <span class="server-summary-badge">Daily avg: ${item.daily_average_gpu_hours} h</span>
          <span class="server-summary-badge">Non-idle: ${item.non_idle_hours} h</span>
          <span class="server-summary-badge">Avg util: ${item.average_gpu_utilization}%</span>
          <span class="server-summary-badge">Avg mem: ${mbToGb(item.average_memory_used_mb || 0)} GB</span>
        </div>
      </div>
      <details class="user-details">
        <summary>View per-host details</summary>
        <table class="table compact-table">
          <thead>
            <tr>
              <th>GPU type</th>
              <th>GPU hours</th>
              <th>Daily avg hours</th>
              <th>Non-idle hours</th>
              <th>Avg util</th>
              <th>Avg mem</th>
            </tr>
          </thead>
          <tbody>
            ${breakdown
              .map(
                server => `
                  <tr>
                    <td>${server.gpu_type}</td>
                    <td>${server.gpu_hours} h</td>
                    <td>${server.daily_average_gpu_hours} h</td>
                    <td>${server.non_idle_hours} h</td>
                    <td>${server.average_gpu_utilization}%</td>
                    <td>${mbToGb(server.average_memory_used_mb || 0)} GB</td>
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

function bytesToHuman(bytes) {
  const b = Number(bytes) || 0;
  if (b >= 1024 ** 4) return `${(b / 1024 ** 4).toFixed(2)} TB`;
  if (b >= 1024 ** 3) return `${(b / 1024 ** 3).toFixed(2)} GB`;
  if (b >= 1024 ** 2) return `${(b / 1024 ** 2).toFixed(2)} MB`;
  if (b >= 1024) return `${(b / 1024).toFixed(2)} KB`;
  return `${b} B`;
}

function renderStorage(items) {
  if (!storageWrapper) return;
  storageWrapper.innerHTML = '';
  if (!items.length) {
    storageWrapper.textContent = 'No storage usage collected yet.';
    storageWrapper.classList.add('empty-state');
    return;
  }
  storageWrapper.classList.remove('empty-state');

  const wrapper = document.createElement('div');
  wrapper.className = 'user-list';

  for (const item of items) {
    const breakdown = (item.server_breakdown || []).slice().sort((a, b) => b.used_bytes - a.used_bytes);
    const rows = breakdown
      .map(
        entry => `
          <tr>
            <td>${entry.host_name}</td>
            <td>${entry.host_address}</td>
            <td>${bytesToHuman(entry.used_bytes)}</td>
            <td>${entry.updated_at ? new Date(entry.updated_at).toLocaleString() : '--'}</td>
          </tr>
        `
      )
      .join('');
    const block = document.createElement('article');
    block.className = 'user-card';
    block.innerHTML = `
      <div class="user-card-head">
        <div>
          <h3>${item.username}</h3>
          <p class="muted">${breakdown.length} host${breakdown.length === 1 ? '' : 's'}</p>
        </div>
        <div class="user-summary-list">
          <span class="server-summary-badge">Total: ${bytesToHuman(item.total_used_bytes)}</span>
        </div>
      </div>
      <details class="user-details">
        <summary>View per-host details</summary>
        <table class="table compact-table">
          <thead>
            <tr>
              <th>Host</th>
              <th>Address</th>
              <th>Used</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </details>
    `;
    wrapper.appendChild(block);
  }

  storageWrapper.appendChild(wrapper);
}

async function requestJobExtension(jobId, hours, reason) {
  return fetchJson(`/api/jobs/${jobId}/extension`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ hours, reason }),
  });
}

async function killJobNow(jobId) {
  return fetchJson(`/api/jobs/${jobId}/kill`, { method: 'POST' });
}

function renderKillJobs(items) {
  if (!killJobsWrapper) return;
  killJobsWrapper.innerHTML = '';
  if (!items.length) {
    killJobsWrapper.textContent = 'No jobs are currently marked for kill.';
    killJobsWrapper.classList.add('empty-state');
    return;
  }
  killJobsWrapper.classList.remove('empty-state');
  const isAdmin = ADMIN_USERS.has(bootstrap.sessionUsername || '');
  const table = document.createElement('table');
  table.className = 'table compact-table';
  table.innerHTML = `
    <thead>
      <tr>
        ${isAdmin ? '<th>User</th>' : ''}
        <th>PID</th>
        <th>Machine</th>
        <th>GPU</th>
        <th>Util</th>
        <th>Memory</th>
        <th>Status</th>
        <th>Kill at</th>
        ${isAdmin ? '<th>Extension</th><th>Total run</th><th>Reason</th><th>Action</th>' : '<th>Request extension</th>'}
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector('tbody');
  for (const item of items) {
    const tr = document.createElement('tr');
    const extensionDisplay = item.extended_until ? new Date(item.extended_until).toLocaleString() : '--';
    const reasonDisplay = item.extension_reason || '--';
    if (isAdmin) {
      tr.innerHTML = `
        <td>${item.username}</td>
        <td>${item.pid}</td>
        <td>${item.host_address}</td>
        <td>${item.gpu_index}</td>
        <td>${Number(item.utilization_gpu).toFixed(1)}%</td>
        <td>${Number(item.memory_used_mb).toFixed(0)} MB</td>
        <td>${item.status}</td>
        <td>${new Date(item.kill_after).toLocaleString()}</td>
        <td>${extensionDisplay}</td>
        <td>${item.total_running_hours} h</td>
        <td>${reasonDisplay}</td>
        <td><button class="danger" data-kill-id="${item.id}">Kill now</button></td>
      `;
    } else {
      tr.innerHTML = `
        <td>${item.pid}</td>
        <td>${item.host_address}</td>
        <td>${item.gpu_index}</td>
        <td>${Number(item.utilization_gpu).toFixed(1)}%</td>
        <td>${Number(item.memory_used_mb).toFixed(0)} MB</td>
        <td>${item.status}</td>
        <td>${new Date(item.kill_after).toLocaleString()}</td>
        <td>
          <div class="extension-controls">
            <select data-hours-id="${item.id}">
              <option value="4">4 hours</option>
              <option value="8">8 hours</option>
              <option value="12">12 hours</option>
              <option value="24">24 hours</option>
            </select>
            <input type="text" data-reason-id="${item.id}" placeholder="Reason" />
            <button data-extend-id="${item.id}">Submit</button>
          </div>
        </td>
      `;
    }
    tbody.appendChild(tr);
  }
  killJobsWrapper.appendChild(table);
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
  const userWindowDays = Number(userWindowSelect?.value || windowSelect.value);
  const [current, gpuHistory, users, storage, killJobs] = await Promise.all([
    fetchJson('/api/status/current'),
    fetchJson(`/api/history/gpus?days=${windowDays}`),
    fetchJson(`/api/history/users?days=${userWindowDays}`),
    fetchJson('/api/storage/users'),
    fetchJson('/api/jobs/to-be-killed'),
  ]);
  renderCurrent(current);
  renderGpuHistory(gpuHistory);
  renderUsers(users);
  renderStorage(storage);
  renderKillJobs(killJobs);
}

async function refreshUsers() {
  if (!bootstrap.accessibleHosts.length) {
    return;
  }
  const userWindowDays = Number(userWindowSelect?.value || windowSelect?.value || 30);
  const users = await fetchJson(`/api/history/users?days=${userWindowDays}`);
  renderUsers(users);
}

refreshButton?.addEventListener('click', async () => {
  refreshButton.disabled = true;
  try {
    const response = await fetchJson('/api/status/refresh', { method: 'POST' });
    renderCurrent(response.current_status || []);
    if (response.errors?.length) {
      alert(`Refresh failed on some hosts:\n${response.errors.join('\n')}`);
    }
  } catch (error) {
    alert(`Refresh failed: ${error.message}`);
  } finally {
    refreshButton.disabled = false;
  }
});

windowSelect?.addEventListener('change', () => {
  refreshAll().catch(error => alert(`Failed to load history: ${error.message}`));
});

userWindowSelect?.addEventListener('change', () => {
  refreshUsers().catch(error => alert(`Failed to load user summary: ${error.message}`));
});

storageRefreshButton?.addEventListener('click', async () => {
  storageRefreshButton.disabled = true;
  const originalLabel = storageRefreshButton.textContent;
  storageRefreshButton.textContent = 'Refreshing...';
  try {
    const response = await fetchJson('/api/storage/refresh', { method: 'POST' });
    renderStorage(response.storage || []);
    if (response.errors?.length) {
      alert(`Storage refresh reported errors:\n${response.errors.join('\n')}`);
    }
  } catch (error) {
    alert(`Storage refresh failed: ${error.message}`);
  } finally {
    storageRefreshButton.disabled = false;
    storageRefreshButton.textContent = originalLabel;
  }
});

jobsRefreshButton?.addEventListener('click', async () => {
  jobsRefreshButton.disabled = true;
  try {
    const rows = await fetchJson('/api/jobs/to-be-killed');
    renderKillJobs(rows);
  } catch (error) {
    alert(`Jobs refresh failed: ${error.message}`);
  } finally {
    jobsRefreshButton.disabled = false;
  }
});

killJobsWrapper?.addEventListener('click', async event => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const killId = target.getAttribute('data-kill-id');
  const extendId = target.getAttribute('data-extend-id');
  if (killId) {
    try {
      await killJobNow(killId);
      const rows = await fetchJson('/api/jobs/to-be-killed');
      renderKillJobs(rows);
    } catch (error) {
      alert(`Kill failed: ${error.message}`);
    }
    return;
  }
  if (extendId) {
    const hoursEl = killJobsWrapper.querySelector(`[data-hours-id="${extendId}"]`);
    const reasonEl = killJobsWrapper.querySelector(`[data-reason-id="${extendId}"]`);
    const hours = Number(hoursEl?.value || 4);
    const reason = String(reasonEl?.value || '').trim();
    if (!reason) {
      alert('Please provide a reason for extension.');
      return;
    }
    try {
      await requestJobExtension(extendId, hours, reason);
      const rows = await fetchJson('/api/jobs/to-be-killed');
      renderKillJobs(rows);
    } catch (error) {
      alert(`Extension failed: ${error.message}`);
    }
  }
});

logoutButton?.addEventListener('click', async () => {
  await fetchJson('/api/session/logout', { method: 'POST' });
  window.location.reload();
});

if (bootstrap.accessibleHosts.length) {
  refreshAll().catch(error => {
    currentGrid.textContent = `Load failed: ${error.message}`;
  });
}
