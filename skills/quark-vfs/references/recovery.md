# 作用域与回收恢复

本参考随 `quark-vfs` skill 安装，说明管理根、marker 和本地任务的恢复边界。

## 管理根丢失

管理根名为 `QuarkVFS-<UUID>`，marker 文件名为 `scope-<UUID>.json`。默认回收目录位于管理根内；正式 manifest、completion event、恢复意图和事件，以及回收 payload 都存放在这里。始终保留管理根及其内容。若配置了独立的 `trash_root_fid`，也必须保留该回收根及其中的正式记录和 payload。

若管理根从网盘消失，先在夸克客户端的原生回收站恢复原目录，再运行：

```sh
quark-vfs --config /absolute/config.toml status --verify-scope
```

QuarkVFS 的 `restore --operation-id ... --entry-id ...` 只恢复 QuarkVFS 回收容器中的条目，不会恢复夸克客户端原生回收站中的目录。若原目录恢复后 FID 已改变，当前绑定无法修复，`bind-scope` 会拒绝覆盖。不要删除本地数据库、自动重绑或新建同名目录掩盖 mismatch。

默认 `status` 只检查本地状态。`--verify-scope` 不修改云文件或绑定；核验可使用本地锁和临时缓存。`--refresh` 只刷新登录状态，两者可以组合。`scope_verification` 为 `not_checked`、`passed`、`mismatch` 或 `unavailable`；只有本次验证明确通过或不匹配时 `live_marker_checked` 才为 true。`unavailable` 表示云端验证未完成，不能据此判定目录被删。只有成功列出管理根但缺少 marker 才能判定 `SCOPE_MARKER_MISSING`；上游读取失败不能证明目录消失。认证错误与 scope 验证错误分别记录。

Marker 验证通过表示当前授权能访问该命名空间，不证明命名空间中的每个文件都可访问，也不提供真实服务端 userID。

## Marker 彻底丢失

若确认 marker 已彻底丢失，必须明确选择建立新作用域并配置新的 account alias，再运行 `init-scope`；工具会生成新的 scope UUID。保留旧数据库、旧任务及远端历史。旧任务和回收记录不会自动接管到新作用域；当前没有旧作用域迁移功能。不要删除旧库或借新建同名管理根恢复旧身份。

## 数据库与任务

数据库遗失不等于没有未完成任务。原数据库缺失时，`status --verify-scope` 无法先通过本地绑定读取 marker。使用已保存的原 scope UUID 和管理根父目录 FID 先执行 `bind-scope --scope-uuid UUID --management-parent-fid FID`；该操作读取并验证原 marker 后建立本地绑定。随后运行 `rebuild-trash` 重建回收索引。它只重建正式远端清单能够证明的记录；缺少 completion observation 的条目应保留为待核对状态。

离线 `jobs` 可以查询旧 account alias 的本地历史，不会核对网盘。`jobs --job-id UUID --resume` 仍要求 scope 验证通过；离线查询不授权恢复任务。写操作结果未知时先核对远端对象，不重复派发。
