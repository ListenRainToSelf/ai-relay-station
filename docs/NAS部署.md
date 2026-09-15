# 把中转站跑在 NAS 上

本文只讲 NAS 特有的部分：怎么装、怎么让它常驻、怎么安全地暴露给局域网，
以及群晖 / 威联通这两家的具体操作差异。通用功能见 [README](../README.md)。

---

## 一、先想清楚三件事

1. **谁访问它**
   - 只有 NAS 本机的应用用 → 监听 `127.0.0.1`（默认），最安全。
   - 局域网里的电脑 / 手机用 → 监听 `0.0.0.0`，此时**管理面必须靠管理员令牌保护**（默认就是强制的）。
2. **数据放哪** → 一定放在 NAS 的存储卷上（别放在容器可写层或 `/tmp`），否则升级/重建容器会丢密钥与统计。
3. **怎么常驻** → 优先 Docker（群晖/威联通都支持）；有 systemd 的发行版也可以直接跑 systemd。

---

## 二、推荐方式：Docker

```bash
# 1) 准备目录（示例：群晖 /volume1/docker/airelay，威联通 /share/Container/airelay）
mkdir -p /volume1/docker/airelay/data

# 2) 放入 docker-compose.yml，把卷映射改成上面的路径
#    volumes:
#      - /volume1/docker/airelay/data:/data

docker compose up -d
docker compose exec airelay python -m airelay --print-token
```

然后浏览器打开 `http://<NAS地址>:8000/admin`，粘贴令牌登录。

**群晖 DSM 7.x（Container Manager）**
- 「项目」→ 新增 → 来源选「创建 docker-compose.yml」→ 粘贴本仓库的 `docker-compose.yml`。
- 把卷映射改成 `/volume1/docker/airelay/data:/data`，端口按需改（例如宿主机 8080 → 容器 8000）。
- 项目启动后，「容器」列表里能看到 `airelay`，日志直接在界面里看。

**威联通 QTS（Container Station）**
- 「应用程序」→ 创建 → 粘贴 compose 内容，卷映射改成 `/share/Container/airelay/data:/data`。
- 端口冲突时改宿主机侧端口即可（容器内固定 8000）。

**资源建议**：给 256–512 MB 内存上限足够；这是 IO 极轻的转发服务，瓶颈在上游网络而不在 NAS。

---

## 三、备选方式：systemd / 直接后台跑

有 systemd 的 Linux NAS（或自己装的 Debian/Ubuntu 主机）：

```bash
sudo ./scripts/install-linux.sh --host 0.0.0.0 --port 8000 --data-dir /volume1/airelay
systemctl status airelay
journalctl -u airelay -f
```

没有 systemd（老版 DSM/QTS、或精简发行版）就手动后台跑，注意把数据目录放到存储卷上：

```bash
cd /volume1/airelay-src
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
AIRELAY_DATA_DIR=/volume1/airelay nohup ./venv/bin/python -m airelay --mode server --host 0.0.0.0 --port 8000 >> /volume1/airelay/run.log 2>&1 &
```

> 这类环境没有自动重启，建议在 NAS 的「开机任务 / Triggered Task」里加一条开机执行上面的命令。

---

## 四、对外暴露与反向代理

**不要**直接把 8000 端口暴露到公网。局域网自用就够了；
确实需要从外网访问时，用 NAS 自带的反代加 HTTPS，并且**只反代 `/v1/*`**（协议面），管理面留在内网。

Nginx 反代示例（局域网内提供 HTTPS）：

```nginx
server {
    listen 443 ssl;
    server_name ai.lan;
    ssl_certificate     /etc/nginx/certs/ai.lan.crt;
    ssl_certificate_key /etc/nginx/certs/ai.lan.key;

    location /v1/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # 流式转发的关键：关掉缓冲，否则 SSE 会攒着一大块才吐出来
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header Connection "";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # 控制台仍然只在内网开放
    location /admin { allow 192.168.0.0/16; deny all; proxy_pass http://127.0.0.1:8000; }
    location /api/  { allow 192.168.0.0/16; deny all; proxy_pass http://127.0.0.1:8000; }
    location /static/ { allow 192.168.0.0/16; deny all; proxy_pass http://127.0.0.1:8000; }
}
```

走反代后记得在控制台把「网络 → 信任反向代理头」打开，这样控制台里显示的来源 IP 才是真实客户端，而不是反代地址。

**群晖反代**：控制面板 → 登录门户 → 高级 → 反向代理服务器 → 新增；
来源 `https://ai.lan:443`，目标 `http://localhost:8000`。
自定义标题里加上 `X-Forwarded-For`，并在「高级 → 自定义响应头」中关闭缓冲（若界面没有该选项，就用上文的 Nginx 方案）。

---

## 五、NAS 上的常见坑

| 现象 | 原因与处理 |
| --- | --- |
| 容器启动后马上退出 | 数据目录权限不对。给它 `chown -R 10001:10001 <数据目录>`（镜像里用的是 uid 10001 的非 root 用户） |
| 局域网访问控制台一直提示要令牌 | 这是设计如此：非环回地址必须用管理员令牌登录。令牌在 `secrets.json` 或 `docker compose exec airelay python -m airelay --print-token` |
| 流式回答一顿一顿、最后一大坨 | 反代缓冲没关。见上面 `proxy_buffering off` |
| 改了端口/监听地址没生效 | 容器与前台的运行方式需要在外部重启；只有托盘或进程内宿主会自动重绑定 |
| 统计里时间是 UTC | 库里统一存 UTC，界面按浏览器本地时区显示；容器的 `TZ` 影响日志时间 |
| SQLite 报 database is locked | 已开 WAL 并设了 30s busy_timeout；仍出现的话检查数据目录是不是在网络文件系统（NFS/SMB）上——把数据目录换到本地卷 |
| 升级后想保留数据 | 只替换代码/镜像，数据目录别动；`secrets.json` 是解密上游密钥的主密钥，丢了就得重填所有上游 Key |

---

## 六、备份与迁移

需要备份的只有一个目录：数据目录（`airelay.db`、`logs/`、`secrets.json`）。

```bash
# 安全备份：先让服务落盘（或在控制台里没有任何进行中的请求时）直接拷
tar czf airelay-backup-$(date +%F).tar.gz -C /volume1/docker/airelay data
```

迁移到新机器：装好程序 → 把数据目录整个拷过去 → 启动。
因为主密钥在 `secrets.json` 里，渠道的上游 API Key 无需重新填写。
