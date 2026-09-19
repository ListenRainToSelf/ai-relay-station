# 本地 AI 中转站

把十几家上游厂商、三种协议、一堆模型，折叠成本机/局域网上的**一个托盘图标 + 一个统一端口**的私人 AI 网关。
本机所有支持 OpenAI 协议的客户端（Cursor / OpenWebUI / 各种脚本）共用一套**不限速、可计费、可观测**的接入能力。

- **对外**：只暴露 OpenAI 兼容协议 `/v1/chat/completions`（含流式），客户端不用改代码。
- **对内**：OpenAI / DeepSeek、Anthropic Claude、Google Gemini 三套协议归一化成同一批「渠道」，可按优先级与权重调度、故障自动切换。
- **两层解耦**：上游厂商密钥（渠道层）与发给客户端的本地密钥（Key 层）互不相干，本地 Key 有独立的配额、限速、模型授权与账本。
- **两种形态**：Windows 上托盘常驻 + 独立控制台窗口；Linux NAS 上无头常驻（systemd / Docker）。

---

## 目录

- [核心能力](#核心能力)
- [快速开始](#快速开始)
  - [Windows（桌面常驻）](#windows桌面常驻)
  - [Linux / NAS（无头服务）](#linux--nas无头服务)
  - [Docker（NAS 最省事的方式）](#dockernas-最省事的方式)
- [第一次配置：三步接入](#第一次配置三步接入)
- [客户端接入示例](#客户端接入示例)
- [架构](#架构)
- [项目结构](#项目结构)
- [命令行](#命令行)
- [配置项](#配置项)
- [接口一览](#接口一览)
- [运维与排障](#运维与排障)
- [性能约定（改这几处代码前先读）](#性能约定改这几处代码前先读)
- [开发与测试](#开发与测试)
- [需求对照与实现边界](#需求对照与实现边界)

---

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 统一协议面 | `/v1/chat/completions`、`/v1/completions`（旧客户端）、`/v1/models`，流式逐块转发、边收边发 |
| 三协议适配 | OpenAI 兼容透传、Anthropic Messages 具名事件 → OpenAI SSE、Gemini `generateContent` → OpenAI 结构（含工具调用与图片） |
| 渠道路由 | 模型别名映射、渠道级模型白名单、`priority` + `weight` 加权随机、故障切换重试、出错渠道熔断冷却 |
| 平台式密钥 | `sk-relay-<前缀>-<随机>`，支持过期、配额、RPM/TPM 限速、模型授权（glob）、启用/停用；**明文加密存库，创建后可随时再次查看与复制** |
| 密钥可反复复制 | 列表行有「复制明文」、详情里有「显示明文」，取回多少次都行；明文彻底丢失时可「重新生成密钥值」，旧值立即失效而配额与统计保留 |
| 用量与计费 | 每次请求落明细（时间戳 / tokens / 首字延迟 / 输出速度 / 重试次数 / 状态），另按 key × 模型 × 分钟桶预聚合；可配单价表折算费用 |
| 多能力（不只对话） | 语音合成 `/v1/audio/speech`、语音识别 `/v1/audio/transcriptions`、图片生成 `/v1/images/generations`；**按能力路由**：TTS 请求不会发给只会对话的渠道 |
| 多模态 | 对话里可带图片/音频/视频，Gemini 的 inlineData 会映射成 OpenAI 的 `images` / `audio` 字段；模型能力在 `/v1/models` 里可见 |
| 趋势图筛选与悬停 | 折线图可按**本地密钥**与**模型**筛选、可切换「合计 / 按模型对比」；鼠标悬停在点上显示该时间点的输入/输出/合计 tokens、请求数、错误数与费用；所有数字按精确值显示（不缩写） |
| 同 Key 并发 | 同一个本地密钥可以并发调用，不串行、不串号；额度统计在并发下仍精确（SQL 层自增），明细逐条落库 |
| 实时会话 | 进程内活跃请求注册表 + WebSocket 推送：正在跑的请求、已收 tokens、瞬时速度、僵死判定 |
| 渠道余额 | DeepSeek 内置 `/user/balance` 适配器，其它厂商可自定义余额 URL 与取值路径，带历史快照与低余额提醒 |
| 本地服务托管 | 把本机推理服务（如 `start.bat` 拉起的 llama.cpp）纳入托管：探活、**掉线自动重启**、一键启停、进程日志；一个脚本拉起多个端口时只执行一次启动命令 |
| 模型别名 | `claude-4 → claude-sonnet-4-5-20250929`，支持 `claude-*` 通配与兜底别名，客户端只见别名 |
| 设置中心 | 网络 / 网关 / 限流 / 监控 / 余额 / 路由 / 计价 / 日志 / 安全 / 界面 共 10 组带类型校验的设置项，落 SQLite |
| 热重绑定 | 服务 IP 与端口在控制台改完即生效——进程内宿主会优雅停掉旧监听、在新端口重启，不退出进程 |
| 两种部署 | Windows：托盘 + 控制台独立窗口（pywebview 或 Edge/Chrome `--app`）；Linux NAS：systemd / Docker 无头 |

---

## 快速开始

### Windows（桌面常驻）

```powershell
git clone <本仓库> airelay ; cd airelay

# 一步到位：建虚拟环境、装依赖、自检、拉起服务、可选开机自启
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Autostart
```

装完会打印控制台地址与管理员令牌。日常启停用项目根目录的两个脚本（双击即可）：

| 脚本 | 行为 |
| --- | --- |
| `启动中转站.cmd` | 托盘图标 + 控制台窗口 + 可见日志；关掉窗口即退出服务 |
| `启动中转站-静默.cmd` | 纯托盘常驻（`pythonw`，无窗口）；退出请右键托盘图标 → 退出，日志在数据目录 `logs/airelay.log` |
| `start.sh` | Linux / macOS 启动（有图形界面走托盘，否则无头服务） |

托盘菜单：打开控制台 / 复制网关地址 / 复制 OpenAI 基地址 / 打开数据目录 / 打开日志 / 重新读取设置 / 退出；
状态那一行会显示当前活跃会话数。脚本会先做依赖自检（缺什么装什么），失败时给出可执行的提示。

> **改这两个 `.cmd` 时请注意：必须保持纯 ASCII + CRLF。** cmd.exe 用系统 OEM 代码页解析批处理，
> UTF-8 中文的尾字节会吃掉行尾的 CR，导致下一行被当成命令执行（实测能把 `@echo off` 变成
> `锘緻echo off` 而报「不是内部或外部命令」）。中文说明放在本 README 里，脚本里只用英文。

> 不想用脚本的话：
> ```powershell
> python -m venv .venv ; .\.venv\Scripts\pip install -r requirements-desktop.txt
> .\.venv\Scripts\python -m airelay
> ```

### Linux / NAS（无头服务）

```bash
sudo ./scripts/install-linux.sh --host 0.0.0.0 --port 8000
```

脚本会建运行用户、装到 `/opt/airelay`、数据放 `/var/lib/airelay`、写好 systemd 单元并 `enable --now`。
NAS 上没有 systemd（群晖 DSM / 威联通 QTS 的旧型号）时用 `--no-systemd`，或直接走下面的 Docker。

```bash
# 手动部署也只要三行
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
AIRELAY_DATA_DIR=/volume1/airelay ./venv/bin/python -m airelay --mode server --host 0.0.0.0 --port 8000
```

### Docker（NAS 最省事的方式）

```bash
docker compose up -d          # 数据落在 ./data，端口 8000
docker compose logs -f
docker compose exec airelay python -m airelay --print-token   # 查管理员令牌
```

群晖 / 威联通也可以直接把 `docker-compose.yml` 粘进 Container Manager。
想只让局域网访问就把端口映射改成 `"127.0.0.1:8000:8000"`，再由 NAS 的反向代理统一入口。

---

## 第一次配置：三步接入

1. 打开 `http://<地址>:8000/admin`，用管理员令牌登录（远程访问必须登录；本机环回地址会自动放行）。
2. **渠道** → 新建渠道：填厂商给的 `base_url` 与 API Key，选协议（OpenAI 兼容 / DeepSeek / 智谱 GLM / Claude / Gemini / 小米 MiMo），按需填模型白名单。
   配完点「探针」确认连通。
3. **密钥** → 创建密钥：得到一个 `sk-relay-...` 明文密钥（**只显示这一次**），设好配额与可用模型。把它填进客户端即可。

可选但建议：
- **模型别名**：把 `claude-sonnet-4-5-20250929` 这类长 id 折叠成 `claude-4`，客户端只记短名；页面上有「路由试算」可以先验证会走哪条渠道。
- **设置 → 计价**：填每个模型的每百万 token 单价，控制台就会显示预估费用；不填也能正常记账，只是费用恒为 0。
- **余额**：DeepSeek 渠道开箱可用；其它厂商在渠道编辑里填自定义余额 URL 与取值路径。

> **填模型名的地方都有下拉候选**（渠道白名单、密钥的允许模型、别名映射的上游模型、路由试算、
> 设置里的兜底模型别名、计价表）。白名单这类多选输入点开是**按渠道分组**的面板：每个渠道一组，
> 组标题上标出「上游可用 N 个 / 上游列表没拉到：原因 / 已禁用」，组里是该渠道白名单与上游真实 id 的
> 并集——所以你想找的模型「是哪一家的」一眼就能看出来。面板带过滤框、↑↓ 选 + 回车加、
> 右下角「刷新上游模型」（点它才真去问上游，平时读缓存）。候选只是提供方便，输入框仍然可以手输，
> 通配符（`claude-*`）照旧。计价表的键就是模型名，上面那排「快捷添加」按钮能直接生成一条待填单价的
> 条目，省得手打（单价键错一个字母只会悄悄不生效）。
>
> **新建渠道时也能拉模型**：表单里的「拉取模型列表」在**未保存**的新渠道上同样可用（走
> `POST /api/admin/channels/models/preview`，直接用表单里还没落库的 `base_url` / API Key 去问上游），
> 拉回来的列表里点模型名或「全部加入」就填进白名单标签；改已有渠道但还没保存时也可用，API Key
> 留空会自动沿用库里存的那把。渠道列表每行的「白名单」按钮则是同一个弹窗的已保存版本。

> **`base_url` 怎么写都行**。网关按「版本段 + 端点尾」自动拼接，下面几种写法等价：
> `https://open.bigmodel.cn/api/paas/v4`、带末尾斜杠的 `.../api/paas/v4/`、
> 甚至把厂商文档里那条完整端点 `https://open.bigmodel.cn/api/paas/v4/chat/completions` 整条粘进来。
> 版本段不写死枚举——`v1`、`v1beta`、`v4`、`v2alpha` 都认（智谱用的正是 `v4`）；
> 端点尾（`/chat/completions`、`/images/generations`、`/audio/speech`…）会被自动摘掉，
> 免得拼成 `.../chat/completions/v1/chat/completions` 这种必然 404 的地址。

---

## 客户端接入示例

任何 OpenAI 兼容客户端都只需要两样东西：**基地址** `http://<地址>:8000/v1` 和**本地密钥** `sk-relay-...`。

```bash
# curl
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-relay-xxxxxxxx-xxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"fast","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

```python
# 官方 SDK：把 base_url 指到本地网关即可，模型名用别名
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-relay-xxxxxxxx-xxxxxxxxxxxxxxxx")
for chunk in client.chat.completions.create(
    model="claude-4", messages=[{"role": "user", "content": "写一句诗"}], stream=True
):
    print(chunk.choices[0].delta.content or "", end="")
```

- **Cursor / Continue / 各类 IDE**：模型供应商选 OpenAI Compatible，Base URL 填 `http://<地址>:8000/v1`，API Key 填本地密钥，模型名填你配的别名。
- **OpenWebUI**：连接设置里 Base URL 填 `http://<地址>:8000/v1`，Key 填本地密钥；模型下拉会列出你在别名表里配置的名字。
- 请求响应头里带 `X-Request-Id` / `X-Airelay-Channel` / `X-Airelay-Upstream-Model`，排查问题时很好用。

---

## 架构

系统是「单进程宿主 + 内嵌 HTTP 服务 + 静态控制台」的分层结构，对外分成三个面，互不干扰：

| 面 | 作用 | 路径 | 谁能进 |
| --- | --- | --- | --- |
| 协议面 | 给客户端调模型 | `/v1/*` | 本机 / 局域网，用本地 Key 鉴权 |
| 管理面 | 密钥、渠道、统计、设置 | `/api/admin/*` | 环回地址直接进；远程需管理员令牌，登录后换 HttpOnly Cookie |
| 应用面 | 托盘图标 + 控制台窗口 | `/admin`、`/static/*` | 本机 GUI |

```mermaid
graph TB
    CLI["客户端<br/>Cursor / OpenWebUI / 脚本"]
    subgraph 宿主["（托盘）常驻宿主"]
        GW["统一入口 /v1/chat/completions"]
        AUTH["鉴权 · 配额 · 限速"]
        ROUTER["路由 · 别名 · 加权 · 熔断"]
        ADAPT["协议适配器<br/>OpenAI / Anthropic / Gemini"]
        METER["用量计量 + 会话监控"]
        ADMIN["管理面 /api/admin + WebSocket"]
        CONSOLE["控制台静态页 + 托盘"]
    end
    DB[("SQLite<br/>keys/channels/usage/stats")]
    EXT["上游厂商<br/>DeepSeek · OpenAI · Claude · Gemini"]

    CLI --> GW --> AUTH --> ROUTER --> ADAPT --> EXT
    ADAPT --> METER --> DB
    AUTH --> DB
    ROUTER --> DB
    CONSOLE --> ADMIN --> DB
```

**两层模型**：上游渠道是「货源」（填厂商的 base_url + key），本地 Key 是「发出去的凭证」；
本地 Key 不绑定具体上游，由路由按别名与权重挑一条渠道完成调用，用量记在该 Key 名下。

**流式为什么不缓冲**：上游 SSE 用 httpx 逐行读取后立刻转成 OpenAI chunk 下发，所以「输出速度」是真实测量值；
同时每收到一块就更新活跃会话，控制台上的速度是实时刷新的。

---

## 项目结构

```
airelay/
├── __main__.py          CLI 入口（--mode / --host / --port / --doctor / --print-token …）
├── main.py              FastAPI 应用工厂（三个面的挂载、统一错误处理）
├── host.py              uvicorn 宿主：后台线程 + 监听热重绑定
├── context.py           应用上下文：装配配置、数据库、服务与后台任务
├── settings.py          设置注册表（类型校验 + 持久化 + 变更通知）
├── models.py  db.py     SQLAlchemy 模型与 SQLite（WAL）引擎
├── security.py          密钥生成/哈希、上游密钥加密、管理员会话签名
├── proxy.py             上游调用编排：路由 → 适配 → 重试 → 流式 → 计量
├── adapters/            openai / anthropic / gemini 三协议适配器 + 注册表
├── services/            keys · channels · routing · mapping · usage · live · balance · ratelimit · supervisor · maintenance
├── api/                 gateway（协议面）· admin（管理面）· ws（实时推送）· ui（静态页）· deps（鉴权）
├── desktop/             托盘、独立窗口、桌面外壳编排
└── web/                 控制台（原生 HTML/CSS/JS，离线可用，无构建步骤）
scripts/                 install-linux.sh · install-windows.ps1 · airelay.service · apply_config.py · acceptance_test.py
tests/                   pytest：适配器单测 + 协议面/管理面端到端 + 实时推送 + 热重绑定（含假上游）
docs/NAS部署.md          群晖/威联通/反代/SQLite 落盘的专项说明
```

### 配套脚本

| 脚本 | 用途 |
| --- | --- |
| `scripts/apply_config.py` | 按 JSON 规格批量接入渠道 / 别名 / 密钥，并逐渠道做真实探针与模型列表自检。新机器配置、数据目录重建、换供应商时最省事 |
| `scripts/acceptance_test.py` | 对运行中的网关做端到端验收（协议面、别名、错误码、限流配额、计量落库、实时会话、余额、导出、路由试算），自动创建/清理临时资源 |
| `scripts/seed_demo.py` | 起本地假上游 + 播种演示数据，用于手工点控制台 |
| `scripts/install-linux.sh` | Linux / NAS 一键安装（建用户、venv、systemd） |
| `scripts/install-windows.ps1` | Windows 一键安装（venv、自检、启动脚本、开机自启） |

```bash
# 批量接入（规格文件含密钥，记得别提交）
python scripts/apply_config.py --spec spec.json --token <管理员令牌> --probe --list-models

# 真上游验收（需要一把本地密钥 + 管理员令牌）
python scripts/acceptance_test.py --url http://127.0.0.1:8412 \
    -k sk-relay-xxxx-xxxx -t <管理员令牌> --small-model fast --big-model mimo
```

---

## 命令行

```bash
python -m airelay                        # 自动判断形态：Windows 走桌面，其它走无头服务
python -m airelay --mode server          # 强制无头常驻
python -m airelay --mode desktop         # 强制桌面（托盘 + 窗口）
python -m airelay --host 0.0.0.0 --port 8090
python -m airelay --data-dir /volume1/airelay
python -m airelay --print-token          # 查看管理员令牌
python -m airelay --rotate-token         # 轮换管理员令牌后启动
python -m airelay --doctor               # 环境自检（目录可写、端口可用、依赖齐全）
python -m airelay --print-config         # 打印全部设置项清单（JSON）
python -m airelay --set gateway.max_retries=2 --set logs.level=DEBUG   # 临时覆盖，不写库
```

环境变量（容器 / systemd 常用）：`AIRELAY_DATA_DIR`、`AIRELAY_HOST`、`AIRELAY_PORT`、`AIRELAY_MODE`、`AIRELAY_ADMIN_TOKEN`。
命令行参数优先级高于库中设置，但不会写回数据库。

---

## 配置项

控制台「设置」页按 11 组呈现，全部落 SQLite，改完立即生效（网络类会触发热重绑定）：

| 分组 | 代表项 |
| --- | --- |
| 网络 | 服务 IP、服务端口、对外基地址、信任反代头、跨域白名单 |
| 网关 | 请求总超时、建连超时、首字节超时、流式静默超时、换渠道重试次数、熔断冷却、默认 max_tokens |
| 本地服务托管 | 托管总开关、默认探活间隔、随网关自动启动、网关退出时一并关闭、上游模型目录缓存秒数 |
| 限流 | 全局 RPM / TPM、新建 Key 默认 RPM / TPM |
| 会话监控 | 僵死判定秒数、推送开关与间隔、最近请求条数 |
| 余额 | 自动刷新开关与间隔、低余额告警阈值 |
| 路由 | 渠道粘滞分钟、兜底模型别名、单请求候选渠道上限 |
| 计价 | 币种、模型单价表（每百万 token）、默认单价 |
| 日志 | 级别、明细保留天数、访问日志开关 |
| 安全 | 会话有效期、远程是否强制令牌、本地密钥前缀 |
| 外观与界面 | 主题、分页条数、开机自启 |

数据目录（默认 Windows `%LOCALAPPDATA%\airelay`，Linux `~/.local/share/airelay`）里放三样东西：
`airelay.db`（SQLite）、`logs/airelay.log`（滚动日志）、`secrets.json`（主密钥 + 管理员令牌 + 哈希 pepper，权限 0600）。

---

## 语音、图片与其他模态

网关对外仍然只有一套 OpenAI 兼容协议，但**能力从「对话」扩到了语音与图片**。

### 三个新端点

```bash
# 语音合成：返回音频文件（wav/mp3 取决于上游）
curl -sS http://127.0.0.1:8000/v1/audio/speech \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"tts","input":"今天天气不错","voice":"冰糖"}' -o speech.wav

# 语音识别：multipart 上传音频，返回 {"text": "..."}
curl -sS http://127.0.0.1:8000/v1/audio/transcriptions \
  -H "Authorization: Bearer $KEY" \
  -F model=asr -F language=zh -F file=@speech.wav

# 图片生成：n 张，返回 b64_json（或上游给的 url）
curl -sS http://127.0.0.1:8000/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"img","prompt":"一只在窗台上打盹的橘猫","n":1}'
```

### 能力（capabilities）

每个协议声明自己支持哪些能力，渠道可以在此基础上**再收紧**（不能凭空多出协议没有的能力）：

| 能力 | 说明 | 谁有 |
| --- | --- | --- |
| `chat` | 文本对话 | 全部 |
| `vision` | 对话里接受图片输入 | OpenAI 兼容、Claude、Gemini、MiMo |
| `audio_in` | 对话里接受音频输入 | OpenAI 兼容、Gemini、MiMo |
| `audio_out` | 对话里返回音频 | OpenAI 兼容、MiMo |
| `speech` | `/v1/audio/speech` | OpenAI 兼容、Gemini、MiMo |
| `transcription` | `/v1/audio/transcriptions` | OpenAI 兼容、Gemini、MiMo |
| `images` | `/v1/images/generations` | OpenAI 兼容、Gemini |

路由分两级，**这一点很关键**：

| 级别 | 包含 | 行为 |
| --- | --- | --- |
| **硬门槛** | 端点自身的能力（对话要 `chat`、TTS 要 `speech`、图片要 `images`、识别要 `transcription`） | 不满足的渠道直接排除，避免把请求发给不可能处理的渠道 |
| **偏好** | 对话里的输入/输出模态（`vision` / `audio_in` / `audio_out`） | 满足的渠道**排在前面**，没有的话请求照样下发，由上游决定怎么处理 |

> 为什么模态只做偏好？因为把「带图片」当成硬门槛会**误伤真实会话**：一个纯文本渠道的模型
> （如 DeepSeek）收到带截图的消息时，以前是转发过去让上游判断，现在会被网关直接判成
> `NO_CHANNEL_AVAILABLE`，客户端表现成「重连中」。实测 DeepSeek 自己会对图片给出明确校验错误
> （`messages[0].image[0]: You have uploaded an unsupported image`），这比网关凭空拒绝有用得多。

`/v1/models` 里能看到每个模型的能力，客户端可以据此决定要不要调语音接口。

> **只写 glob 的渠道不会出现在 `/v1/models` 里**。`glm-*` 这类通配展开不成具体名字，
> 列表只收录「别名」和「白名单里写死的模型名」。想让客户端下拉能选到，就把常用的具体 id
> （如 `glm-4-flash` / `glm-4v-flash` / `cogview-3-flash`）一并写进白名单——和通配不冲突，
> 通配继续负责「以后新出的模型自动放行」。

### 各家上游的实测差异（网关会替你翻译）

同样是「语音合成」，三家上游的实现完全不同，适配器负责抹平：

| 上游 | 语音合成 | 语音识别 | 图片生成 |
| --- | --- | --- | --- |
| OpenAI 兼容 | 原生 `POST /audio/speech`，返回音频字节 | 原生 `POST /audio/transcriptions`（multipart） | 原生 `POST /images/generations` |
| 小米 MiMo | **没有音频端点**，走 `/chat/completions`：把文本放进 **assistant** 消息，音频在 `message.audio.data`（base64 WAV，24kHz） | 同样走 `/chat/completions`，音频放 `input_audio` 块 | 不提供 |
| Google Gemini | `generateContent` + `responseModalities:["AUDIO"]`，返回**裸 PCM**，网关补 WAV 头 | `generateContent` 带 inlineData 音频，取回复文本 | `generateContent` + `["TEXT","IMAGE"]`；模型名含 `imagen` 的走 `:predict` |
| Anthropic Claude | 不支持 | 不支持 | 不支持 |
| DeepSeek | 不支持 | 不支持 | 不支持 |
| 智谱 GLM | 原生 `POST /audio/speech`，模型要用 `cogtts`（`glm-4-voice` 不是 TTS 模型）；**未实测**，本账号免费额度不含 | 原生 `POST /audio/transcriptions`；同上未实测 | 原生 `POST /images/generations`（`cogview-*`），实测可用 |

> **智谱 GLM 的三个实测细节**（`provider_type: zhipu`，预设 base_url `https://open.bigmodel.cn/api/paas/v4`）：
> ① 端点前缀的版本段是 `v4`，早期网关只认 `v1`/`v1beta`，拼接时会把 `v4` 丢掉，
> 表现成「配了渠道但一律 404」——现在按任意版本段识别，整条端点 URL 粘进来也能用；
> ② 官方 `/models` 只返回 11 个 id（`glm-4.5` 到 `glm-5.3`），**不含**真正免费可用的
> `glm-4-flash` / `glm-4v-flash`，所以它不能当能力清单用，以实际调用为准；
> ③ 免费额度只覆盖 `glm-4-flash`（对话）、`glm-4v-flash`（视觉）、`cogview-3-flash`（文生图），
> 其余模型（`glm-4.6`、`glm-5.3-flash`、`cogview-3`、`cogtts` 等）统一返回
> `429 余额不足或无可用资源包,请充值。`——这是账号计费状态，不是网关问题。

> **两个容易踩的上游约束**（实测踩到过，已固化进适配器与假上游）：
> 1. MiMo 的 TTS **必须**有 assistant 消息承载待合成文本，否则 `400 messages must contain an assistant role for TTS model`。
>    音色只能用它的这套：`mimo_default / 冰糖 / 茉莉 / 苏打 / 白桦 / Mia / Chloe / Milo / Dean`；
>    常见的 OpenAI 音色名（alloy/nova/…）会被自动映射过去。另外 `mimo-v2.5-tts-voicedesign`
>    要求在 user 消息里给音色描述，`mimo-v2.5-tts-voiceclone` 需要参考音频。
> 2. MiMo 的 ASR **不接受** text 内容块，且只能有一个 `input_audio` 块
>    （`ASR request must not include text parts` / `requires exactly one input_audio part`）；
>    提示词由上游自己注入，`language` 只能作为顶层字段传。

### 计量与计价

非对话能力没有 token，按各自的单位计量并写进用量明细与图表：

| 能力 | 计量单位 | 明细里的字段 |
| --- | --- | --- |
| 语音合成 | 字符数 | `units` + `unit_kind=character` |
| 语音识别 | 音频秒数（不足一秒算一秒） | `units` + `unit_kind=second` |
| 图片生成 | 张数 | `units` + `unit_kind=image` |

计价表按同样的口径填（每百万字符 / 每秒 / 每张），例如：

```json
{
  "tts-model": {"character": 100},
  "asr-model": {"second": 0.0002},
  "img-model": {"image": 0.04}
}
```

对话仍按 token 计价（`prompt` / `completion`）。控制台的「用量统计 → 请求明细」会把媒体请求显示成
`18 字符` / `3 秒` / `2 张`，不再显示成 0 token。

## 本地服务托管（可选）

除了云端厂商，本机跑的推理服务（llama.cpp / vLLM / Ollama / 自己的 `serve.py`）也能交给网关托管：
**没起来就拉起来、意外退出自动重启、需要时一键关掉**，状态和日志都在「本地服务」页里。

### 怎么配

在「渠道」里新建（或编辑）一条指向本机地址的渠道，例如 `http://127.0.0.1:8080/v1`，
然后在表单底部的 **本地进程托管** 一节填启动命令：

```text
名称          本地 llama.cpp
协议          OpenAI 兼容
base_url      http://127.0.0.1:8080/v1
上游 API Key  <本地服务自己的 key>   ← 例如 llama-server 启动参数里的 --api-key
模型白名单     my-model-a, my-model-b

启动命令      D:\my-llm\start.bat          ← 会以当前用户身份执行
工作目录      D:\my-llm
健康检查路径  /v1/models                 ← 对该路径发 GET，任何 HTTP 响应都算「在跑」
启动宽限(秒)  60                         ← 起来后先等这么久，别在加载模型时就判失败
自动启动      ✓  网关启动时自动拉起
自动重启      ✓  掉线自动拉起
退出时关闭    ✗  网关退出时保留它（按需勾选）
```

配好后在「本地服务」页可以看到状态、PID、运行时长、重启次数、最近探活延迟与最近事件，
并能 启动 / 停止 / 重启 / 立即探活 / 查看日志（进程输出会落到数据目录 `logs/services/`）。

### 三条行为约定

1. **探活是唯一事实来源**，不看进程是否存活。`start.bat` 这类脚本常常拉起后台进程后自己就退出了
   （甚至 detached），所以"启动进程还在"并不能说明服务可用——只要端口后面能应答 HTTP 就认为它在跑。
   探活**不带** API Key，所以本机服务返回 `401`（如 llama-server 配了 `--api-key`）同样算「在跑」，
   界面上会显示成 `HTTP 401（在跑，需鉴权）`。这样即便上游密钥填错，也不会把健康进程误杀重启。
   另外每次拉起后会有一段**就绪窗口**（启动宽限 / 重启退避 / 探活间隔取最大值再加一个探活周期），
   期间探活失败不计数——模型加载几十秒也不会被误判成掉线。
2. **已经有人在跑就绝不重复拉起**。启动前先探活：如果你已经手动双击过 `start.bat`，
   网关只会把它登记为「外部进程」，不会起第二份（本机推理服务重复启动会抢显存/端口）。
3. **一个启动命令只执行一次**。多个渠道指向同一个脚本时（一个脚本拉起多个端口的常见形态），
   它们共享同一个「托管单元」，不会各拉起一遍。

### 停止策略

| 策略 | 行为 | 适用 |
| --- | --- | --- |
| `auto`（默认） | 先杀网关拉起的进程树；停不掉再按端口终止监听进程 | 通用 |
| `process` | 只杀自己拉起的进程树 | 前台常驻型服务（如 `start.bat` → `python serve.py`） |
| `port` | 按渠道 base_url 的端口，终止占用它的进程 | 脚本 detach 后拿不到子进程句柄 |
| `command` | 执行你配置的停止命令（如 `stop.bat`） | 服务自带优雅停止脚本 |

> 停止只会动两类进程：**网关自己拉起的进程树**，或**占用该渠道端口的进程**。
> 不会按进程名乱杀（自带的 `stop.bat` 常写 `taskkill /IM llama-server.exe`，那会杀掉机器上所有 llama-server，注意取舍）。

### 重启策略与护栏

- 连续探活失败达到「失败几次才重启」（默认 3 次）才重启，避免一次网络抖动就重启；
- 重启按「重启退避」线性放大，最多 10 倍；
- 「重启上限」达到后停止自动重启并标记为**异常**，不会无休止地拉起（避免刷屏/占满 CPU）；
- 启动宽限期内的失败不计入重启决策，让模型有时间加载。

### 安全提示

**填启动命令 = 让网关在本机执行命令**，所以管理面（`/api/admin`）的访问控制就是唯一门槛：
保持环回绑定或务必设管理员令牌，不要把管理面暴露到公网。只填你自己信任的脚本。

相关设置：设置 → **本地服务托管**（总开关、默认探活间隔、随网关自动启动、退出时一并关闭）。

---

## 接口一览

协议面（OpenAI 兼容，Bearer 鉴权）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/chat/completions` | 主力接口，支持 `stream` SSE |
| POST | `/v1/completions` | 旧版文本补全，内部映射到 chat 再转回 legacy 结构 |
| GET | `/v1/models` | 别名表 / 渠道声明的模型列表 |
| GET | `/v1/models/{id}` | 查某个模型会映射到哪个上游模型 |

管理面（环回免登录，远程需令牌）：`/api/admin/` 下的
`session`、`system`、`keys`（含 `reset-usage` / `rotate` / `secret`）、`channels`（含 `probe` / `models` / `reset-cooldown` / `models/preview`）、
`services`（本地服务：`{id}/start|stop|restart|check`、`bulk/{action}`、`{id}/log`）、
`models/map`（含 `import` / `resolve`）、`models/catalog`、`stats`（含 `logs` / `export.csv`）、`live`、`health`、`balance`（含 `refresh` / `history`）、`settings`。

其中两个给「填模型名」用的接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/admin/channels/models/preview` | 按**还没落库**的表单值（`provider_type` / `base_url` / `api_key`）拉一次上游 `/models`；`api_key` 留空且给了 `channel_id` 时沿用库里那把密钥。新建渠道时用 |
| GET | `/api/admin/models/catalog` | 按渠道分组返回模型目录（每个渠道的白名单 + 上游真实 id + 取回时间 + 失败原因），`?refresh=true` 强制并发刷新。默认走缓存，TTL 是设置里的 `services.catalog_ttl_seconds`（600 秒） |

运维探针：`/healthz`（存活）、`/readyz`（就绪）。交互式接口文档：`/docs`。

---

## 运维与排障

### 接真实上游时值得先知道的三件事

1. **推理模型的 `max_tokens` 要给够**。DeepSeek v4、MiMo v2.5 这类模型的输出预算会先花在思考上，
   `max_tokens: 16` 很可能只得到空正文（token 全记为 `reasoning_tokens`）。
   网关会把上游的 `reasoning_content` 原样透传（含流式），客户端可自行区分「思考」与「正文」；
   渠道页的「探针」默认发 128 token，就是为了避免出现「连通正常但正文为空」的误导结果。
2. **不同厂商的同一模型名可能都不认**。模型 id 各家用各家的，建议在「模型别名」里把长 id 折叠成
   客户端好记的短名；网关的 `/v1/models` 会同时列出别名与渠道声明的真实模型 id。
3. **计价表默认是空的**，所以「预估费用」恒为 0——这不是 bug。到「设置 → 计价」按每百万 token
   填单价后，控制台的费用列才会出现数值。

### 常见操作

```bash
# 日志
journalctl -u airelay -f                    # systemd
docker compose logs -f                      # 容器
tail -f "%LOCALAPPDATA%\airelay\logs\airelay.log"   # Windows

# 备份：停服务后直接拷数据目录即可（SQLite 用 WAL，热拷建议先执行一次检查点）
```

> **设置项都会落库**：在控制台改完的设置（含服务 IP / 端口）重启后依然生效。
> 命令行 `--host/--port/--set` 是临时覆盖，优先级高于库中配置、但不写库；
> 在控制台把同一项保存一次，就把它「锁定」成了持久配置。

> **密钥明文怎么拿**：本地密钥的明文是**加密存在数据库里**的（Fernet，主密钥在数据目录 `secrets.json`），
> 所以创建之后随时能在控制台再查看与复制：列表点「复制明文」，或详情里点「显示明文」。
> 每次取回都会在网关日志里留一条审计记录。
>
> 这是「平台式密钥管理」的取舍：方便，但数据目录因此成了**必须重点保护的机密**——
> 谁拿到 `airelay.db` + `secrets.json` 就等于拿到了你所有的本地密钥。
> 不想这样，可以在「设置 → 安全 → 本地密钥可再次查看」里关掉：之后新建的密钥只存哈希，
> 明文无法取回（老密钥已有的密文不受影响）。加密存储启用之前创建的密钥没有密文，
> 控制台会标「明文不可取回」，点「重新生成密钥值」即可换成可管理的一把。

> **管理面的权限等于密钥池**：因为管理面能读回明文，环回绑定与管理员令牌就是唯一的门槛。
> 不要把管理面暴露到公网；远程访问务必走令牌登录，并考虑只反代 `/v1/*`。

| 现象 | 先看这里 |
| --- | --- |
| 客户端 401 `INVALID_API_KEY` | 密钥是否填全（`sk-relay-` 开头）、是否被禁用或过期 |
| 403 `MODEL_NOT_ALLOWED` | 该本地 Key 的「允许模型」白名单不包含请求的模型名（别名也算） |
| 429 `QUOTA_EXCEEDED` / `RATE_LIMITED` | 配额用尽或触发 RPM/TPM；响应头 `Retry-After` 是建议等待秒数 |
| 503 `NO_CHANNEL_AVAILABLE` | 没有渠道服务该模型：看渠道白名单是否放开、渠道是否都在冷却中；用「模型别名 → 路由试算」直接定位 |
| 502 `UPSTREAM_ERROR` | 上游返回错误：把鼠标停在渠道列表「健康」列的徽记上看最近错误原文与发生时间，再配合「探针」结果，必要时核对上游密钥与 base_url |
| 流式突然中断 | 上游静默超过「流式静默超时」或超过「请求总超时」；调大这两个值，或检查渠道稳定性 |
| 改了端口没生效 | 前台/容器方式运行时需要在外部重启进程；托盘或进程内宿主方式会自动重绑定 |
| 控制台点了按钮没反应 | 多半是前端脚本抛了异常（页面上会弹一条「界面报错」提示，内容即异常信息）；把这条内容发出来，或按 F12 看控制台堆栈 |
| 下拉候选里看不到某个模型 | 候选按渠道分组，先看那组的标题：写「上游列表没拉到：原因」就点面板右下角「刷新上游模型」看具体报错（常见是上游 `/models` 需要鉴权或该协议不支持）；写「已禁用」说明渠道被停用了。都不行就直接手输模型名，白名单不依赖候选 |

控制台上每个请求都能看到 `X-Request-Id`，配合「用量统计 → 请求明细」可以直接对上号。

---

## 性能约定（改这几处代码前先读）

网关的管理面和协议面跑在**同一个事件循环**上。只要有人在 `/v1` 上跑长回答，控制台就得靠这个循环
才能响应——所以下面三条不是优化建议，而是防止「控制台打不开」的硬约束。

1. **流式路径上禁止「每块重算全文」**。统计已产出 token 请用 `TokenCounter` 增量累加，
   不要写成 `estimate_tokens("".join(accumulated))`：那是 O(块数 × 全文长度)，一次 1200 块的长回答
   要重复扫描几千万字符，纯 Python 字符循环会把事件循环占满（`tests/test_gateway.py` 里有一条
   回归测试专门数「实际被扫描的字符数」，它必须正好等于回答长度）。
2. **实时推送的 tick 要节流**。每个流式块都会触发一次状态变化，`LiveRegistry` 只按
   `TICK_MIN_INTERVAL_SECONDS`（0.2s）发轻量 tick；控制台面板走的是「按固定间隔推完整快照」，
   tick 只是提示，攒着发不影响观感。
3. **控制台的前端资源必须能失效**。`/admin` 的 HTML 是 `no-store`，`/static/*` 是 `no-cache`
   且在 HTML 里带 `?v=<前端文件 mtime>`：这样升级或重启之后，控制台窗口立刻用上新前端，
   不会出现「新后端配旧前端」这种最难查的组合。

前端刷新策略：统计类页面每 5 秒拉一次数据，用「请求数/错误数/token 总量…」组成的指纹判断有没有变化，
**没变化就不重绘**（避免图表整片闪动）；标签页在后台时不发请求，切回前台立刻补一帧。

---

## 开发与测试

```bash
pip install -r requirements-desktop.txt pytest anyio

# 跑全部测试（会自带一个假上游，覆盖三种协议、流式、故障切换、限流、配额、实时推送、端口热重绑定）
python -m pytest tests -q

# 只跑适配器翻译的单测
python -m pytest tests/test_adapters.py -q

# 起一个假上游 + 播种演示数据，方便手工点控制台
python tests/mock_upstream.py --port 9001 &
python -m airelay --mode server --port 8137 --data-dir ./devdata
python scripts/seed_demo.py --base http://127.0.0.1:8137 --token <管理员令牌> --chat 12
```

测试目录说明：`tests/mock_upstream.py` 是一个能模拟 OpenAI / Anthropic / Gemini 三种协议（含流式与故障注入）的假上游；
`tests/conftest.py` 提供临时数据目录、假上游、ASGI 客户端与真实端口宿主等夹具。

---

## 需求对照与实现边界

| 原始诉求 | 落地位置 |
| --- | --- |
| 托盘常驻，后台运行 | `desktop/`（pystray + 独立窗口）；NAS 上以 systemd/Docker 无头常驻 |
| 接入多个 API 聚合中转 | `services/channels.py` 多渠道 + `services/routing.py` 优先级/权重/熔断 |
| 支持主流三种接口 | `adapters/`（openai / anthropic / gemini） |
| 完善的独立窗口前端 | `airelay/web/`（原生 JS 控制台，八大专区，离线可用） |
| 像正常提供商一样创建 API Key | `services/keys.py` + 控制台「密钥」页（前缀、掩码、过期、配额、限速、模型授权） |
| 对 API Key 做时间戳分模型统计 | `usage_logs` 明细 + `api_key_stats` 分钟桶（`services/usage.py`） |
| 实时监测当前运行会话 | `services/live.py` + `/api/admin/live/ws` |
| 查询外部接入 API 的余额 | `adapters` 的 `build_balance_call` / `normalize_balance` + 控制台「余额」页 |
| 模型 ID 自定义 / 简化 | `services/mapping.py` + `/api/admin/models/map`（支持通配与绑定渠道） |
| 监测不同来源的输出速度 | 流式逐块计时，记录首字延迟与 tok/s（`proxy.py`） |
| 完善的设置系统（含服务 IP、端口） | `settings.py` 注册表 + 控制台「设置」页 + 监听热重绑定 |

**明确的边界**（与方案一致）：
- 不涉及上游账号的获取与付费授权，只把「已合法持有的渠道」接进来归一化；不含任何绕过或逆向。
- 网关不重算 token：优先采用上游返回的 `usage`；上游不返回时用字符启发式估算并在明细里标为 `estimated`。
- 余额是「上游账上还剩多少」，与本地 Key 的消费统计是两本账，界面上分开呈现。
- 打包签名 / 代码签名 / 自动更新不在当前范围。
- `pytest` 覆盖的是网关自身行为；各家上游的字段细节以官方最新文档为准，接入新厂商时建议先跑一次「探针」。
