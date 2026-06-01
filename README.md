# 硅谷AI晨报 PWA — 完整渐进式 Web 应用

## 📁 文件清单

```
05-PWA/
├── index.html              # 主页面（基于v2.0原型 + PWA增强）
├── manifest.json           # PWA 清单文件
├── sw.js                   # Service Worker（离线缓存 + 推送处理）
├── push-notification.js    # 推送通知前端逻辑
├── server.py               # 后端服务器（端口8899）
├── README.md               # 本文件
└── icons/
    ├── gen_icons.py         # 图标生成脚本
    ├── icon-192.png         # 192×192（安卓桌面）
    ├── icon-512.png         # 512×512（PWA通用）
    ├── apple-touch-icon.png # 180×180（iOS桌面）
    └── favicon-32.png       # 32×32（浏览器标签页）
```

## ✨ PWA 功能概述

### 1. 可安装到桌面
- **iOS**: Safari 中点击分享 → 添加到主屏幕，使用 `apple-mobile-web-app` meta 标签实现全屏体验
- **安卓**: Chrome 自动弹出安装提示，支持 `beforeinstallprompt` 事件
- **鸿蒙**: 通过浏览器 "添加到桌面" 功能支持
- 安装后以 `standalone` 模式运行，无浏览器工具栏

### 2. 离线使用
- Service Worker 预缓存核心资源（index.html, manifest, icons）
- **Network First** 策略：联网时获取最新内容，断网时回退缓存
- 离线时顶部显示非侵入式提示条「📡 当前离线，已缓存的内容仍可阅读」
- 未缓存的 API 请求返回友好错误 JSON

### 3. 推送通知
- 支持 Web Push API（需 HTTPS 或 localhost + VAPID）
- 前端 `push-notification.js` 处理权限请求、订阅、取消订阅
- Service Worker 处理推送事件和通知点击
- 通知分组、振动反馈
- 设置页面可开关不同推送类型

### 4. 安装横幅
- 底部固定横幅：「📱 添加到桌面，体验更好」
- iOS 检测：引导用户通过 Safari 分享菜单添加
- 安卓检测：响应 `beforeinstallprompt` 事件
- 关闭后 3 天再次提示（localStorage 记录）
- 已安装（`display-mode: standalone`）时自动隐藏

### 5. 更新检测
- Service Worker 每小时检测更新
- 发现新版本时顶部显示「🔄 有新版本可用，点击更新」
- 点击后跳过等待并激活新 SW，自动刷新页面

### 6. iOS 专属优化
- `apple-mobile-web-app-capable`：全屏运行
- `apple-mobile-web-app-status-bar-style`：状态栏样式
- `apple-touch-icon`：桌面图标
- `viewport-fit=cover`：全面屏适配
- `safe-area-inset-*` CSS 变量适配刘海屏

### 7. 其他
- `viewport` 适配移动端
- `user-scalable=no` 防止双击缩放
- 响应式布局（桌面端显示 iPhone 模拟框，移动端全屏）

## 🚀 如何部署

### 方式一：直接用 Python 服务器
```bash
cd 05-PWA
python3 server.py
# 访问 http://localhost:8899
```

### 方式二：用 Node.js（推荐生产环境）
```bash
npx serve 05-PWA -l 8899 --ssl
```

### 方式三：部署到 Nginx
```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;
    root /path/to/05-PWA;

    # PWA 文件正确 Content-Type
    location /sw.js {
        add_header Content-Type application/javascript;
        add_header Service-Worker-Allowed /;
    }
    location /manifest.json {
        add_header Content-Type application/json;
    }

    # SPA fallback
    location / {
        try_files $uri $uri/ /index.html;
    }

    # VAPID API
    location /api/ {
        proxy_pass http://127.0.0.1:8899;
    }
}
```

### ⚠️ 重要提示
- **HTTPS 必需**：Service Worker 和 Push API 需要 HTTPS（localhost 例外）
- **VAPID 密钥**：生产环境需生成真实的 VAPID 密钥对，替换 `server.py` 中的演示值
- **推送后端**：`server.py` 仅提供订阅存储端点，实际推送发送需要额外的后端服务（如 `web-push` 库）

## 🧪 如何测试 PWA

### 1. Chrome DevTools 验证
```bash
# 启动服务器
python3 server.py

# 打开 Chrome，访问 http://localhost:8899
# F12 → Application 标签页
```

#### Manifest 验证
- **Application → Manifest**
- 检查：名称、图标、主题色、display 模式
- ✅ 应显示 "可安装" 提示

#### Service Worker 验证
- **Application → Service Workers**
- 检查：状态为 "activated and is running"
- 点击 "Offline" 模拟离线
- 刷新页面，应能正常显示缓存内容
- 顶部应出现离线提示条

#### 推送测试
- 点击页面上的 🔔 按钮测试推送通知
- 检查通知权限是否正确请求

### 2. Lighthouse PWA 审计
```bash
# Chrome → F12 → Lighthouse 标签页
# 勾选 "Progressive Web App" → Generate report
# 目标: PWA 评分 ≥ 90
```

### 3. 离线测试
```bash
# 1. 正常访问页面，确保 SW 已注册和缓存
# 2. Chrome DevTools → Network → 勾选 "Offline"
# 3. 刷新页面
# 4. ✅ 应显示缓存内容 + 离线提示条
# 5. 文章详情中未缓存的 API 显示 "需要网络连接"
```

### 4. 安装测试
```bash
# 桌面 Chrome：
# 地址栏右侧应出现安装图标 ⊕
# 点击安装 → 桌面出现独立窗口

# Android Chrome：
# 底部应出现安装横幅
# 或通过菜单 → "添加到主屏幕"

# iOS Safari：
# 底部横幅显示 "如何添加"
# 点击后提示：分享 → 添加到主屏幕
```

### 5. 真机测试
```bash
# 1. 确保手机和电脑在同一局域网
# 2. 获取电脑 IP：ipconfig（Windows）/ ifconfig（Mac/Linux）
# 3. 手机浏览器访问 http://<电脑IP>:8899
# 4. 测试安装、推送通知、离线功能
```

### 6. `about://inspect` (Android USB 调试)
```bash
# 1. USB 连接安卓手机，开启开发者模式 + USB 调试
# 2. Chrome 地址栏输入 chrome://inspect
# 3. 手机上打开 PWA 页面
# 4. 在桌面 Chrome 中远程调试 SW 和 Manifest
```

## 📊 技术栈

| 层级 | 技术 |
|------|------|
| 前端框架 | Vanilla JS（无需框架） |
| PWA Manifest | W3C Web App Manifest |
| 离线缓存 | Service Worker (Network First) |
| 推送通知 | Web Push API + VAPID |
| iOS 适配 | Apple Meta Tags + Safe Area CSS |
| 后端 | Python http.server |
| 图标 | Pillow (Python) |

## 🔧 自定义

### 修改应用名称
- `manifest.json` 中的 `name` 和 `short_name`
- `index.html` 中的 `<title>` 和 `<meta name="apple-mobile-web-app-title">`

### 修改主题色
- `manifest.json` 中的 `theme_color` 和 `background_color`
- `index.html` 中的 `<meta name="theme-color">`

### 添加截图（PWA 安装对话框用）
```json
// 在 manifest.json 中添加:
"screenshots": [
  {
    "src": "screenshots/home.png",
    "sizes": "1170x2532",
    "type": "image/png",
    "form_factor": "narrow"
  }
]
```

### 生产环境 VAPID 密钥
```bash
# 安装 web-push
npm install web-push -g

# 生成 VAPID 密钥
web-push generate-vapid-keys

# 输出:
# Public Key:  <复制到 server.py 的 VAPID_PUBLIC_KEY>
# Private Key: <用于后端推送发送>
```
