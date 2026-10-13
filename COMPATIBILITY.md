# 官方运行时兼容边界

本轮静态核查通过版本与协议定位，真实 Codex 识别、浏览器授权和账号刷新已通过；授权 scope 保持未知，受限文档往返已通过，完整文件语义专项验收尚未完成。独立结构化 CLI 的官方支持范围仍未知，当前没有据此认定禁止或宣称已获支持。

包装层本轮只在本机 macOS、Python 3.12 验收。Artifact 安全读取依赖 `O_NOFOLLOW` 与 `os.open(dir_fd=...)`，平台缺少支持时返回 `PATH_CHECK_UNAVAILABLE`；其他平台尚未验收。

## 版本与安装

基线为官方仓库提交 `cba49a7e703d32607b1376375e3214fd8c63788f`，Skill 1.0.24、CLI `1.0.24-29460f9`。入口及包指纹见 [证据记录](docs/m0-evidence.md)。初次定向检查未发现已有安装；随后固定包安装至 `~/.local/share/quark-vfs/official/1.0.24`，入口与包 SHA-256 未改变。

完整 Skill 文档要求安装在 Agent 全局 skills 目录；仓库 README 的 `~/.quarkclouddrive` 描述与包内安装约定不同。独立包装运行时位置应使用用户选定的固定私有目录，记录绝对入口，避免仓库、临时目录和同步目录。本轮采用上述私有固定目录；未运行安装脚本，因此尚未验证官方动态升级流程。

正常 `scripts/quark-drive.cjs` 布局以包根为状态基准。认证首选 `包根/codex/config.json`；包根不可写时回退 `cwd/.quarkclouddrive/codex/config.json`。命令状态首选 `包根/codex/<command>/storage.json`，不可写回退 cwd 下对应目录。启动前检查包根及实际状态目录可写，使用专用私有 cwd，阻止状态落入项目。包装层不读取凭证。

## 公共参数与更新

完整官方 Skill 明确要求每轮输入后首次 CLI 调用前执行 `bash scripts/install.sh`，同轮复用结果，安装失败停止，安装更新与业务操作串行。完整 Skill 的固定版本豁免未找到；本项目不能把固定静态基线称为官方允许的长期冻结版本。

完整 Skill 要求 `--session-input` 传用户逐字原始提问，用于质量追踪；确实无法获取原始提问时可省略。`--session-id` 为 Unix 秒时间戳加六位随机字母数字，同一对话复用。CLI 解析器接受缺失这两个参数，技术可执行性不能证明独立集成的支持约定。

独立结构化接口与完整对话 Skill 的适用关系待上游确认。包装层默认只发送文件操作字段，不自行收集对话，不编造 session 文本。若实际采用完整 Skill 模式，应先向用户明确原始提问会发送给夸克并取得该数据范围的选择。最终非空 `result.notice.text` 应保留供调用方原样展示。

## 诊断与真实上下文

准确入口为 `node /绝对安装目录/scripts/quark-drive.cjs resolve-agent`，成功 action 写 `QK_AGENT_ID=codex` 纯文本。其全局钩子仍初始化追踪、读取官方配置，并可能迁移旧配置；它不是零副作用检查。本轮真实 `doctor --probe` 已严格解析为 codex。

认证探活命令为 `get-user-info`，需要登录且请求服务端；不存在 `auth status` 子命令。`login` 会打开浏览器并阻塞等待授权，另支持 `login --token <授权码>`。本轮 `auth login` 浏览器授权返回 confirmed，`status --refresh` 返回 authenticated；未读取凭证，scope 未知。包装层等待上限为 180 秒；固定版本上游轮询服务端 pageCode，截止为 60 秒，不启动 localhost callback。

子进程继承真实 Codex 环境，禁止设置虚假身份。本轮未发送 session-input；独立集成适用约定仍未知。普通无 Agent 环境会失败；MCP 继承路径须独立验证。CLI 输出消费与 MCP stdout 隔离，MCP 未通过时保留 CLI 工作流。

## 真实枚举与响应 ID 边界

根目录请求使用字符串 0，但实测条目 parent_fid 是上游 opaque 元数据，不能要求它与请求字符串相等。枚举范围绑定本次请求、精确 result.parent_fid、本调用 task-dir、完整 count 与 hasMore=false，另保留条目原始字段。

上传任务 fileId、建目录响应 fid 与 fresh browse 的完整 FID 来自不同接口，实测不能直接按整串或末尾指纹判等。包装层不猜加密规则，不调用私有 HTTP；受限样例只在本轮独占新目录中选唯一名称、类型、尺寸候选，使用 fresh 完整 FID 下载并校验源字节与 SHA-256。原始响应 ID 分来源保存在本地记录中。移动结果回显输入，需目标 fresh 枚举和内容检查确认落位。
