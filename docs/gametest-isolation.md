> 更新时间：2026-10-22 15:18 +08:00（Asia/Shanghai）

# GameTest 持久化兼容性与隔离边界

## 修改原因与迁移决定

完整恢复测试会经过临时目录、租约、居民记忆、照片和启动配置清理。直接沿用删除分支会丢失失败证据；仅绑定 World/Agent 保存根，也不能隔离 Host 重新创建的服务或先于场景初始化的音频 autoload。另外，离场自动保存可能发布更新版本：读档受理时选中的 revision 不一定等于最终恢复的 revision，完成回执必须以后者为准。

本次决定为 **`no_migration`**，不是“迁移测试已通过”。相对本 PR 的 `origin/main` 对比，以下格式定义文件没有修改；没有增加迁移 ID、重写历史样本或升级已发布存档版本：

| 格式 | 当前源码定义 | 本次变化 |
|---|---|---|
| 世界 snapshot | [TownWorldSaveCodec.gd](../game/world/runtime/persistence/TownWorldSaveCodec.gd) 使用 [TownSaveSchemaRegistry.gd](../game/world/presentation/session/TownSaveSchemaRegistry.gd) 的 World 版本 2 | 无 |
| 会话 manifest / startup profile | 同一版本注册表分别为 3 / 2；[TownSessionSaveManifest.gd](../game/world/presentation/session/TownSessionSaveManifest.gd) 的 manifest 字段定义不变 | 无格式变化 |
| Agent / 居民 envelope | [AgentSaveStore.gd](../game/agent/lifecycle/AgentSaveStore.gd) 为 3；[ResidentStateCodec.gd](../game/agent/lifecycle/ResidentStateCodec.gd) 为 2 | 无 |
| 音频 cfg | [TownAudioController.gd](../game/audio/TownAudioController.gd) 的既有 section/key 写法 | 不改格式，仅隔离路径及失败处理 |

[开局样本](../game/tests/fixtures/town_world_opening.json)用于确定性 World 夹具；[历史样本清单](../game/tests/fixtures/historical_saves/catalog.json)仅作兼容性参照。本声明不表示本轮重新执行过完整历史迁移矩阵。

## 做了什么

| 接入位置 | 行为与保护 |
|---|---|
| [AgentFileSystem.gd](../game/agent/AgentFileSystem.gd) | 进程内一次性显式登记保留根；仅允许 `owned/` 后代移动到唯一 `retained/` 目标，记录实际 PID、来源、目标、文件清单和 SHA-256。拒绝越界、链接和目标冲突，移动后复核；失败不冒充成功。 |
| AgentSystem、居民/化身记忆 Store 和 EvidenceQueue | 测试保留失败向上传播；不能在失败时静默丢失待恢复事务或旧上下文。默认正式清理路径仍保留。 |
| [TownSessionSaveStore.gd](../game/world/presentation/session/TownSessionSaveStore.gd)、[TownStartupSaveCatalog.gd](../game/world/presentation/session/TownStartupSaveCatalog.gd)、[TownConversationPhotoStore.gd](../game/world/integration/TownConversationPhotoStore.gd) | 将已授权测试清理入口接入相同保留策略；校验根路径、租约归属和失败结果。profile 已发布但旧备份留证失败时明确报告部分结果，不当作完整成功。 |
| [GameFlowHost.gd](../game/world/presentation/game_flow/GameFlowHost.gd) | 测试存储注入同时要求既有 `--gametest-isolated-test` 和登记根；内部重建 Gateway/保存服务也绑定隔离路径，不绕过真实健康检查。完成回执从实际激活的恢复上下文同步 revision，不修改选档、离场保存或存档字节格式。 |
| [TownAudioController.gd](../game/audio/TownAudioController.gd) | 隔离启动时，autoload 未绑定前不回退读取正式 `user://audio_settings.cfg`；绑定同样要求隔离参数与登记根。坏/不可读测试 cfg 保留，读取失败锁存后禁止默认值覆盖；未绑定保存返回失败。 |

保留模式由登记启用，不是普通用户开关；Host/音频的入口还要求隔离启动参数。正常启动不加该参数、不登记测试保留根，仍使用原正式路径及原物理清理分支。**默认行为保持的源码审查，不等于已动态验证默认删除分支。**

## 测试与已有结果

为遵守现有 World 架构棘轮，已获准撤销本 PR 新增的世代 getter。只有显式开启的 Bridge 测试适配层及既有测试投影读取同一个真实 `_runtime_generation` 字段；必须为正整数，否则不发布就绪状态、不受理命令、不伪造状态事件，活动命令以已知旧身份失败结束。此实现把内部字段名的耦合成本留在测试适配层，不是向游戏业务开放反射接口。九个已注入上下文 `Callable.call()` 在现有白名单逐项说明用途，不扩大动态方法派发、扫描排除或任何 guard 基线。新增合同负例与真实同对象恢复回归仍须实际执行，不能仅凭补丁宣称通过。

- [game_test_bridge_test.gd](../game/tests/game_test_bridge_test.gd) 是本 PR 新增的 Bridge 合同回归，覆盖异步受理/完成、幂等回执、失败和世界换代等边界。其 `FakeSessionFlowHost` 只验证协议合同，不能证明真实磁盘保存或 Host 场景恢复。
- 实际存储/保留、非空任务/日志/记忆和 Host 路线分别见 [test_bridge_read_api.py](../game/test_bridge/phase2_tests/test_bridge_read_api.py)、[test_bridge_command_api.py](../game/test_bridge/phase2_tests/test_bridge_command_api.py)；实际损坏 World/未来 Agent 拒绝与音频失败保护见 [test_bridge_host_load_failures.py](../game/test_bridge/phase2_tests/test_bridge_host_load_failures.py)。不以测试替身回执代替这些真实链路。
- 本机完整回归索引：[closeout-audio-full-20261020-a1/closeout-audit.json](../game/test_bridge/artifacts/closeout-audio-full-20261020-a1/closeout-audit.json)。身份为干净 `f63bac2edd88d7e115a4f05a5b1d57ee18433b6c`：pytest 74 通过、零失败/错误/跳过，Godot 685 条检查，Newman 5 请求/14 断言，Pylint 10/10。检查条数不是额外 pytest 用例；该历史本机结果不能继承为当前最终 SHA 的云端通过。
- P025 真实红灯原件保留在本机 `game/test_bridge/artifacts/closeout-host-flow-20261013-a2/` 及相邻 `-temp`、`-junit.xml`：实际恢复版本与旧完成回执不一致；后续 Host 回归核对实际 Agent/manifest/HTTP/WS 版本一致。旧 Python 原文本未完整归档等历史证据边界继续保留。
- `083e352` 的首个正式只读压测轮次 [formal-1client-r1](../game/test_bridge/artifacts/jmeter-formal-matrix-20261022-a1/formal-1client-r1/measurement-summary.json) 有完整 300 秒窗口、300 请求零失败、成功 1 RPS、p95/p99 为 8/9ms，恢复 10/10。第二轮中断且没有最终结果；整批 15 次正式测量及稳定性尚未完成，不能称整体压测通过。

上述 `artifacts/` 是本机忽略目录，不随 Git 仓库或本 PR 分发。它们是可核查的本地原件定位，不是承诺可公开下载的 CI 附件；远端可用证据、最终同 SHA 三次独立 CI 与链接核验仍需收口。本文和声明本身也不等于 guard 执行通过。

## 风险、退出与恢复办法

1. 原物理递归删除没有执行/验收；保留式移动不能冒充其验证。Profile 每个 flush/备份回滚分支、全部并发与外部 I/O 故障亦未穷举。
2. 路径/链接检查不是对同权限恶意进程的文件系统沙箱，仍有检查与使用之间的竞态边界。保留失败应停止相关操作、检查返回错误与 `retention.jsonl`，不得删除现场或放宽断言。
3. `TownAudioDisplaySettingsService` 是独立路径，不在音频 autoload 保护结论内；当前 GUI 夹具不创建该服务、不进入设置/全屏。真实读档、玩家交互和可视错误提示 UI 尚需补验，无头恢复不是图形验收。
4. 保留登记是进程内单向状态；不要在同一进程撤销后转向正式根。停止已确认归属的测试子进程，保留整个隔离根；后续使用新的、另行授权的隔离产物根重新验证，不复用或覆盖失败样本。正常游戏启动不以恢复测试产物替换正式存档。
5. 早期 JMeter 原生报告原型曾自动清理隔离派生缓存，已作为执行偏差保留记录；原 XML/CSV 哈希未变但无逐文件删除清单。因此不能声称“全过程零删除/全文件系统完全隔离”。后续报告配置避开对应清理消费者，不抹去历史偏差。
6. 首次新 GUI 夹具因生成脚本解析失败退出；启动命令遗漏 `--log-file`，Godot 尝试写默认用户日志时被拒绝。没有 UI 通过结果，也不能据此声称未发生任何默认路径访问。该根和日志保留，生成器已改为明确的隔离日志路径，修复仍需解析及真实 UI 复验。

阶段二还需按原标准补完其余压力测量、实际 UI、适用 guard、最终 CI 与总验收。上游是否合并 PR 不作为这些本机验证的完成条件。
