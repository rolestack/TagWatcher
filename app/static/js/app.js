/**
 * TagWatcher - Main JavaScript
 * Uses Alpine.js for reactivity (loaded via CDN in base.html)
 */

// ─── Global Toast ─────────────────────────────────────────────────────────────

function twToast() {
    return {
        toasts: [],
        _nextId: 0,
        show({ message, type = 'error', duration = 5000 }) {
            const id = ++this._nextId;
            this.toasts.push({ id, message, type, visible: true });
            setTimeout(() => this.dismiss(id), duration);
        },
        dismiss(id) {
            const t = this.toasts.find(t => t.id === id);
            if (t) t.visible = false;
            setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, 200);
        },
    };
}

function twShowToast(message, type = 'error', duration = 5000) {
    window.dispatchEvent(new CustomEvent('tw:toast', { detail: { message, type, duration } }));
}

// ─── Space Tech Icons ─────────────────────────────────────────────────────────

const TW_TECH_ICONS = {
    __docker: `<svg viewBox="0 0 24 24" fill="#2496ED" style="width:1.5rem;height:1.5rem">
        <rect x="5.5" y="3.5" width="4" height="3" rx="0.5"/>
        <rect x="10.5" y="3.5" width="4" height="3" rx="0.5"/>
        <rect x="0.5" y="7.5" width="4" height="3" rx="0.5"/>
        <rect x="5.5" y="7.5" width="4" height="3" rx="0.5"/>
        <rect x="10.5" y="7.5" width="4" height="3" rx="0.5"/>
        <path d="M22.7 9.1c-.5-.3-1.5-.6-2.6-.4-.3-1.3-1.2-2-1.3-2l-.3-.2-.2.3c-.3.4-.4.9-.5 1.3-.2.9 0 1.8.3 2.5-.3.1-.7.3-1.4.3H.5c-.1.9 0 3 1.3 4.8.9 1.1 2.1 1.6 3.7 1.6 3.6 0 6.3-1.7 7.7-4.8.5 0 1.5 0 2-.9l.1-.2-.1-.1z"/>
    </svg>`,
    __k8s: `<svg viewBox="0 0 24 24" style="width:1.5rem;height:1.5rem">
        <circle cx="12" cy="12" r="2.5" fill="#326CE5"/>
        <circle cx="12" cy="12" r="9.5" fill="none" stroke="#326CE5" stroke-width="1.5"/>
        <line x1="12" y1="9.5" x2="12" y2="2.5" stroke="#326CE5" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="13.95" y1="10.44" x2="19.43" y2="6.08" stroke="#326CE5" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="14.44" y1="11.44" x2="21.26" y2="14.11" stroke="#326CE5" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="13.08" y1="14.25" x2="16.12" y2="20.56" stroke="#326CE5" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="10.92" y1="14.25" x2="7.88" y2="20.56" stroke="#326CE5" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="9.56" y1="11.44" x2="2.74" y2="14.11" stroke="#326CE5" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="10.05" y1="10.44" x2="4.57" y2="6.08" stroke="#326CE5" stroke-width="1.5" stroke-linecap="round"/>
    </svg>`,
};

// ─── Log Viewer Component ────────────────────────────────────────────────────

function logViewer(containerId) {
    return {
        logs: '',
        connected: false,
        connecting: false,
        error: null,
        ws: null,
        autoScroll: true,

        init() {
            this.$watch('logs', () => {
                if (this.autoScroll) {
                    this.$nextTick(() => {
                        const el = this.$refs.logOutput;
                        if (el) el.scrollTop = el.scrollHeight;
                    });
                }
            });
            // Auto-connect when redirected here after an image update
            if (new URLSearchParams(window.location.search).get('logs') === '1') {
                history.replaceState(null, '', window.location.pathname);
                this.$nextTick(() => this.connect());
            }
        },

        connect() {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) return;
            this.connecting = true;
            this.error = null;

            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/containers/${containerId}/logs`;

            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                this.connected = true;
                this.connecting = false;
                this.logs += 'Connected to log stream...\n';
            };

            this.ws.onmessage = (event) => {
                if (event.data) {
                    this.logs += event.data;
                    // Keep log buffer from growing too large (keep last 5000 lines)
                    const lines = this.logs.split('\n');
                    if (lines.length > 5000) {
                        this.logs = lines.slice(lines.length - 5000).join('\n');
                    }
                }
            };

            this.ws.onclose = (event) => {
                this.connected = false;
                this.connecting = false;
                if (event.code !== 1000) {
                    this.logs += `\n[Connection closed: code=${event.code}]\n`;
                }
            };

            this.ws.onerror = (err) => {
                this.connected = false;
                this.connecting = false;
                this.error = 'WebSocket connection failed';
                this.logs += '\n[WebSocket error]\n';
            };
        },

        disconnect() {
            if (this.ws) {
                this.ws.close(1000, 'User disconnected');
                this.ws = null;
            }
            this.connected = false;
        },

        clear() {
            this.logs = '';
        },

        copyLogs() {
            navigator.clipboard.writeText(this.logs).then(() => {
                // Brief visual feedback
                const btn = this.$el.querySelector('[data-copy-btn]');
                if (btn) {
                    const orig = btn.textContent;
                    btn.textContent = 'Copied!';
                    setTimeout(() => { btn.textContent = orig; }, 1500);
                }
            });
        },

        onScroll() {
            const el = this.$refs.logOutput;
            if (!el) return;
            const threshold = 50;
            this.autoScroll = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
        },
    };
}


// ─── Notification Channel Fields ─────────────────────────────────────────────

const TW_CHANNEL_FIELDS = {
    slack: [
        { key: 'webhook_url', label: 'Webhook URL', type: 'url', required: true,
          placeholder: 'https://hooks.slack.com/services/...' },
    ],
    discord: [
        { key: 'webhook_url', label: 'Webhook URL', type: 'url', required: true,
          placeholder: 'https://discord.com/api/webhooks/...' },
    ],
    telegram: [
        { key: 'bot_token', label: 'Bot Token', type: 'text', required: true,
          placeholder: '1234567890:AAABBB...' },
        { key: 'chat_id', label: 'Chat ID', type: 'text', required: true,
          placeholder: '-1001234567890' },
    ],
    zulip: [
        { key: 'site', label: 'Zulip Site URL', type: 'url', required: true,
          placeholder: 'https://yourorg.zulipchat.com' },
        { key: 'email', label: 'Bot Email', type: 'email', required: true,
          placeholder: 'tagwatcher-bot@yourorg.zulipchat.com' },
        { key: 'api_key', label: 'API Key', type: 'password', required: true,
          placeholder: 'abcdef1234567890' },
        { key: 'stream', label: 'Stream', type: 'text', required: false,
          placeholder: 'general' },
        { key: 'topic', label: 'Topic', type: 'text', required: false,
          placeholder: 'TagWatcher Updates' },
    ],
    mattermost: [
        { key: 'webhook_url', label: 'Incoming Webhook URL', type: 'url', required: true,
          placeholder: 'https://mattermost.example.com/hooks/...' },
        { key: 'channel', label: 'Channel (optional)', type: 'text', required: false,
          placeholder: '#general' },
        { key: 'username', label: 'Username (optional)', type: 'text', required: false,
          placeholder: 'TagWatcher' },
    ],
    teams: [
        { key: 'webhook_url', label: 'Incoming Webhook URL', type: 'url', required: true,
          placeholder: 'https://company.webhook.office.com/webhookb2/...' },
    ],
};


// ─── Notification Channel Form ────────────────────────────────────────────────

function notificationForm() {
    return {
        showModal: false,
        channelType: 'slack',
        name: '',
        config: {},

        fieldsByType: TW_CHANNEL_FIELDS,

        get currentFields() {
            return this.fieldsByType[this.channelType] || [];
        },

        get configJson() {
            return JSON.stringify(this.config);
        },

        setField(key, value) {
            this.config = { ...this.config, [key]: value };
        },

        getField(key) {
            return this.config[key] || '';
        },

        resetConfig() {
            this.config = {};
        },

        open() {
            this.showModal = true;
            this.resetConfig();
        },

        close() {
            this.showModal = false;
        },
    };
}


// ─── Notification Channel Editor ─────────────────────────────────────────────

function channelEditor(spaceId) {
    return {
        showModal: false,
        channelId: null,
        channelType: '',
        name: '',
        config: {},
        isActive: true,
        saving: false,
        saved: false,
        error: null,

        fieldsByType: TW_CHANNEL_FIELDS,

        get currentFields() {
            return this.fieldsByType[this.channelType] || [];
        },

        open(ch) {
            this.channelId = ch.id;
            this.channelType = ch.type;
            this.name = ch.name;
            this.config = Object.assign({}, ch.config);
            this.isActive = ch.is_active;
            this.saved = false;
            this.error = null;
            this.showModal = true;
        },

        close() {
            this.showModal = false;
        },

        setField(key, value) {
            this.config = { ...this.config, [key]: value };
        },

        getField(key) {
            return this.config[key] || '';
        },

        async save() {
            this.saving = true;
            this.error = null;
            try {
                const resp = await fetch(
                    `/spaces/${spaceId}/notifications/${this.channelId}`,
                    {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            name: this.name,
                            config: this.config,
                            is_active: this.isActive,
                        }),
                    }
                );
                if (resp.ok) {
                    this.saved = true;
                    setTimeout(() => { this.close(); location.reload(); }, 600);
                } else {
                    const d = await resp.json();
                    this.error = d.detail || 'Update failed';
                }
            } catch (e) {
                this.error = e.message;
            } finally {
                this.saving = false;
            }
        },
    };
}


// ─── Admin User Management ────────────────────────────────────────────────────

function userAdmin() {
    return {
        loading: {},
        states: {},
        showCreateUser: false,

        initUser(uid, active, admin) {
            this.states[uid] = { active, admin };
        },

        async toggleActive(userId) {
            if (this.loading[userId]) return;
            const cur = this.states[userId].active;
            this.states[userId].active = !cur;
            this.loading[userId] = true;
            try {
                const resp = await fetch(`/admin/users/${userId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ is_active: !cur }),
                });
                if (!resp.ok) this.states[userId].active = cur;
            } catch (e) {
                this.states[userId].active = cur;
            } finally {
                this.loading[userId] = false;
            }
        },

        async toggleAdmin(userId) {
            if (this.loading[userId]) return;
            const cur = this.states[userId].admin;
            this.states[userId].admin = !cur;
            this.loading[userId] = true;
            try {
                const resp = await fetch(`/admin/users/${userId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ is_admin: !cur }),
                });
                if (!resp.ok) this.states[userId].admin = cur;
            } catch (e) {
                this.states[userId].admin = cur;
            } finally {
                this.loading[userId] = false;
            }
        },

        async deleteUser(userId, userName) {
            if (!confirm(`Delete user "${userName}"? This cannot be undone.`)) return;
            this.loading[userId] = true;
            try {
                const resp = await fetch(`/admin/users/${userId}`, { method: 'DELETE' });
                const data = await resp.json();
                if (resp.ok) {
                    location.reload();
                } else {
                    alert(data.detail || 'Delete failed.');
                }
            } catch (e) {
                alert('Delete failed.');
            } finally {
                this.loading[userId] = false;
            }
        },
    };
}


// ─── Container Image Update (pull & recreate) ────────────────────────────────

function containerUpdater(containerId) {
    return {
        confirming: false,
        updating: false,

        async applyUpdate() {
            this.confirming = false;
            this.updating = true;
            try {
                const resp = await fetch(`/containers/${containerId}/update`, { method: 'POST' });
                if (resp.ok) {
                    const url = new URL(window.location.href);
                    url.searchParams.set('logs', '1');
                    window.location.href = url.toString();
                } else {
                    const data = await resp.json().catch(() => ({}));
                    twShowToast(data.detail || 'Container update failed.', 'error');
                }
            } catch (e) {
                twShowToast(e.message || 'Container update failed.', 'error');
            } finally {
                this.updating = false;
            }
        },
    };
}


// ─── Container Reload (re-sync from Docker) ───────────────────────────────────

function containerReloader(containerId) {
    return {
        reloading: false,

        async reload() {
            this.reloading = true;
            try {
                const resp = await fetch(`/containers/${containerId}/reload`, { method: 'POST' });
                if (resp.ok) {
                    window.location.reload();
                } else {
                    const data = await resp.json().catch(() => ({}));
                    alert('Reload failed: ' + (data.detail || 'Unknown error'));
                }
            } catch (e) {
                alert('Reload failed: ' + e.message);
            } finally {
                this.reloading = false;
            }
        },
    };
}


// ─── Container Check Trigger ──────────────────────────────────────────────────

function containerChecker(containerId) {
    return {
        checking: false,
        result: null,

        async check() {
            this.checking = true;
            this.result = null;
            try {
                // Pass the currently-selected strategy from the UI (even before Save).
                const strat = window.__twContainerStrategy || '';
                const url = `/containers/${containerId}/check` +
                    (strat ? `?strategy_override=${encodeURIComponent(strat)}` : '');
                const resp = await fetch(url, { method: 'POST' });
                if (resp.ok) {
                    this.result = await resp.json();
                    // Update banner and Latest Tag card immediately without reload.
                    window.dispatchEvent(new CustomEvent('tw:check-result', { detail: this.result }));
                } else {
                    this.result = { error: 'Check failed' };
                }
            } catch (e) {
                this.result = { error: e.message };
            } finally {
                this.checking = false;
            }
        },
    };
}


// ─── Host Sync Trigger ────────────────────────────────────────────────────────

function hostSyncer(spaceId, hostId) {
    return {
        syncing: false,
        message: null,

        async sync() {
            this.syncing = true;
            this.message = null;
            try {
                const resp = await fetch(`/spaces/${spaceId}/hosts/${hostId}/sync`, {
                    method: 'POST',
                });
                if (resp.redirected || resp.url.includes('/auth/login')) {
                    window.location.href = '/auth/login';
                    return;
                }
                if (resp.ok) {
                    this.message = { type: 'success', text: 'Sync completed!' };
                    setTimeout(() => location.reload(), 1500);
                } else {
                    const data = await resp.json();
                    this.message = { type: 'error', text: data.detail || 'Sync failed' };
                }
            } catch (e) {
                this.message = { type: 'error', text: e.message };
            } finally {
                this.syncing = false;
            }
        },
    };
}


// ─── Host Update Check Trigger ────────────────────────────────────────────────

function hostChecker(spaceId, hostId) {
    return {
        checking: false,
        done: 0,
        total: 0,
        message: null,
        _pollTimer: null,

        get progress() {
            return this.total > 0 ? Math.round((this.done / this.total) * 100) : 0;
        },

        get progressLabel() {
            if (!this.checking) return '';
            if (this.total === 0) return 'Syncing...';
            return `${this.done} / ${this.total}`;
        },

        async _pollProgress() {
            try {
                const r = await fetch(`/spaces/${spaceId}/hosts/${hostId}/check-progress`);
                if (r.ok) {
                    const d = await r.json();
                    this.total = d.total || 0;
                    this.done = d.done || 0;
                }
            } catch {}
        },

        async check() {
            this.checking = true;
            this.done = 0;
            this.total = 0;
            this.message = null;
            this._pollTimer = setInterval(() => this._pollProgress(), 400);
            try {
                const resp = await fetch(`/spaces/${spaceId}/hosts/${hostId}/check`, {
                    method: 'POST',
                });
                if (resp.redirected || resp.url.includes('/auth/login')) {
                    window.location.href = '/auth/login';
                    return;
                }
                if (resp.ok) {
                    this.done = this.total;
                    setTimeout(() => location.reload(), 600);
                } else {
                    const data = await resp.json();
                    this.message = { type: 'error', text: data.detail || 'Check failed' };
                }
            } catch (e) {
                this.message = { type: 'error', text: e.message };
            } finally {
                clearInterval(this._pollTimer);
                this._pollTimer = null;
                this.checking = false;
            }
        },
    };
}


// ─── Space-level Check All ────────────────────────────────────────────────────

function spaceChecker(spaceId) {
    return {
        checking: false,
        message: null,

        async checkAll() {
            this.checking = true;
            this.message = null;
            try {
                const resp = await fetch(`/spaces/${spaceId}/hosts/check-all`, {
                    method: 'POST',
                });
                if (resp.redirected || resp.url.includes('/auth/login')) {
                    window.location.href = '/auth/login';
                    return;
                }
                if (resp.ok) {
                    this.message = { type: 'success', text: 'Check completed!' };
                    setTimeout(() => location.reload(), 1500);
                } else {
                    const data = await resp.json();
                    this.message = { type: 'error', text: data.detail || 'Check failed' };
                }
            } catch (e) {
                this.message = { type: 'error', text: e.message };
            } finally {
                this.checking = false;
            }
        },
    };
}


// ─── Confirm Delete ───────────────────────────────────────────────────────────

function confirmDelete(url, redirectUrl) {
    return {
        async doDelete() {
            if (!confirm('Are you sure you want to delete this item? This cannot be undone.')) return;
            try {
                const resp = await fetch(url, { method: 'DELETE' });
                if (resp.redirected || resp.url.includes('/auth/login')) {
                    window.location.href = '/auth/login';
                    return;
                }
                if (resp.ok) {
                    window.location.href = redirectUrl;
                } else {
                    alert('Delete failed. Please try again.');
                }
            } catch (e) {
                alert('Delete failed: ' + e.message);
            }
        },
    };
}


// ─── Per-log Notification Acknowledge ────────────────────────────────────────

function logAcknowledger(containerId, logId, initialStatus, initialChangedAt) {
    return {
        status: initialStatus,
        changedAt: initialChangedAt,
        acking: false,

        async ack() {
            if (this.status === 'ack' || this.acking) return;
            this.acking = true;
            try {
                const resp = await fetch(
                    `/containers/${containerId}/notifications/${logId}/acknowledge`,
                    { method: 'POST' }
                );
                if (resp.ok) {
                    const data = await resp.json();
                    this.status = 'ack';
                    this.changedAt = data.status_changed_at_display || '';
                    this.$dispatch('log-acked', { snoozeUntil: data.snooze_until_display });
                } else {
                    alert('Acknowledge failed. Please try again.');
                }
            } catch (e) {
                alert('Acknowledge failed: ' + e.message);
            } finally {
                this.acking = false;
            }
        },
    };
}


// ─── Test Notification ────────────────────────────────────────────────────────

function testNotification(spaceId, channelId) {
    return {
        testing: false,
        result: null,

        async test() {
            this.testing = true;
            this.result = null;
            try {
                const resp = await fetch(
                    `/spaces/${spaceId}/notifications/${channelId}/test`,
                    { method: 'POST' }
                );
                if (resp.ok) {
                    this.result = { type: 'success', text: 'Test notification sent!' };
                } else {
                    const data = await resp.json();
                    this.result = { type: 'error', text: data.detail || 'Test failed' };
                }
            } catch (e) {
                this.result = { type: 'error', text: e.message };
            } finally {
                this.testing = false;
            }
        },
    };
}
