# Quark VFS

Quark VFS 为本地 Agent 提供夸克网盘文件访问接口。目标工作流是下载资料、在本地处理、显式上传产物；多端访问沿用现有夸克客户端。

当前版本 `0.1.0` 已实现本地诊断、认证入口、共用文件核心及受控 CLI，包括上传下载、移动重命名、远端清单回收与恢复。M0 能力审计与 M1 真实验收已完成，包括文件/目录往返、移动重命名、冲突处理、逐项任务恢复，以及本地数据库遗失后的回收重建。这里的完成指 M1 核心工作流；批量重命名、搜索过滤等接口差异见 [能力矩阵](CAPABILITIES.md)。文件与回收接口需通过配置显式启用；MCP、归档替换和官方下载续传门禁保持关闭。

## 安装与运行

需要 Python 3.12 或以上版本和 uv。在仓库根目录执行：

```sh
uv sync --locked
uv run --locked quark-vfs version
uv run --locked quark-vfs doctor
```

CLI 可通过项目虚拟环境运行，也可构建 wheel 后单独安装。将配置保存在源码仓库之外的私有目录；薄 skill 的源码位于 [skills/quark-vfs](skills/quark-vfs/SKILL.md)，可按目标 Agent 的目录规则安装。

已在 macOS、Python 3.12 环境完成验收。其他平台未验收；Artifact 安全读取依赖 `O_NOFOLLOW` 和 `os.open(dir_fd=...)`，缺失时返回 `PATH_CHECK_UNAVAILABLE`。

尚未提供配置时，`doctor` 返回结构化的配置缺失结果和非零退出码。开发检查可以在未安装官方运行时、未登录网盘的情况下完成。

复制 `examples/config.toml` 到用户私有配置目录，填写 Node、官方入口、状态和缓存目录的绝对路径。官方运行时须单独安装在固定、可写、位于源码仓库之外的位置。此项目不包含官方安装包或认证文件。

```sh
uv run --locked quark-vfs --config /absolute/path/config.toml doctor
uv run --locked quark-vfs --config /absolute/path/config.toml status
```

`doctor` 和默认 `status` 检查本地配置、运行条件与本地绑定记录。`status` 默认不探测账号或云端 marker，也不读取凭证；认证状态保持未知。`status --verify-scope` 显式只读检查云端 marker，可与 `--refresh` 组合；`--refresh` 本身只刷新登录状态。验证状态为 `not_checked`、`passed`、`mismatch` 或 `unavailable`，其中 `unavailable` 表示验证未完成。

显式添加 `--probe` 会执行官方 `resolve-agent`：

```sh
uv run --locked quark-vfs --config /absolute/path/config.toml doctor --probe
```

官方运行时在命令钩子中包含配置初始化、旧配置迁移及遥测逻辑，因此探测可能发生本地写入与网络请求。包装层继承真实环境，不自行添加 Agent 身份字段，不上传整轮聊天。独立 CLI 与官方完整 skill 的支持约定见 `COMPATIBILITY.md`。

## 认证与实际验收

```sh
uv run --locked quark-vfs --config /absolute/path/config.toml auth login
uv run --locked quark-vfs --config /absolute/path/config.toml status --refresh
```

`auth login` 启动官方浏览器授权，包装层超时为 180 秒；上游通过服务端页面状态轮询等待授权，轮询截止 60 秒，不使用 localhost callback。`status --refresh` 调用官方 `get-user-info`，只发布受控认证状态，包装层不读取凭证。

集成验收使用经指纹核查的官方 1.0.24 包，验证了 `doctor --probe` 的 Codex 识别、浏览器登录的 `confirmed` 状态及账号刷新的 `authenticated` 状态。授权 scope 保持未知。受限文档往返烟雾已完成：下载 103 字节测试资料、按下载内容加工产物、上传下载核验，并移入本轮独占回收容器后再次验证两个文件内容。

## 早期 M0 文档往返样例

```sh
uv run --locked quark-vfs --config /absolute/path/config.toml verify-workflow
uv run --locked quark-vfs --config /absolute/path/config.toml verify-workflow --resume <verification-uuid>
```

该命令只处理新 UUID 测试目录内生成的资料，最终收纳到本轮独占测试回收目录。`--resume` 只继续已记录成功源上传的受限 checkpoint，不重传源文件；结果不确定时先核对 journal，不反复重跑命令。该早期样例的真实恢复已完成，包装层没有重复派发不确定写。

测试回收容器只有本地 journal，没有远端恢复 manifest；此次通过不代表完整 M0/M1、通用下载续传或正式回收恢复通过。

## 开发验证

```sh
uv run --locked python -m unittest discover -s tests -v
uv run --locked python -m compileall -q src tests
uv build
```

测试使用临时目录、合成协议样例和受控测试进程，不访问真实网盘。合成测试通过不代表官方集成或续传能力通过实测。

## 设计与进度

- [设计草案](references/Quark-VFS-Implementation-Draft-v0.1.md)
- [实施计划](docs/IMPLEMENTATION.md)
- [能力矩阵](CAPABILITIES.md)
- [兼容性约定](COMPATIBILITY.md)
- [M0 证据记录](docs/m0-evidence.md)
- [本地验收报告](docs/VERIFICATION.md)

M0 和 M1 实际样例、负向结果及限制见验收报告。默认冲突失败，显式保留双方；工具不提供云文件永久删除。

## M1 共用文件接口

通用 CLI 已接入共享 Catalog、Transfers、Operations、Trash 与 SQLite 状态；M1 专项真实验收已通过。`examples/config.toml` 的 `file_operations_enabled`、`trash_enabled` 默认 false，部署时需按验收结果显式开启。MCP、archive_replace、download_resume_verified 继续关闭。

先用 `init-scope --parent-fid 0` 创建并精确下载验证项目云 marker，然后才能使用绑定账号 alias 的文件接口；工具自动生成 scope UUID。已有 alias 不能自动重绑；本地数据库遗失时使用明确的 `bind-scope --scope-uuid UUID --management-parent-fid FID` 验证原命名空间。初始化是显式云写入。详细恢复分支见 [RECOVERY.md](RECOVERY.md)。

文件引用以 JSON 明确给出，例如 `--ref '{"fid":"完整FID","parent_fid_hint":"明确父FID"}'` 或 `--ref '{"path":"/资料/笔记.md"}'`；不接受自然语言命令或原始上游参数。主要入口：

```sh
quark-vfs --config /absolute/path/config.toml list --ref '{"fid":"0"}'
quark-vfs --config /absolute/path/config.toml stat --ref '{"path":"/资料/笔记.md"}'
quark-vfs --config /absolute/path/config.toml search --keyword '笔记'
quark-vfs --config /absolute/path/config.toml mkdir --parent '{"fid":"明确父FID"}' --name '产物'
quark-vfs --config /absolute/path/config.toml upload --parent '{"fid":"明确父FID"}' --local-path /absolute/path/output.md
quark-vfs --config /absolute/path/config.toml download --ref '{"fid":"完整FID","parent_fid_hint":"明确父FID"}' --local-target /absolute/path/output.md
quark-vfs --config /absolute/path/config.toml move --ref '{"fid":"完整FID","parent_fid_hint":"明确父FID"}' --target '{"fid":"明确目标FID"}'
quark-vfs --config /absolute/path/config.toml rename --ref '{"fid":"完整FID","parent_fid_hint":"明确父FID"}' --name '新名称.md'
quark-vfs --config /absolute/path/config.toml trash --ref '{"fid":"完整FID","parent_fid_hint":"明确父FID"}'
quark-vfs --config /absolute/path/config.toml restore --operation-id UUID --entry-id UUID
quark-vfs --config /absolute/path/config.toml jobs
quark-vfs --config /absolute/path/config.toml rebuild-trash
```

`tree` 按明确起点和深度浏览目录树，默认深度 2、目录预算 100、显示条目预算 1000，默认 JSON 输出。预算耗尽或分支失败时保留已读结果并标记 partial；它不代表全盘清单或原子快照。参数、预算边界与 partial 语义见 [CLI 工作流](skills/quark-vfs/references/cli.md#tree)。本地验证及真实网盘验收状态见[验收记录](docs/VERIFICATION.md)。

冲突默认 fail，支持的写操作可显式 keep_both。回收根目录采用配置 `trash_root_fid`，未配置时使用已验证的项目管理根。正式回收的 manifest、恢复事件及本地库遗失处理见 [RECOVERY.md](RECOVERY.md)。

必须保留 `QuarkVFS-<UUID>` 管理根、marker 和配置的回收根。作用域恢复及离线任务边界见 [RECOVERY.md](RECOVERY.md)。

通用传输单文件上限为 128 MiB；这是包装层的本地保护值，不是已确认的夸克容量上限，超过或无法安全确认大小时返回明确错误。`jobs --job-id UUID --resume` 对下载采用 fresh 来源和新 staging 安全重启，不声称 offset 续传；未知上传、移动或重命名先核对，拒绝自动重放。

上传默认排除秘密文件名和凭证目录，并在逐项结果中列出 skipped。用户明确要求上传这些文件时可传 `--include-secrets`；配置的包装层 state、cache 和官方运行时目录始终排除。移动与重命名支持 `--expected`：重命名单个 JSON 元数据，移动为按引用顺序对应的 JSON 数组，字段为 name、parent_fid、kind 与可选 size、modified、content_hash；已观测变化使操作停止。


M1 验收脚本的 `--resume-after-search /absolute/path/journal.jsonl` 只接受成功上传并核验源文件后停在只读搜索的严格前缀；先核对 marker、专用目录与源字节，再继续尚未执行的步骤。它不重复初始化或源上传，也不用于未知写结果。当前环境中带 browse 完整父 FID 的搜索返回上游 21001；验收脚本使用唯一合成 UUID 的全局候选搜索，并明确记录范围。产品 `search --parent-fid` 保留原始范围和错误，不静默扩大查询。

固定验收步骤 22 的 `--resume-predispatch-move journal.jsonl` 仅接受已确认发生在任务创建前的读取失败：本地不存在该源的 move job，源与小目录字节仍吻合，专用目标仍为空，才首次派发移动。它复用此前成功的目录上传、下载和分页结果，不重做这些步骤。已有写任务、未知副作用或目录状态改变时停止。

## 将 skill 提供给其他 coding agent

部署分为三个部分：skill 目录提供操作说明；已安装的 `quark-vfs` CLI 执行文件语义；配置文件指向官方运行时、状态、缓存和账号作用域。复制 skill 不会安装 Python 包、Node 或官方运行时，也不会复制登录状态。

将仓库中的 `skills/quark-vfs` 整个目录复制到目标 Agent 支持的 skills 目录，例如 `<project>/.agents/skills/quark-vfs`。保留 `SKILL.md` 和引用文件，并在安装副本中记录 CLI 与配置文件的绝对路径。安装后从目标工作目录运行 `status` 检查配置和运行条件。目录发现取决于 Agent；目前完成完整宿主验收的是 Codex。其他 Agent 还须具备命令执行、文件和网络权限，并验证官方运行时对其真实上下文的支持；不伪造 Agent 标识。

迁移到另一台机器时，要另外安装 CLI、Node 和官方运行时，填写该机器的绝对路径，并独立完成官方登录与作用域绑定。不要将认证目录或数据库当作 skill 一起分发。

通过复制安装的 skill 不会自动同步更新。新增命令、参数、能力或使用限制时，更新 CLI 后同步 skill 及其引用；仅内部修复、调用契约不变时可只更新 CLI。运行时路径变化时，更新安装副本中的入口说明。共享发布以仓库 `skills/quark-vfs` 为说明源，具体路径由各部署环境填写。
