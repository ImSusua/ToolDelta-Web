(function() {
    var _settings = window.TDSettings || {};
    var currentUser = _settings.currentUser || '';
    var isAdmin = !!_settings.isAdmin;
    var wallpaperUrl = _settings.wallpaperUrl || '';

    function safeWallpaperUrl(url) {
        if (!url || typeof url !== 'string') return '';
        if (!/^https?:\/\//i.test(url)) return '';
        try { url = encodeURI(url); } catch(e) { return ''; }
        url = url.replace(/['"\\]/g, encodeURIComponent);
        return "url('" + url + "')";
    }

    function toggleFbtokenVisibility(btn) {
        var input = document.getElementById('fbtokenInput');
        if (!input) return;
        var isPw = input.type === 'password';
        input.type = isPw ? 'text' : 'password';
        btn.setAttribute('aria-pressed', isPw ? 'true' : 'false');
        btn.textContent = isPw ? '隐藏' : '显示';
        btn.setAttribute('aria-label', isPw ? '隐藏 fbtoken' : '显示 fbtoken');
    }

    function applyWallpaperPreview(url) {
        var el = document.getElementById('wallpaperPreview');
        if (!el) return;
        if (url) {
            var safeUrl = String(url).replace(/['"\\(){};]/g, encodeURIComponent);
            el.style.backgroundImage = "url('" + safeUrl + "')";
        } else {
            el.style.backgroundImage = '';
        }
    }

    function loadMarketSources() {
        fetch('/api/market/sources')
            .then(function(r){return r.json()})
            .then(function(sources){
                var sel = document.getElementById('cfg_market_source');
                if (!sel) return;
                sources.forEach(function(s){
                    var opt = document.createElement('option');
                    opt.value = s.url;
                    opt.textContent = s.name;
                    sel.appendChild(opt);
                });
            });
    }

    function loadLauncherConfig() {
        return fetch('/api/launcher/config').then(function(r){return r.json()}).then(function(d){
            document.getElementById('cfg_github_mirror').value = d['全局GitHub镜像'] || '';
            document.getElementById('cfg_logging').checked = d['是否记录日志'] !== false;
            document.getElementById('cfg_market_source').value = d['插件市场源'] || '';
            var fate = d['FateArk接入点启动模式'] || {};
            document.getElementById('cfg_fate_server').value = fate['服务器号'] || '';
            document.getElementById('cfg_fate_password').value = fate['密码'] || '';
            document.getElementById('cfg_fate_server_addr').value = fate['验证服务器地址(更换时记得更改fbtoken)'] || '';
        }).catch(function(){ showToast('加载失败', 'error'); });
    }

    function saveLauncherConfig() {
        return fetch('/api/launcher/config').then(function(r){return r.json()}).then(function(current){
            current['是否记录日志'] = document.getElementById('cfg_logging').checked;
            current['插件市场源'] = document.getElementById('cfg_market_source').value.trim();
            if (!current['FateArk接入点启动模式']) current['FateArk接入点启动模式'] = {};
            current['FateArk接入点启动模式']['服务器号'] = parseInt(document.getElementById('cfg_fate_server').value) || 0;
            current['FateArk接入点启动模式']['密码'] = document.getElementById('cfg_fate_password').value;
            current['FateArk接入点启动模式']['验证服务器地址(更换时记得更改fbtoken)'] = document.getElementById('cfg_fate_server_addr').value.trim();
            return fetch('/api/launcher/config', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(current) })
            .then(function(r){return r.json()}).then(function(d){
                if(d.success) showToast('配置已保存（重启ToolDelta后生效）', 'success');
                else showToast(d.error||'保存失败', 'error');
            });
        }).catch(function(){ showToast('加载失败', 'error'); });
    }

    function loadFbtoken() {
        fetch('/api/fbtoken').then(function(r){return r.json()}).then(function(d){
            var el = document.getElementById('fbtokenInput');
            if (el) el.value = d.token || '';
        }).catch(function(){});
    }

    function saveFbtoken() {
        var token = document.getElementById('fbtokenInput').value.trim();
        return fetch('/api/fbtoken', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token:token}) })
        .then(function(r){return r.json()}).then(function(d){
            if(d.success) showToast('fbtoken 已保存', 'success');
            else showToast(d.error||'保存失败', 'error');
        });
    }

    function fetchWallpaper() {
        var errEl = document.getElementById('wallpaperError');
        if (errEl) errEl.style.display = 'none';
        return fetch('/api/settings/wallpaper/fetch', { method:'POST' })
        .then(function(r){return r.json()})
        .then(function(d){
            if(d.success && d.data && d.data.url) {
                applyWallpaperPreview(d.data.url);
                if(window.TDWallpaper) TDWallpaper.lock(d.data.url);
                showToast('壁纸已更换', 'success');
            } else {
                if (errEl) {
                    errEl.textContent = d.error || '获取壁纸失败，你可以手动输入图片URL';
                    errEl.style.display = 'block';
                }
                showToast(d.error||'获取壁纸失败', 'error');
            }
        })
        .catch(function(){ showToast('请求失败', 'error'); });
    }

    function setManualWallpaper() {
        var url = document.getElementById('manualWallpaperUrl').value.trim();
        if (!url) { showToast('请输入图片URL', 'error'); return; }
        return fetch('/api/settings/wallpaper/fetch', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:url}) })
        .then(function(r){return r.json()})
        .then(function(d){
            if(d.success && d.data && d.data.url) {
                applyWallpaperPreview(d.data.url);
                if(window.TDWallpaper) TDWallpaper.lock(d.data.url);
                document.getElementById('manualWallpaperUrl').value = '';
                showToast('壁纸已设置', 'success');
            } else {
                var errEl = document.getElementById('wallpaperError');
                if (errEl) {
                    errEl.textContent = d.error || '设置失败';
                    errEl.style.display = 'block';
                }
            }
        })
        .catch(function(){ showToast('请求失败', 'error'); });
    }

    function clearWallpaper() {
        return fetch('/api/settings/wallpaper/clear', { method:'POST' })
        .then(function(r){return r.json()})
        .then(function(d){
            if(d.success) {
                applyWallpaperPreview('');
                if(window.TDWallpaper) TDWallpaper.unlock();
                showToast('壁纸已清除', 'success');
            } else {
                showToast('清除失败', 'error');
            }
        })
        .catch(function(){ showToast('请求失败', 'error'); });
    }

    function changePassword() {
        var oldPwEl = document.getElementById('oldPw');
        var newPwEl = document.getElementById('newPw');
        var newPw2El = document.getElementById('newPw2');
        var oldPw = oldPwEl.value, newPw = newPwEl.value, newPw2 = newPw2El.value;
        var firstErr = null;
        if (!oldPw) { setFieldError(oldPwEl, '请输入当前密码'); firstErr = firstErr || oldPwEl; }
        else setFieldError(oldPwEl, '');
        if (!newPw) { setFieldError(newPwEl, '请输入新密码'); firstErr = firstErr || newPwEl; }
        else if (newPw.length < 6) { setFieldError(newPwEl, '密码至少 6 位'); firstErr = firstErr || newPwEl; }
        else setFieldError(newPwEl, '');
        if (newPw && newPw !== newPw2) { setFieldError(newPw2El, '两次密码不一致'); firstErr = firstErr || newPw2El; }
        else setFieldError(newPw2El, '');
        if (firstErr) { firstErr.focus(); return; }
        return fetch('/api/change-password', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({old_password:oldPw, new_password:newPw}) })
        .then(function(r){return r.json()})
        .then(function(d){
            if(d.success){
                var warn = (d.data||{}).password_warning;
                if (warn && warn.tips && warn.tips.length) {
                    showToast('密码已修改（提示: ' + warn.tips.join('; ') + '）', 'info');
                } else {
                    showToast('密码已修改', 'success');
                }
                oldPwEl.value=''; newPwEl.value=''; newPw2El.value='';
                renderPwStrength('', 'settingsPwBox');
            }
            else {
                setFieldError(oldPwEl, d.error||'修改失败');
                oldPwEl.focus();
            }
        })
        .catch(function(){ showToast('请求失败', 'error'); });
    }

    function confirmReset() {
        showPrompt('请输入当前密码以确认重置面板', '输入当前密码', '', function(pw) {
            if (!pw) return;
            fetch('/api/reset-panel', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:pw}) })
            .then(function(r){return r.json()})
            .then(function(d){
                if(d.success){ showToast('面板已重置', 'success'); setTimeout(function(){ window.location.href='/setup'; }, 1500); }
                else { showToast(d.error||'重置失败', 'error'); }
            })
            .catch(function(){ showToast('请求失败', 'error'); });
        }, 'password');
    }

    function createUser() {
        var uEl = document.getElementById('newUserName');
        var pEl = document.getElementById('newUserPw');
        var rEl = document.getElementById('newUserRole');
        var u = uEl.value.trim();
        var p = pEl.value;
        var r = parseInt(rEl.value, 10);
        if (isNaN(r)) r = 1;
        var firstErr = null;
        if (!u) { setFieldError(uEl, '请输入用户名'); firstErr = firstErr || uEl; }
        else if (u.length < 2) { setFieldError(uEl, '用户名至少 2 个字符'); firstErr = firstErr || uEl; }
        else setFieldError(uEl, '');
        if (!p) { setFieldError(pEl, '请输入密码'); firstErr = firstErr || pEl; }
        else if (p.length < 6) { setFieldError(pEl, '密码至少 6 位'); firstErr = firstErr || pEl; }
        else setFieldError(pEl, '');
        if (firstErr) { firstErr.focus(); return; }
        return fetch('/api/users/create', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:u, password:p, role:r}) })
        .then(function(r){return r.json()})
        .then(function(d){
            if(d.success){
                var warn = (d.data||{}).password_warning;
                if (warn && warn.tips && warn.tips.length) {
                    showToast('用户已创建（密码提示: ' + warn.tips.join('; ') + '）', 'info');
                } else {
                    showToast('用户已创建', 'success');
                }
                uEl.value=''; pEl.value=''; renderPwStrength('', 'createUserPwBox'); loadUsers();
            }
            else {
                setFieldError(uEl, d.error||'创建失败');
                uEl.focus();
            }
        })
        .catch(function(){ showToast('请求失败', 'error'); });
    }

    function deleteUser(username) {
        showConfirm('确定删除用户 "'+username+'" 吗？', function(ok) {
            if (!ok) return;
            fetch('/api/users/delete', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:username}) })
            .then(function(r){return r.json()})
            .then(function(d){
                if(d.success){ showToast('用户已删除', 'success'); loadUsers(); }
                else { showToast(d.error||'删除失败', 'error'); }
            })
            .catch(function(){ showToast('请求失败', 'error'); });
        });
    }

    function loadUsers() {
        if (!isAdmin) return;
        fetch('/api/users').then(function(r){return r.json()}).then(function(d){
            if(!d.success) return;
            var tbody = document.getElementById('userList');
            if (!tbody) return;
            tbody.innerHTML = '';
            d.data.forEach(function(u) {
                var tr = document.createElement('tr');
                var roleLabel = u.role === 10 ? '管理员' : '普通用户';
                var c1 = document.createElement('td'); c1.style.padding='8px 10px'; c1.textContent = u.username || '';
                var c2 = document.createElement('td'); c2.style.padding='8px 10px'; c2.textContent = roleLabel;
                var c3 = document.createElement('td'); c3.style.padding='8px 10px'; c3.style.color='var(--ink-subtle)'; c3.style.fontSize='12px'; c3.textContent = u.login_at || '-';
                tr.appendChild(c1); tr.appendChild(c2); tr.appendChild(c3);
                var td = document.createElement('td');
                td.style.padding = '8px 10px';
                if (u.username !== currentUser) {
                    var del = document.createElement('button');
                    del.className = 'btn btn-sm btn-outline';
                    del.textContent = '删除';
                    del.onclick = function() { deleteUser(u.username); };
                    td.appendChild(del);
                }
                tr.appendChild(td);
                tbody.appendChild(tr);
            });
        }).catch(function(){});
    }

    function bindEvents() {
        document.getElementById('fetchWallpaperBtn').addEventListener('click', function() {
            withGuard(this, fetchWallpaper);
        });
        document.getElementById('clearWallpaperBtn').addEventListener('click', function() {
            withGuard(this, clearWallpaper);
        });
        document.getElementById('setManualWallpaperBtn').addEventListener('click', function() {
            withGuard(this, setManualWallpaper);
        });
        document.getElementById('manualWallpaperUrl').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                var btn = document.getElementById('setManualWallpaperBtn');
                withGuard(btn, setManualWallpaper);
            }
        });
        document.getElementById('changePwBtn').addEventListener('click', function() {
            withGuard(this, changePassword);
        });
        ['oldPw', 'newPw', 'newPw2'].forEach(function(id) {
            var el = document.getElementById(id);
            if (el) el.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    var btn = document.getElementById('changePwBtn');
                    withGuard(btn, changePassword);
                }
            });
        });
        document.getElementById('newPw').addEventListener('input', function() {
            renderPwStrength(this.value, 'settingsPwBox');
        });
        document.getElementById('refreshLauncherBtn').addEventListener('click', function() {
            withGuard(this, loadLauncherConfig);
        });
        document.getElementById('saveLauncherBtn').addEventListener('click', function() {
            withGuard(this, saveLauncherConfig);
        });
        document.getElementById('saveFbtokenBtn').addEventListener('click', function() {
            withGuard(this, saveFbtoken);
        });
        document.getElementById('fbtokenInput').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                var btn = document.getElementById('saveFbtokenBtn');
                withGuard(btn, saveFbtoken);
            }
        });
        var fbtokenToggle = document.getElementById('fbtokenToggleBtn');
        if (fbtokenToggle) {
            fbtokenToggle.addEventListener('click', function() {
                toggleFbtokenVisibility(this);
            });
        }
        document.getElementById('resetPanelBtn').addEventListener('click', confirmReset);
        if (isAdmin) {
            document.getElementById('createUserBtn').addEventListener('click', function() {
                withGuard(this, createUser);
            });
            document.getElementById('newUserPw').addEventListener('input', function() {
                renderPwStrength(this.value, 'createUserPwBox');
            });
            ['newUserName', 'newUserPw'].forEach(function(id) {
                var el = document.getElementById(id);
                if (el) el.addEventListener('keydown', function(e) {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        var btn = document.getElementById('createUserBtn');
                        withGuard(btn, createUser);
                    }
                });
            });
        }
        var newPwEl = document.getElementById('newPw');
        var newPw2El = document.getElementById('newPw2');
        if (newPw2El) {
            newPw2El.addEventListener('blur', function() {
                var n = newPwEl ? newPwEl.value : '';
                if (this.value && n && this.value !== n) {
                    setFieldError(this, '两次密码不一致');
                } else {
                    setFieldError(this, '');
                }
            });
            newPw2El.addEventListener('input', function() {
                if (this.getAttribute('aria-invalid') === 'true') setFieldError(this, '');
            });
        }
        if (newPwEl) {
            newPwEl.addEventListener('input', function() {
                if (newPw2El && newPw2El.value && this.value !== newPw2El.value) {
                    setFieldError(newPw2El, '两次密码不一致');
                } else if (newPw2El) {
                    setFieldError(newPw2El, '');
                }
            });
        }
    }

    function loadAboutInfo() {
        fetch('/api/system/info').then(function(r){return r.json()}).then(function(d){
            var el = function(id){return document.getElementById(id);};
            if(el('aboutPython')) el('aboutPython').textContent = d.python_version || '-';
            if(el('aboutPlatform')) el('aboutPlatform').textContent = d.platform || '-';
            if(el('aboutTdDir')) el('aboutTdDir').textContent = d.tooldelta_dir || '-';
            if(el('aboutPlugins')) el('aboutPlugins').textContent = (d.plugin_count||0) + ' (启用: '+(d.enabled_plugins||0)+')';
        }).catch(function(){});
    }

    document.addEventListener('DOMContentLoaded', function() {
        applyWallpaperPreview(wallpaperUrl);
        bindEvents();
        loadMarketSources();
        loadLauncherConfig();
        loadFbtoken();
        loadAboutInfo();
        if (isAdmin) loadUsers();
    });

    window.TDSettings = {
        toggleFbtokenVisibility: toggleFbtokenVisibility
    };
})();
