# M0 官方核查证据

最终状态（2026-10-13）：M0 能力审计完成，M1 文件接口、回收恢复与新数据库重建后恢复目录均真实通过；本机文件和回收接口已启用。以下保留各阶段原始观察与停止原因，最终汇总见 [验收报告](VERIFICATION.md)。

## 2026-10-13 剩余专项验收结果

- `89490fd4-c18f-4ace-9605-98ab11be954e`：独占目录上传 105 个极小合成文件，完整枚举名称/类型/尺寸一致；首、中、末 3 个样本下载字节与 SHA-256 一致。整个测试目录已移入独立保留容器。
- `6505b73e-e9ed-4b5f-a9cf-4a00a2678b93`：同名建目录保持唯一。文件移动撞名后，服务端将移入文件命名为 `collision(1).md`，原 `collision.md` 保留；两份合成内容经后续只读下载核验一致。测试因最初对名称的严格假设停止，未重复移动或上传，后续步骤从已核对状态继续。
- `9a1f345c-af8e-4044-a2f4-5346224663bf`：合成文件上传下载已完成且内容一致；下载前后及随后只读复查的 `download list` 均返回零条记录。没有猜测 recordId，也没有调用不明确的 resume。完成记录关联未通过；此结果不证明中断中的记录同样不存在。

三个此前停止的测试目录（`9a1f345c`、`7e51eb1a`、`32cc40ab`）已各执行一次受限保留移动，目标枚举、原位置消失、完整 payload 字节和 SHA-256 均再次通过。保留脚本只接受注册样例及其原创建证据，不重复已有移动意图，不重做上传或中断实验。

以上是当前安装版本及账号环境中的样例结果。没有把样例行为升级为跨客户端并发或原子性保证。

- 同一 `6505b73e-e9ed-4b5f-a9cf-4a00a2678b93` 测试在只读核对后继续未执行步骤：重命名内容一致，重命名撞名被拒绝且双方字节保留。中途发现写后目录短暂未包含刚上传文件，稍后只读枚举可见；因此适配加入有上限的写后只读核对，不重新派发写操作。整个测试目录最终移入独立保留容器。
- `9a1f345c-af8e-4044-a2f4-5346224663bf` 的独立短文件覆盖测试：在本轮私有 `short-tail` 目录预置较长合成内容，官方 `--overwrite` 返回成功，但预期 22 字节实际为 122 字节，旧尾部仍在。没有触碰用户本地文件；M1 只用全新空 staging，验证完成后 no-replace 发布。
- `7e51eb1a-6df8-46c2-bcd8-b916443773fa`：8 MiB 合成源已上传，本轮下载进程在 4,194,304 字节时被测试器终止，退出码 -9；下载记录仍无法唯一关联，未调用 resume。该负向结果关闭官方文件内续传路径，不能用伪造 recordId 绕过。

- `32cc40ab-a3ac-4820-823c-ed7f64c4ce9f`：最初的混合上传收到未知逐项 schema 后停止；只读枚举和下载确认可读文件成功，未重发原批次。静态核查发现失败 list 只有 recordId，没有 fileName，解析器改为用官方 upload list 按当前 recordId 精确关联。
- `5b17cbf8-77d5-48eb-94ba-1b593a3fd386`：新独立实验对一个可读文件和一个权限拒绝文件只执行一次混合上传，逐项部分成功与成功文件字节均通过；最终保留移动后因首次枚举未确认而停止；随后只读核对确认原根目录已无测试目录，独立保留容器内只有该测试目录及 31 字节成功文件。未重复移动。
- M1 搜索只读对照：36 字符测试 UUID 不带 parent_fid 时成功；同一 UUID 或 8 字符前缀带父目录均返回上游 `21001`。该码在固定包没有解释，不推断权限或权益原因，也不将失败变为空结果或静默全盘搜索。

## M1 实测补充：移动会改变修改时间

同一 `be61ff07-fe28-4693-8449-40cfd9bb5ddc` 样例的正式回收已完成。首次 restore 在纯预检阶段返回 EXPECTED_CHANGED，没有恢复任务或远端恢复意图。只读核对回收容器的唯一 payload，名称、类型、尺寸与 opaque hash 一致，只有 updated_at 从 `1791857236975` 变为 `1791857459257`。该文件再次下载与已知 22 字节源逐字节、SHA-256 一致。

因此原位置 manifest 保留历史元数据，恢复须另用回收后观测并持久化的元数据。不能通过忽略 modified 来接受并发变化；旧样例只能在已知源摘要实际核验且没有恢复派发的条件下受限接续。

后续已用独立不可变完成记录保存回收后元数据。该样例成功恢复且再次下载内容一致；随后新的文件/目录联合回收、恢复其中一项、新数据库绑定与重建、恢复剩余目录和内容校验全部通过。自动写重试与永久删除均为零。

## 初始静态核查（2026-10-12）

核查日期：2026-10-12。采用项目 `document-writing-style` 写作约定。本轮仅下载、静态阅读公开材料和检查指定安装路径存在性，没有执行官方运行时、读取本机凭证、登录或云端写入。

## 固定基线

| 项目 | 核查结果 |
| --- | --- |
| 官方仓库 | [quark-clouddrive/quarkclouddrive_offical](https://github.com/quark-clouddrive/quarkclouddrive_offical) |
| 当前 main | `cba49a7e703d32607b1376375e3214fd8c63788f`，提交时间 `2026-10-10T13:32:06Z`；GitHub API 元信息核查 |
| 固定包 | [quarkclouddrive-1.0.24.zip](https://pdds.quark.cn/download/stfile/fflliomikfgjfhnhk/quarkclouddrive-1.0.24.zip) |
| Skill / CLI | `1.0.24` / `1.0.24-29460f9`（脚本常量） |
| ZIP SHA-256 | `a4847bb6e4d6bafb21eb28ba0d9b34b643b6ba5aed44d1a25c86c7f5ba3efb8a` |
| 入口 SHA-256 | `7fade9bebb348443ff6f8c184cf5f674c69562de086d9c7d34ff00d134be1143` |

指纹由本轮下载内容计算，是内容定位依据，不是官方数字签名。包暂存 `/tmp/quark-m0-package`，不进入项目源码。

## 一手证据定位

包内 `SKILL.md` 第 89–118 行：公共参数说明；第 120–145 行：每轮安装检查、串行更新及 notice。在线 [固定提交 Skill](https://github.com/quark-clouddrive/quarkclouddrive_offical/blob/cba49a7e703d32607b1376375e3214fd8c63788f/skills/quarkclouddrive/SKILL.md) 同样明确完整 Skill 的参数和更新要求。包内 `references/auth.md` 定义 `login`、`get-user-info`，没有 `auth status`。

包内公共参数说明原文：

> 此为 agent 行为要求，不影响 CLI 执行逻辑。

同段规定原始提问逐字传递、仅用于质量追踪，确实无法获取时可省略。该要求明确作用于官方 Skill 中 Agent 的调用行为；未找到独立结构化包装接口的支持说明、豁免或禁止。后续需要确认适用范围，而非通过省略可执行参数推定支持。

包内脚本压缩在约 116 行，以符号定位复查：`PR` 注册 resolve-agent；`sy` 注册全部命令及 preAction/postAction；`ny` 添加公共参数；`tw` 检测 Codex；`Ha/za/Du` 定位认证配置；`Hs` 定位命令状态；`loadPersistedConfig/migrateLegacyConfig` 读取和迁移旧配置；`lc` 注册 SDK storage；`cl` flush tracing。resolve-agent action 只写纯文本，但全局钩子可读取和迁移配置，因此不承诺零写入或零网络。

`ey/$v/Bv/XR` 定义 browse：默认发布 result 五条预览后 artifact，artifact.data 包含 `file_path/count/format`；`--task-dir` 分支通过 result.data.file_path 返回完整 JSONL。上传成功 list.data 包含 `recordId/fileId/fileName/fileSize/instantUpload`；失败 list 含 recordId 与非零 code。result 成功 code 为 0，失败使用数字 code、msg、action、data，可附 notice。退出码、result 和逐项状态共同判定任务结果。

## 安装发现与尚待验证

定向查找 `~/.agents/skills`、`~/.codex/skills` 下名称含 quark 的目录未发现安装；随后仅检查 `~/.quarkclouddrive` 和指定入口存在性，目录不存在。没有读取任何认证文件。

待验证项：固定可写安装落点；真实 Codex→包装 CLI→官方 CLI 识别；MCP 上下文；独立 CLI 集成约定；用户授权和 scope；文档下载、处理、上传往返；分页、身份、碰撞、部分成功、下载关联与续传专项样例。静态阶段可继续实现保守解析与诊断，真实业务能力须以相应验收结果启用。

## 2026-10-13：安装与登录链路静态补充

以下仍来自同一固定包，没有执行安装或登录。`scripts/install.sh` 第 22–29 行以自身位置确定包根和其父目录的固定 `temp`，没有 prefix 参数。复制完整包至固定目录后，该目录内脚本在技术上以它为安装目标；独立目录的官方支持约定仍未知。脚本会请求 `/agent/v1/skill_config` 下载动态版本并覆盖 scripts、references，退出清理父目录的 `temp`；执行前须确认该路径为专用临时位置。第 46–59 行还尝试删除 `/usr/local/bin/quarkclouddrive`；Node 不足 16 时可能执行包管理器安装与覆盖链接。不能将 install 称为仅检查版本的只读命令。

固定脚本登录链为 `hS → dS → qT → fS → pS`：浏览器模式获取服务端 authorizePageUrl/pageCode，在 macOS 用 `open` 打开服务端页面，redirect_uri 为空；每两秒调用 getAacByPageCode，轮询截止为 60 秒，单次网络等待可延长实际时长。该链路没有本地 callback server 或端口。包内 auth.md 的本地授权服务器描述与实现不一致，真实验收采用当前脚本行为。

准确业务入口为 `node <绝对入口> login`；浏览器正常完成时无需用户复制 code。备选 `login --token <agent_auth_code>` 直接交换用户提供的授权码。公共 session 参数仍遵循前述适用范围记录。浏览器打开失败、取消或轮询超时的最终 msg 可带 `[前往授权]URL`，启动时没有独立结构化 URL 事件；包装层不能虚构上游 callback 成功或 URL 字段。

成功 login 的 result.data 为 `userId/status/msg/skill_vip_url?`，不输出 accessToken 或 refreshToken。`get-user-info` 成功 action 为 `get-user-info`、code 为 0，data 为 `vipInfo` 与 `userInfo`；两次接口都成功才发布结果，userInfo 仅含 nickname。建议只白名单输出认证状态、已知成功 status 和实际 notice；原始 stdout/stderr 与 verbose 网络日志不进入长期诊断包。网络 URL 或异常文本可能包含授权页设备参数、授权码、签名或 token，错误处理不得无条件透传。

固定 CLI 本地认证码：缺少 token 为 -103，授权过期为 -107，已有授权有效为 -118；服务端 errno 可透传，因此这些不是认证错误全集。普通错误保持认证未知，不能一概判断为未授权。现有授权 -118 分支先通过服务端账号校验，不应自动重复 login。

## 2026-10-13 本机安装与环境识别

官方固定包已安装到用户私有数据目录的 `quark-vfs/official/1.0.24`，包装层状态、缓存和配置分别位于用户数据、缓存和配置目录。安装采用已核验包的完整复制，没有执行会更改全局命令的安装脚本。入口 SHA-256 与上述基线一致；此事实不改变上游更新约定仍待确认的状态。

用户在获知官方初始化涉及状态与遥测后提升权限并明确允许继续。真实 `quark-vfs doctor --probe` 成功，子进程继承当前环境，官方输出通过严格身份解析，识别结果为 `codex`。这验证了本机 Codex → 包装 CLI → 官方 CLI 的环境识别链；MCP 路径尚未验证。

## 2026-10-13：初始化遥测范围静态定位

固定包内 tracing 默认 cluster 为 quark、sampleRate 为 1。内置目标映射指向 `https://px.wpk.quark.cn`，上传路径 `/api/v1/jstrace/upload`，远端配置路径 `/api/v1/otelconfig`。这是源码目标定位，本轮研究没有抓取真实遥测请求，不能据此保证动态配置与实际网络只使用这些路径。

`cli_command_wild` 和 `auth_trace_wild` 记录命令、版本、结果、耗时、Agent 标识、机器标识、平台/架构、工作目录、认证配置路径及读写错误；已有授权时可包含 userId 与服务端 deviceId。识别失败分支额外记录环境变量名称清单，不读取该清单对应的变量值。省略 session-input 不会关闭这些诊断；用户提问仅在实际传入时设置 raw_query。

认证 token 属性经 `Ku` 按字段名匹配 access/refresh token 后调用 `Nr`；`Nr` 为 MD5 摘要前六字符。主命令的 token 维度同样调用 `Nr`。因此静态证据支持这些属性发送短 token 指纹，不支持将它们描述为明文 token。错误文本另有 Bearer、URL token/code 和部分 JSON 字段脱敏逻辑；这不能证明所有异常文本均已脱敏。包装层继续有界消费输出、白名单发布结果、避免 verbose 与原始日志落盘。

用户知悉初始化外发风险后的实际环境识别结果由上方本机验收段记录；本段只追加静态证据，不改写真实授权或运行结果。


## 2026-10-13：真实受限文档往返与响应身份边界

主代理真实执行验证 ff1af5fc-e842-47fb-aac7-e595ff399db0，返回 ok=true，source_round_trip、processed_round_trip、moved_to_verified_trash 完成；源文件 103 字节，两个 payload 在移动前后均通过字节、尺寸和可信本地 SHA-256 验证。automatic_retries=0，resumed_source_upload=true，上游 notices=[]。

首轮未执行云写；第二轮成功源上传后停止。恢复前只读核对 journal 和协议，不重复源上传。根列表条目 parent_fid 不等于请求 0，上传/建目录返回 ID 与枚举完整 FID 也不能直接相等；静态 SDK 直接透传这些不同接口字段，没有客户端转换保证。范围使用精确请求回显、本调用 task-dir、完整条数与分页终态；受限关联使用独占目录唯一名称、类型、尺寸候选，加下载后精确源内容核验。未猜加密算法、未使用私有 HTTP。

本轮测试回收位置为 /QuarkVFS-M0-Trash-ff1af5fc-e842-47fb-aac7-e595ff399db0/QuarkVFS-M0-ff1af5fc-e842-47fb-aac7-e595ff399db0，未写远端恢复 manifest，只有本地 journal 与测试容器。完整 M0/M1、超过 100 项枚举、碰撞/并发、正式回收恢复、通用续传仍未验收；scope 未知，MCP、archive_replace、download_resume_verified 保持 false。最终工程检查由主代理另行复验。
