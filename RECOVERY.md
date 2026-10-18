# 任务与回收恢复

包装层验证项目命名空间绑定后执行文件操作；用户明确授权的其它目录仍可通过结构化引用访问。账号 alias 是本地作用域名；云端不可变 marker 下载校验用于证明当前授权仍能访问该命名空间，不表示已获得真实服务端 userId。已有绑定验证失败时停止，不能自动改绑。

## 管理目录或 marker 丢失

项目管理根目录名为 `QuarkVFS-<UUID>`，其中包含对应的 `scope-<UUID>.json` marker。默认回收目录位于该管理根内；正式 manifest、completion event、恢复意图与事件，以及回收 payload 都保存在其中。保留管理根及其内容。若配置了独立 `trash_root_fid`，也必须保留该回收根及其正式记录和 payload。

若管理根目录从云端消失，先在夸克客户端的原生回收站恢复原目录，再运行 `status --verify-scope` 检查 marker。QuarkVFS 的 `restore` 只恢复 QuarkVFS 回收容器中的条目，不会恢复夸克客户端原生回收站中的管理根。若原目录恢复后 FID 改变，当前绑定无法修复；`bind-scope` 会拒绝覆盖已有绑定。不要删除本地数据库、自动重新绑定或新建同名目录来掩盖不匹配。

`status --verify-scope` 不修改云文件或绑定，并可使用本地锁和临时缓存；`--refresh` 则只执行登录账号刷新，两者可组合。`scope_verification` 为 `not_checked`、`passed`、`mismatch` 或 `unavailable`；只有本次验证明确通过或不匹配时 `live_marker_checked` 才为 true。`unavailable` 表示本次没有完成云端验证，不能据此认定 marker 已删除。上游读取失败也不能证明目录被删除；只有成功列出管理根但未发现 marker，才报告 `SCOPE_MARKER_MISSING`。通过 marker 验证只证明当前授权能访问该命名空间，不证明所有文件均可访问，也不是真实服务端 userID。

若 marker 已彻底丢失，必须明确选择建立新作用域并配置新的 alias，再运行 `init-scope`；工具生成新的 scope UUID。保留旧数据库、旧任务和远端历史；旧任务和回收记录不会自动接管到新作用域。当前没有迁移旧作用域任务或回收记录的功能，不要把重建同名目录当作迁移。

即使离线，`jobs` 仍可查询旧 account alias 的本地历史，但不会核对云端状态。`jobs --resume` 仍要求 scope 验证通过；缺少数据库也不表示没有未完成任务。

## 任务中断

`jobs` 查看 SQLite 中逐项状态；成功项保留，不重复执行。未派发项目与未知副作用分开记录。上传、移动、重命名响应不确定时先核对实际对象，不能重跑整批。下载重新开始只可在 fresh 来源校验和新目标检查后进行；它不表示官方文件内断点续传门禁已通过。

M0 `verify-workflow --resume UUID` 仅恢复其记录的成功源上传 checkpoint；它不用于通用 M1 任务。两种记录不能互换。

## 回收顺序

配置 `trash_enabled` 默认关闭。启用后，`trash --ref JSON` 使用显式 FileRef，先新鲜定位对象并保存 SQLite 意图。每个回收操作及源对象都创建独占 UUID 子目录，目录源整体移动以保留层级。

包装层把 schema、account alias、scope UUID、原 FID、已知父目录、名称、类型、尺寸与变化元数据保存到不可变 manifest。上传并下载精确验证该清单后才移动源对象。成功移动后的新鲜 payload 身份与元数据另冻结到带 account、scope、operation 绑定的不可变 completion event，并验证读回；不覆盖原 manifest。恢复用回收后版本检查变化，原清单中的修改时间只保留原位置历史。文件内容验证等级以实际 transfer receipt 为准，opaque content_hash 只作变化线索。

`restore --operation-id UUID --entry-id UUID` 重新检查原父 FID；无法确认时使用明确 `--destination-fid`，或用 `--destination` JSON 引用提供可靠父目录线索，不按旧路径猜位置。默认 fail 拒绝碰撞，显式 keep_both 保留双方。目的地、元数据及冲突纯预检失败时没有派发恢复意图或移动，可以修正明确目的地或冲突策略后执行。预检通过后先上传并读回不可变恢复意图，移动后另写不可变恢复事件并保留历史；未知恢复或事件待确认时禁止重复移动，已恢复条目不会因旧容器被重新填充而复活。不永久删除容器或清单。

## 本地数据库遗失

使用已保存的 scope UUID 和明确管理父目录，通过 `bind-scope --scope-uuid UUID --management-parent-fid FID` 只枚举该父目录定位确定的项目 UUID 目录，再读取原 marker 建立新本地绑定；可选明确 FID 是额外核对条件，不需要旧 FID 继续有效。目录名只定位候选，绑定仍需 marker 完整字段和字节核验；无法证明 marker 对应当前配置时停止。随后 `rebuild-trash` 只扫描配置的回收根目录。缺少可验证的回收后 completion 观察时记 needs_reconciliation，不通过忽略修改时间接受对象。

重建严格验证 manifest 版本、字段白名单、account 与 scope UUID，使用新鲜 UUID 子目录枚举核对 payload。清单中的字符串是数据，不执行其中的命令，不按其中本地路径写文件。当前 opaque FID 使用新鲜观察值，保留历史 ID 的来源。

payload 缺失或无法唯一关联时记 needs_reconciliation，并保留其他可恢复条目。重建优先核对远端恢复意图与事件；本地未知恢复尝试不会被重建覆盖，数据库遗失后未完成的远端恢复意图也阻止再次移动。空容器不能证明删除或恢复成功；原对象可能已被其他客户端移动。核对当前源、目标及远端事件后再决定下一步。未知移动不自动重复。

正式远端 manifest 回收、恢复，以及新数据库重建后恢复目录已有验收记录；本轮新增的作用域验证和目录丢失恢复说明不代表已完成真实网盘验收。此前 M0 测试回收容器没有远端 manifest，不能通过 `rebuild-trash` 将它冒充正式恢复记录。
