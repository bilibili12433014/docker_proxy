# Docker Proxy

Docker Proxy 是一个基于 YAML 配置的轻量 Docker 用户管理与 SSH 网关。管理界面监听 `2221` 端口，SSH 网关监听 `2222` 端口；两项服务完全分离，可以分别通过普通 HTTP 和 Cloudflare Tunnel 暴露。

项目不使用数据库。首次启动时无需准备配置文件，网页初始化完成后会在项目根目录生成 `config.yaml`；SSH 主机密钥和用户密钥保存在 `./key_dir`。这两个位置均已加入 `.gitignore`。

## 功能

- 通过网页完成首次初始化、管理员登录和 Docker 用户管理
- 为每个用户生成独立的 Ed25519 SSH 密钥
- 使用唯一用户名创建、启动、停止、启用或停用用户容器
- 默认镜像为 `ubuntu:26.04`，并自动记住最近一次使用的镜像
- 每个容器实例的可写层限制为 20GB，不包含单独挂载的 `/data`
- 将宿主机 `/data/<username>` 固定映射到容器内的 `/data`
- SSH 公钥认证成功后，通过 Docker Exec 以 root 身份进入用户容器，无需在镜像内运行 `sshd`
- 交互登录自动进入持久化的 `workspace` tmux 会话
- tmux 启用鼠标操作、彩色路径提示符和 100000 行历史记录
- 缺少 tmux 时自动安装；Ubuntu 和 Debian 普通 APT 仓库使用清华 IPv4 镜像
- 自动识别 NVIDIA 显卡型号和数量，使用从 `0` 开始的编号分配显卡
- 已创建的实例可在管理页面修改 GPU 分配，重建时保留 SSH 密钥、运行状态和 `/data`
- 提供无需下载密钥的 PowerShell 临时密钥登录命令
- 提供无需下载密钥的 PowerShell SCP 命令，将 `FILE_NAME` 上传到实例的 `/data/`
- 创建用户遇到同名遗留容器时，网页会要求确认后再重置
- 只要同名受管容器存在，无论 YAML 中是否已有该用户，都可以保留容器并只重置 SSH 密钥

## 运行要求

- Linux Docker 主机
- 可访问的 Docker Socket：`/var/run/docker.sock`
- 可写的宿主机目录：`/data`
- TCP `2221` 和 `2222` 端口
- 使用本地方式运行时需要 Python 3

20GB 实例限制通过 Docker 的 `storage-opt size` 实现。使用 `overlay2` 时，Docker 数据目录的底层文件系统必须是启用了 `pquota` 的 XFS；不满足该条件时 Docker 会拒绝创建带容量限制的容器。

用户镜像至少需要包含 `/bin/sh`。若镜像包含 `/bin/bash`，登录后会使用带颜色和当前路径的 Bash 提示符。

GPU 功能要求宿主机已经安装 NVIDIA 驱动和 NVIDIA Container Toolkit，并在执行 `nvidia-ctk runtime configure --runtime=docker` 后重启 Docker。管理页会显示 Docker 存储驱动、底层文件系统、默认 runtime、NVIDIA runtime 注册状态以及识别到的显卡数量；显卡枚举会优先调用本机 `nvidia-smi`，不可用时通过临时 Docker 容器检测，失败时直接显示 Docker 或 NVIDIA runtime 返回的原因。

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

创建用户时填写唯一用户名和 Docker 镜像，并使用从 `0` 开始的 GPU 编号选择实例允许使用的显卡。管理页默认勾选检测到的全部显卡，也允许取消全部显卡或只选择其中一部分。服务会依次完成以下操作：

1. 检查本地镜像，不存在时通过 Docker 拉取。
2. 创建 `/data/<username>`。
3. 生成用户 Ed25519 密钥。
4. 使用所选 GPU 和 20GB 可写层限制创建名为 `docker-proxy-<username>` 的容器，并挂载数据目录。
5. 将用户、镜像、容器 ID、公钥、GPU 编号和状态写入 `config.yaml`。

容器可写层上限为 `20G`，宿主机绑定挂载的 `/data/<username>` 不计入该限制。如果密钥目录或同名受管容器由此前失败的创建操作遗留，管理界面会提示确认重置。停用用户会同时阻止 SSH 登录并停止容器；重新启用后仍需按需启动容器。

容量限制仅能在创建容器时设置。升级前已经存在的容器不会自动重建，也不会自动获得 20GB 限制；需要保留 `/data/<username>` 后重新创建对应实例。

只要同名受管容器已经存在，再次提交该用户名时就会进入确认页面，不要求 YAML 中已有用户记录。选择复用后不会删除或重建容器，只会生成新的 SSH 密钥、创建或修复用户记录、重新启用用户并立即使旧私钥失效；镜像、可写层容量和 GPU 分配保持不变。

管理页面可以修改已创建实例的 GPU 分配。由于 Docker 不支持原地更新容器的 GPU 设备请求，保存时会使用相同镜像和 20GB 限制重建容器，保留 SSH 密钥、原先的启动或停止状态以及 `/data/<username>`；容器可写层中的其他内容会被清除。

## SSH 登录

直接连接 SSH 网关：

```bash
ssh -p 2222 -i id_ed25519_<username> <username>@<服务器地址>
```

管理页面提供内嵌 Ed25519 私钥的 PowerShell 登录命令和 SCP 上传命令，不再提供一键登录包。两条命令都会自动安装缺失的 `cloudflared`，在 `%TEMP%` 中创建随机临时密钥、收紧 ACL，并在命令结束后删除密钥。

SCP 命令中的 `"FILE_NAME"` 是待上传文件路径，目标固定为实例内的 `/data/`。命令使用 `scp -O` 兼容 SSH 网关的命令转发方式；如果镜像中没有 `scp`，网关会在第一次传输前通过镜像的包管理器静默安装。命令本身包含完整私钥，不应发送给其他人，也不应保存在共享终端历史中。

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
- 容器可写层容量 `container_storage_size: 20G`
- 容器名称前缀和保活命令
- 最近一次使用的 Docker 镜像
- 用户、镜像、容器 ID、公钥、GPU 编号、启用状态和创建时间

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
