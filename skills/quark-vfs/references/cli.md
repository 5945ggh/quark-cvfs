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
```

需要保留双方时，在支持该策略的命令上显式传 `--conflict keep_both`。上传可重复传 `--local-path`，移动和回收可重复传 `--ref`；以逐项结果判断部分成功。

返回对象中的 `ok`、`error.code`、任务状态和逐项结果共同决定是否成功。`LOCATION_UNKNOWN` 表示当前无法可靠定位，不能据此判断已删除；`needs_reconciliation` 表示可能已经产生副作用，不能再次派发同一写操作。

只有 `trusted_hash_verified` 才表示已与可靠预期摘要比较。`local_fingerprint_recorded` 只是本地指纹；provider 的不透明 hash 不等于 SHA-256。

账号首次初始化使用 `init-scope --parent-fid <用户授权的管理目录>`；已有绑定不会自动替换。数据库遗失后，使用原云标记的作用域 UUID 和管理根的父目录 FID 执行 `bind-scope --scope-uuid UUID --management-parent-fid FID`，再 `rebuild-trash`。先阅读项目 `RECOVERY.md` 和当前命令帮助，不凭记忆补齐这些标识。
