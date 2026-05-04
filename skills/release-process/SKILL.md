---
name: release-process
description: "MakeCode 软件发布流程技能。当用户需要发布新版本、修改版本号、打包构建、上传更新文件、或了解发布规范时触发。包含版本变更规则、构建步骤、发布流程和更新机制说明。适用场景：发布版本、版本号管理、构建打包、更新部署、发布问题排查。"
---

# MakeCode 发布流程

本技能指导完成 MakeCode 软件的完整发布流程，包括版本管理、构建打包、发布部署和自动更新机制。

---

## 1. 版本变更规则

MakeCode 使用**语义化版本号**（Semantic Versioning），格式为 `MAJOR.MINOR.PATCH`：

### 版本号定义

| 版本类型 | 变更时机 | 示例 |
|---------|---------|------|
| **MAJOR**（主版本） | 不兼容的 API 变更、架构重大重构、数据格式不兼容变更 | 2.x.x → 3.0.0 |
| **MINOR**（次版本） | 新增功能、新增工具、新增能力（向后兼容） | 3.0.x → 3.1.0 |
| **PATCH**（补丁版本） | Bug 修复、性能优化、文档更新（向后兼容） | 3.0.0 → 3.0.1 |

### 版本号修改位置

版本号统一在 `version.py` 文件中管理：

```python
# version.py
CURRENT_VERSION = "3.0.1"  # ← 修改此处

UPDATE_SERVER_URL = "https://starvpn.forwardforever.top"
VERSION_CHECK_URL = f"{UPDATE_SERVER_URL}/version.json"
DOWNLOAD_URL = f"{UPDATE_SERVER_URL}/MakeCode.exe"
```

### 版本变更检查清单

在修改版本号前，确认以下事项：

- [ ] **MAJOR 变更**：检查是否有不兼容的 API 变更、数据库/配置格式迁移需求
- [ ] **MINOR 变更**：确认新功能已完整实现并通过测试
- [ ] **PATCH 变更**：确认 Bug 已修复且不影响现有功能
- [ ] 更新 `version.py` 中的 `CURRENT_VERSION`
- [ ] 检查是否需要更新 `UPDATE_SERVER_URL`（服务器地址变更时）

---

## 2. 构建打包流程

### 2.1 版本号检查与提交

**这是整个发布流程的第一步。** 在做任何其他操作之前，先获取远程版本以判断是否需要版本变更：

1. 请求 `https://starvpn.forwardforever.top/version.json` 获取远程当前已发布版本（这是首要步骤，决定后续所有操作）
2. 对比本地 `version.py` 中的 `CURRENT_VERSION`
3. 如果版本号相同 → 询问用户新版本号并更新 `version.py`
4. 如果版本号已递增 → 直接进入下一步
5. 运行 `git status` 检查所有待提交的变更（包括 `version.py` 和其他代码变更）
6. **先提交所有变更，再开始构建** — 确保构建产物基于已提交的代码
7. 版本号确认且提交完成后再开始构建

### 2.2 前置准备

确保以下工具已安装：
- Python 3.8+
- PyInstaller
- 项目依赖（`pip install -r requirements.txt`）

### 2.3 构建 updater.exe

updater 是独立的更新器程序，需要先构建：

```bash
pyinstaller updater.spec
```

构建产物：`dist/updater.exe`

### 2.4 构建主程序 MakeCode.exe

```bash
pyinstaller MakeCode.spec
```

构建产物：`dist/MakeCode.exe`

**注意**：`MakeCode.spec` 会自动将 `dist/updater.exe` 打包到主程序中，因此必须先构建 updater。

### 2.5 构建顺序

```
1. pyinstaller updater.spec    → 生成 dist/updater.exe
2. pyinstaller MakeCode.spec   → 生成 dist/MakeCode.exe（内含 updater）
```

---

## 3. 发布流程

### 3.1 生成版本信息文件

运行发布脚本生成 `version.json`（`--release_log` 为必需参数，传入发布日志文件路径）：

创建发布日志文件（如 `RELEASE_LOG.md`），写入 markdown 格式的发布内容，然后：

```bash
python release.py --release_log RELEASE_LOG.md
```

该脚本会：
1. 检查 `dist/MakeCode.exe` 是否存在
2. 计算 exe 文件的 SHA256 哈希值
3. 生成 `dist/version.json`，内容包含：
   - `version`：当前版本号
   - `download_url`：下载地址
   - `sha256`：文件校验值
   - `release_log`：发布日志（markdown 格式，用于 GitHub Release 和客户端更新通知展示）

### 3.2 上传文件到服务器（可并行）

FTP 上传和 GitHub Release 上传相互独立，**可以同时执行**以加快发布速度。

使用 FTP 上传脚本将构建产物上传到更新服务器：

```bash
python ftp_release.py
```

该脚本会将以下文件上传到 FTP 服务器：

| 本地文件 | 服务器路径 | 用途 |
|---------|-----------|------|
| `dist/MakeCode.exe` | MakeCode.exe | 主程序下载 |
| `dist/version.json` | version.json | 版本检查 |

**FTP 配置**（存储在 `.ftp_config` 文件中）：
```json
{
    "host": "120.79.196.147",
    "port": 21,
    "user": "panel_ssl_site",
    "pass": "******"
}
```

**注意事项**：
- `.ftp_config` 包含 FTP 凭据，已加入 `.gitignore`，不会提交到远程仓库
- 脚本使用 `NatFTP` 类修复 NAT 环境下 PASV 返回内网 IP 的问题
- 服务器需开放被动端口范围（39000-40000），否则数据通道会超时

### 3.3 上传到 GitHub Release（可与 FTP 并行）

使用 GitHub Release 脚本将构建产物发布到 GitHub，可与 FTP 上传同时执行：

```bash
python github_release.py
```

该脚本会：
1. 删除仓库中所有现有 Releases 和对应 tags
2. 创建新的 Release（tag 为 `v{版本号}`），body 包含版本和 commit 信息（markdown 格式）
3. 上传 `MakeCode.exe` 和 `version.json`

**GitHub 配置**：
- 仓库：`upupmake/MakeCode`
- Token：存储在 `.github_token` 文件中（已加入 `.gitignore`）
- Token 需要 `repo` 权限

**注意事项**：
- 每次发布会清除所有历史 Release，只保留最新版本
- Token 权限不足会导致 404 错误，需确保勾选 `repo` 权限

### 3.4 发布检查清单

- [ ] 版本号已确认（`version.py`）
- [ ] **所有变更已提交**（`version.py` + 代码变更）— 构建前完成
- [ ] updater.exe 已构建
- [ ] MakeCode.exe 已构建
- [ ] `python release.py` 已执行成功
- [ ] `dist/version.json` 已生成且内容正确
- [ ] FTP 上传完成
- [ ] GitHub Release 上传完成
- [ ] 验证服务器版本检查接口返回正确
- [ ] **确认工作区干净**：运行 `git status` 确认无未提交的文件

---

## 4. 自动更新机制

### 4.1 更新检查流程

用户端启动时会：
1. 请求 `{UPDATE_SERVER_URL}/version.json` 获取最新版本信息
2. 比较本地版本与服务器版本
3. 如果有新版本，提示用户下载

### 4.2 更新执行流程

updater.exe 负责执行更新：

```
1. 接收参数：--exe-path, --new-file, --pid, --launch-args
2. 等待主程序退出（超时 30 秒）
3. 备份旧版本为 .old 文件
4. 用新版本替换主程序
5. 清理备份文件和临时目录
6. 退出更新器
```

### 4.3 更新失败恢复

- 如果替换失败，updater 会尝试恢复备份
- 如果恢复也失败，会提示主程序可能损坏
- 用户可手动从 `.old` 备份文件恢复

---

## 5. 常见问题排查

### Q1: 构建失败 "找不到 updater.exe"

确保先运行 `pyinstaller updater.spec`，再运行 `pyinstaller MakeCode.spec`。

### Q2: version.json 生成失败

检查 `dist/MakeCode.exe` 是否存在，确保构建步骤已完成。

### Q3: 用户无法更新

检查：
- 服务器上的 `version.json` 是否可访问
- `MakeCode.exe` 下载链接是否正确
- SHA256 是否匹配

### Q4: 版本号格式错误

确保使用 `MAJOR.MINOR.PATCH` 格式，如 `3.0.1`，不要添加前缀 `v`。

### Q5: FTP 上传数据通道超时

FTP 使用两个通道：控制通道（端口 21）和数据通道。被动模式下数据通道端口由服务器动态分配。

解决方法：
1. 确保服务器已开放被动端口范围（39000-40000）
2. 如果服务器在 NAT 后面，`ftp_release.py` 中的 `NatFTP` 类会自动用公网 IP 替换 PASV 返回的内网 IP

---

## 6. 快速发布命令参考

```bash
# 完整发布流程
# 1. 获取远程已发布版本，判断是否需要版本变更（首要步骤）
curl -s https://starvpn.forwardforever.top/version.json
# 2. 对比本地 version.py，必要时更新版本号并提交所有变更
git add -A && git commit -m "release: vX.Y.Z"
# 3. 构建打包
pyinstaller updater.spec
pyinstaller MakeCode.spec
# 4. 创建发布日志文件 RELEASE_LOG.md，写入 markdown 格式的发布内容
# 5. 生成版本信息（--release_log 传入发布日志文件路径）
python release.py --release_log RELEASE_LOG.md
# 6. 上传到服务器（FTP 和 GitHub 可同时执行）
python ftp_release.py &
python github_release.py &
wait
# 7. 确认工作区干净
git status  # 应输出 "nothing to commit, working tree clean"
```

---

## 7. 相关文件说明

| 文件 | 用途 |
|------|------|
| `version.py` | 版本号和服务器地址配置 |
| `release.py` | 发布脚本，生成 version.json |
| `MakeCode.spec` | 主程序 PyInstaller 打包配置 |
| `updater.spec` | 更新器 PyInstaller 打包配置 |
| `updater.py` | 更新器源码 |
| `ftp_release.py` | FTP 上传脚本（配置存储在 `.ftp_config`） |
| `github_release.py` | GitHub Release 上传脚本（配置存储在 `.github_token`） |
| `.ftp_config` | FTP 服务器配置（不提交远程） |
| `.github_token` | GitHub Token（不提交远程） |
