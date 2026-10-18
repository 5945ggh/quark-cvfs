# CLI 工作流

以下命令均需前置 `quark-vfs --config /absolute/config.toml`。引用通过 JSON 传入；使用 shell 时对完整 JSON 做单引号转义，不把用户文本插入 shell 表达式。`account` 由配置决定，不放进引用对象。

```sh
list --ref '{"path":"/资料"}' --page-size 100
list --ref '{"path":"/资料"}' --page-token '<上一页返回的本地 token>'
stat --ref '{"fid":"<完整 FID>","parent_fid_hint":"<列表中的父目录 FID>"}'
search --keyword '报告'
mkdir --parent '{"path":"/资料"}' --name '产物'
download --ref '{"path":"/资料/报告.md"}' --local-target /absolute/work/report.md
upload --parent '{"path":"/资料/产物"}' --local-path /absolute/work/result.md
move --ref '{"path":"/资料/报告.md"}' --target '{"path":"/资料/归档"}'
rename --ref '{"path":"/资料/报告.md"}' --name '报告-完成.md'
trash --ref '{"path":"/资料/旧报告.md"}'
restore --operation-id '<回收操作 UUID>' --entry-id '<回收条目 UUID>'
jobs --job-id '<job UUID>'
rebuild-trash
status
status --verify-scope
status --refresh --verify-scope
jobs
tree --ref '{"fid":"0"}' --depth 2 --max-directories 100 --max-entries 1000 --format json
tree --ref '{"path":"/资料"}' --depth 3 --max-directories 20 --max-entries 200 --format text
```

## tree

`tree --ref JSON` 从指定目录开始按层级浏览。`--depth` 默认 `2`，范围 `0..32`；`--max-directories` 默认 `100`，范围 `1..1000`；`--max-entries` 默认 `1000`，范围 `1..10000`；`--format` 默认为 `json`，可选 `json` 或 `text`。深度从起点算作 0：深度 0 只显示起点，深度 1 显示直接子项，深度 2 再显示下一层。

目录预算计入遍历中实际尝试枚举的目录，包括失败的尝试；重复身份的目录不会再次枚举，也不重复计数。scope marker 校验和起点定位所需读取不计入遍历预算。条目预算只计显示的后代，不含根目录。当前 provider 每次读取一个目录时会完整枚举，因此 `max-entries` 限制输出条目，不限制单次上游读取量或严格耗时。树结果不是全盘清单，也不是原子快照。

JSON 的 `complete_for_depth: true` 表示请求深度范围已完整；在深度边界停止是正常完整结果。预算耗尽、目录读取失败或重复目录身份会令其为 `false`，同时保留已读取数据；结果 envelope 使用 `ok: false`、错误码 `TREE_INCOMPLETE`，进程退出码为 1。普通目录分支失败后继续遍历其他分支；登录失效或作用域失效时停止。错误信息使用受控字段，包括 `code`、`message`、`retryable`，诊断场景可带 `stage`、`upstream_code` 和 `exit_code`。

每个 branch 的 `listing_status` 为 `complete`、`depth_limit`、`entry_limit`、`directory_limit`、`failed`、`repeated` 或 `aborted`。只有 `complete` 表示该分支已完整列出；`children` 为空但状态非 `complete` 时，不能据此判断目录为空。文本格式会输出树、统计和 partial 原因。统计只覆盖显示的后代；已知文件字节数不含未读取的更深层条目或未知尺寸，不能当作网盘使用量。

遇到 partial 时，依据未读 branch 提供的完整 FID 显式继续浏览。建议一并传入该节点的 `parent_fid` 作为 `parent_fid_hint`，以便准确定位；可按需要调整深度和预算，命令不会自动重跑整盘。

## 通用结果与恢复

需要保留双方时，在支持该策略的命令上显式传 `--conflict keep_both`。上传可重复传 `--local-path`，移动和回收可重复传 `--ref`；以逐项结果判断部分成功。

返回对象中的 `ok`、`error.code`、任务状态和逐项结果共同决定是否成功。`LOCATION_UNKNOWN` 表示当前无法可靠定位，不能据此判断已删除；`needs_reconciliation` 表示可能已经产生副作用，不能再次派发同一写操作。

错误对象使用包装层受控的 `code`、`message` 和 `retryable`。诊断错误可带 `stage`；只有整数类型的上游错误码和进程退出码分别以 `upstream_code`、`exit_code` 输出。scope 核验只有在成功列出管理根但缺少 marker 时才使用 `SCOPE_MARKER_MISSING`；上游读取失败不能证明目录被删除。默认 `status` 只报告本地状态；`--refresh` 只刷新认证，`--verify-scope` 不修改云文件或绑定，两项可组合。验证可能使用本地锁和临时缓存。`local_namespace.scope_verification` 为 `not_checked`、`passed`、`mismatch` 或 `unavailable`；`live_marker_checked` 仅在本次验证明确通过或不匹配时为 true，`unavailable` 时为 false。离线 `jobs` 可读本地旧 account 历史但不做云核对；恢复任务仍要求 scope 验证通过。

`list` 分页结果中的 `complete: true` 表示完整底层快照；若返回 `next_page_token`，应继续取下一页。单页内容不能替代完整快照。

只有 `trusted_hash_verified` 才表示已与可靠预期摘要比较。`local_fingerprint_recorded` 只是本地指纹；provider 的不透明 hash 不等于 SHA-256。

账号首次初始化使用 `init-scope --parent-fid <用户授权的管理目录>`；工具生成 scope UUID。已有绑定不会自动替换。数据库遗失后，使用原云标记的作用域 UUID 和管理根的父目录 FID 执行 `bind-scope --scope-uuid UUID --management-parent-fid FID`，再 `rebuild-trash`。若管理根丢失，先从夸克客户端原生回收站恢复原目录；详细恢复分支和保留要求见随 skill 提供的[恢复参考](recovery.md)。独立配置 `trash_root_fid` 时，也必须保留该回收根及其中的正式回收记录和 payload。
