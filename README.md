# Docker Proxy

Docker Proxy 是一个基于 YAML 配置的轻量 Docker 用户管理与 SSH 网关。管理界面监听 `2221` 端口，SSH 网关监听 `2222` 端口；两项服务完全分离，可以分别通过普通 HTTP 和 Cloudflare Tunnel 暴露。

项目不使用数据库。首次启动时无需准备配置文件，网页初始化完成后会在项目根目录生成 `config.yaml`；SSH 主机密钥和用户密钥保存在 `./key_dir`。这两个位置均已加入 `.gitignore`。

## 功能

- 通过网页完成首次初始化、管理员登录和 Docker 用户管理
- 为每个用户生成独立的 Ed25519 SSH 密钥
- 使用唯一用户名创建、启动、停止、启用或停用用户容器
- 默认镜像为 `ubuntu:26.04`，并自动记住最近一次使用的镜像
- 将宿主机 `/data/<username>` 固定映射到容器内的 `/data`
- SSH 公钥认证成功后，通过 Docker Exec 以 root 身份进入用户容器，无需在镜像内运行 `sshd`
- 交互登录自动进入持久化的 `workspace` tmux 会话
- tmux 启用鼠标操作、彩色路径提示符和 100000 行历史记录
- 缺少 tmux 时自动安装；Ubuntu 和 Debian 普通 APT 仓库使用清华 IPv4 镜像
- 提供 PowerShell、BAT 登录命令以及包含私钥和登录脚本的 ZIP 下载
- 创建用户遇到同名遗留容器时，网页会要求确认后再重置

## 运行要求

- Linux Docker 主机
- 可访问的 Docker Socket：`/var/run/docker.sock`
- 可写的宿主机目录：`/data`
- TCP `2221` 和 `2222` 端口
- 使用本地方式运行时需要 Python 3

用户镜像至少需要包含 `/bin/sh`。若镜像包含 `/bin/bash`，登录后会使用带颜色和当前路径的 Bash 提示符。

## Docker 启动

在项目根目录执行：

```bash
docker compose -f app/compose.yaml up -d --build
```

查看运行日志：

```bash
docker compose -f app/compose.yaml logs -f docker-proxy
```

更新代码后重新构建并启动：

```bash
docker compose -f app/compose.yaml up -d --build
```

## 本地启动

安装当前软件源提供的最新版依赖：

```bash
python3 -m pip install --upgrade asyncssh docker Flask PyYAML
python3 start.py
```

系统包安装方式和 Docker 国内镜像配置记录在 `install.txt`。需要详细日志时可以使用：

```bash
LOG_LEVEL=DEBUG python3 start.py
```

## 首次初始化

启动后访问：

```text
http://服务器地址:2221/
```

首次页面需要填写：

- 管理密码：允许英文字母、数字以及常用 ASCII 符号，不限制长度
- SSH 公网域名：用于生成 Cloudflare SSH 登录命令

提交后会生成根目录下的 `config.yaml` 并自动登录管理界面。端口、监听地址等启动参数在服务启动时读取，手动修改后需要重启服务。

## 用户与容器

创建用户时填写唯一用户名和 Docker 镜像。服务会依次完成以下操作：

1. 检查本地镜像，不存在时通过 Docker 拉取。
2. 创建 `/data/<username>`。
3. 生成用户 Ed25519 密钥。
4. 创建名为 `docker-proxy-<username>` 的容器并挂载数据目录。
5. 将用户、镜像、容器 ID、公钥和状态写入 `config.yaml`。

如果密钥目录或同名受管容器由此前失败的创建操作遗留，管理界面会提示确认重置。停用用户会同时阻止 SSH 登录并停止容器；重新启用后仍需按需启动容器。

## SSH 登录

直接连接 SSH 网关：

```bash
ssh -p 2222 -i id_ed25519_<username> <username>@<服务器地址>
```

管理页面也可以复制 Cloudflare 登录命令，或下载一键登录包。Windows 登录脚本会在缺少 `cloudflared` 时执行：

```powershell
winget install Cloudflare.cloudflared
```

随后使用以下形式连接：

```powershell
ssh -i ".\id_ed25519_<username>" -o IdentitiesOnly=yes -o "ProxyCommand=cloudflared access ssh --hostname %h" <username>@<SSH公网域名>
```

登录脚本会收紧 Windows 私钥文件权限，避免 OpenSSH 因私钥可被其他用户读取而拒绝加载。

## Cloudflare Tunnel

网页和 SSH 必须使用不同的 hostname，并分别指向不同端口：

```yaml
ingress:
  - hostname: admin.example.com
    service: http://localhost:2221
  - hostname: ssh.example.com
    service: ssh://localhost:2222
  - service: http_status:404
```

不要把管理网页 hostname 指向 `tcp://localhost:2222` 或 `ssh://localhost:2222`。`2222` 只处理 SSH，网页只由 `2221` 提供。

## 配置与数据

首次初始化生成的 `config.yaml` 保存：

- 管理密码和 Flask 会话密钥
- SSH 公网域名
- 管理端口 `2221` 和 SSH 端口 `2222`
- 固定的 `key_dir: ./key_dir`
- 容器名称前缀和保活命令
- 最近一次使用的 Docker 镜像
- 用户、镜像、容器 ID、公钥、启用状态和创建时间

用户数据目录固定为宿主机 `/data/<username>`，不从 YAML 读取，避免 Windows 路径或项目路径被错误传递给 Linux Docker。用户配置在管理操作和 SSH 认证时重新读取。

## 目录结构

```text
docker_proxy/
├── start.py
├── README.md
├── install.txt
├── app/
│   ├── Dockerfile
│   ├── Dockerfile.dockerignore
│   ├── compose.yaml
│   ├── templates/
│   ├── static/
│   └── 应用源码
├── config.yaml
└── key_dir/
```

`config.yaml` 和 `key_dir/` 仅在运行时生成，不应提交到 Git，也不会读取或写入项目父目录。

## 安全说明

挂载 Docker Socket 等同于向本服务授予 Docker 主机上的高权限。管理界面不应直接暴露给不受信任的网络，应使用 HTTPS、Cloudflare Access 或等效访问控制。

`config.yaml` 包含管理密码，`key_dir/` 包含未设置口令的 SSH 私钥。部署、备份和迁移时必须将两者作为敏感数据处理。
