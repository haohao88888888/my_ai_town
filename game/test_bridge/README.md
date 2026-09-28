> 更新时间：2026-09-28 14:20 +08:00（Asia/Shanghai）

# GameTestBridge：阶段 1 外部测试桥

这是本项目新增的外部测试接入层，不是原游戏内部 NPC Agent。默认只读；额外开启命令模式后支持速度设置、真实玩家移动、正式会话存读档和执行结果查询；`/events` 提供最小 WebSocket 语义事件流。

## 1. 开启游戏和接口

先正常保存并退出正在运行的游戏，避免两份游戏同时使用同一存档。在 PowerShell 中执行：

```powershell
& 'D:\Godot\Godot_v4.7.2-stable_win64_console.exe' --path 'D:\ChenDian\CeKaiAndAI\upstream\game' -- --gametest-bridge
```

看到 `GAMETEST_BRIDGE http://127.0.0.1:18765 (read-only)` 表示监听成功。随后在游戏中手动加载原存档。这个命令不修改环境变量、不自动读档、不自动删除或覆盖存档。

普通 F5 启动如果没有传入测试开关，不会开启接口。需要另一个端口时在命令最后加 `--gametest-port=18766`；非法端口或占用端口会报告错误，不会偷偷换端口。

## 2. 在另一个 PowerShell 窗口查询

```powershell
Invoke-RestMethod 'http://127.0.0.1:18765/health'
$snapshot = Invoke-RestMethod 'http://127.0.0.1:18765/world/state'
$snapshot.data | ConvertTo-Json -Depth 8
$residentId = $snapshot.data.residents[0].residentId
Invoke-RestMethod "http://127.0.0.1:18765/residents/$residentId" | ConvertTo-Json -Depth 8
```

- `/health`：HTTP 200 表示桥正常；`data.worldReady` 才表示世界是否已启动。主菜单中它应为 `false`。
- `/world/state`：时间、暂停状态、倍率、居民 ID/姓名、玩家化身公开状态、会话和版本。主菜单返回 HTTP 503 `WORLD_NOT_READY`。
- `/residents/{id}`：仅接受居民稳定 ID，不能用显示姓名代替；返回姓名、地点、空间、坐标、公开行为和在场状态。不存在的 ID 返回 HTTP 404。

JSON 坐标格式为 `[x, y]`，不是 Godot 内部 `Vector2`。不返回记忆、密钥、模型设置、完整动作负载或可变对象引用。

## 3. 协议边界

`protocol.schema.json` 是响应协议 v0.1 的 JSON Schema（2020-12）。所有响应均包含 `protocolVersion`、唯一 `requestId`、`ok`、`data` 和 `error`。

状态响应另有 `sessionId`、`worldGeneration`、`stateVersion`、UTC `capturedAt`。`worldGeneration` 是不透明字符串，组合桥实例随机标识、World 实例和世界内部代次；不能解析为业务时间。同一 World 对象重新启动也会换代次。

本版的 HTTP 子集刻意收窄：

- 只监听 `127.0.0.1`，接受 HTTP/1.1 GET；命令模式另外开放指定 POST 路由，一次连接只处理一个请求并关闭。
- Host 仅允许 `127.0.0.1:端口` 或 `localhost:端口`；拒绝带 Origin 的请求，不开 CORS。
- GET 不支持请求体；POST 要求 JSON Content-Type 和准确的 Content-Length。不支持 chunked、查询参数、百分号编码路径或重复头字段。原始中文 URL 不是居民 ID，返回 400。
- 默认只读模式 POST 返回 405；命令模式未知命令返回 404。错误响应也遵循统一 JSON 包装，不要把 HTTP 状态失败吞掉当成测试通过。
- 最多保留 8 个连接；超过上限直接断连，客户端可能看到网络错误，不保证收到 JSON 503。
- 请求头上限 8 KiB，POST 体上限 4 KiB，响应 JSON 上限 64 KiB；每连接绝对生命周期 3 秒，慢速逐字发送也不能无限占用。
- 每次收发最多 4 KiB，按帧轮转连接并设置 2 ms 轮询预算；单次世界投影耗时不受此预算抢占，真实高负载性能仍待阶段 6 测量。

本机监听不是进程级鉴权：本机其他程序也能使用已开放的能力，命令模式下包括改速。正式发布默认不开桥，不能将其当作互联网服务部署。

## 4. 阶段 1B：速度与玩家移动命令

### 4.1 速度命令

先用隔离测试世界验收，不需要让正式存档加速，也不会调用真实模型。运行下面的命令，会开放一个存在 45 秒的测试世界，端口为 **18865**：

```powershell
& 'D:\Godot\Godot_v4.7.2-stable_win64_console.exe' --headless --path 'D:\ChenDian\CeKaiAndAI\upstream\game' --script res://tests/game_test_bridge_test.gd -- --gametest-isolated-test --bridge-smoke-server --bridge-smoke-commands
```

看到 `GAMETEST_SMOKE_READY` 后，在另一个 PowerShell 窗口一次粘贴运行：

```powershell
$bridgeUrl = 'http://127.0.0.1:18865'
$snapshot = Invoke-RestMethod "$bridgeUrl/world/state" -TimeoutSec 3
$commandId = [guid]::NewGuid().ToString()
$command = @{
    commandId = $commandId
    idempotencyKey = $commandId
    expectedSessionId = $snapshot.data.sessionId
    expectedWorldGeneration = $snapshot.data.worldGeneration
    expectedStateVersion = $snapshot.data.stateVersion
    timeoutMs = 3000
    speed = 2
}
$jsonBody = $command | ConvertTo-Json -Compress
Invoke-RestMethod "$bridgeUrl/commands/set-speed" -Method Post -ContentType 'application/json' -Body $jsonBody -TimeoutSec 5 | ConvertTo-Json -Depth 8
Invoke-RestMethod "$bridgeUrl/commands/$commandId" -TimeoutSec 3 | ConvertTo-Json -Depth 8
# 完全相同的请求再发一次，应得到同一执行结果，不再次改速。
Invoke-RestMethod "$bridgeUrl/commands/set-speed" -Method Post -ContentType 'application/json' -Body $jsonBody -TimeoutSec 5 | ConvertTo-Json -Depth 8
Invoke-RestMethod "$bridgeUrl/world/state" -TimeoutSec 3 | ConvertTo-Json -Depth 8
```

| 请求参数 | 小白解释 |
|---|---|
| `commandId` | 这次操作的编号，用于之后查结果。它不是每次 HTTP 请求的 `requestId`。 |
| `idempotencyKey` | 防重复执行的凭据。网络重试必须保留原值和全部原参数；新操作用新编号。 |
| `expectedSessionId` | 我准备操作哪一个小镇会话，直接复制刚读到的值。 |
| `expectedWorldGeneration` | 这次加载世界的代次。重建/读档后旧代次不能用于新操作。 |
| `expectedStateVersion` | 我依据哪个状态版本操作；世界变了就拒绝，避免在旧状态上执行。 |
| `timeoutMs` | 从服务器接受连接到开始执行允许经过多少毫秒，范围 1～3000；不是游戏分钟。 |
| `speed` | 目标倍率，只允许整数 1、2、3。 |

编号只允许英文字母、数字、`-`、`_`、`:`，长度 1～128。请求不允许缺字段或多字段。`protocol.schema.json` 的 `$defs.setSpeedRequest` 定义请求合同，GDScript 按同一合同做显式校验；没有声称在引擎里嵌入通用 JSON Schema 执行器。

成功时 `accepted=true`、`status=completed` 表示正式运行时已经完成设置；`result.simulationSpeed` 为执行时的倍率，`result.changed=false` 表示原本就是该倍率。真正改变倍率才增加世界版本。以后异步移动会另外实现受理和完成状态，不能用这里的同步结果冒充移动完成。

| HTTP / 错误码 | 含义与处理 |
|---|---|
| 400 `INVALID_COMMAND_SCHEMA` / `INVALID_JSON` | 参数或 JSON 错误，修正后再提交。 |
| 409 `SESSION_CONFLICT` / `WORLD_GENERATION_CONFLICT` | 会话或代次已换，重新观察世界，不能盲目重放旧动作。 |
| 409 `STATE_VERSION_CONFLICT` | 当前版本与预期不同，重新查询并判断动作是否仍适合，再建新命令。 |
| 409 `IDEMPOTENCY_CONFLICT` / `COMMAND_ID_CONFLICT` | 同一编号被用于不同请求，不会执行。 |
| 408 `COMMAND_TIMEOUT` | 请求已收全，但执行前等待超过自己的期限，未执行。 |
| 503 `WORLD_NOT_READY` | 主菜单或世界停止，不能改速。 |
| 503 `COMMAND_CAPACITY_REACHED` | 本桥实例已记录 128 条成功命令，拒绝新命令，旧结果仍可查。 |
| 404 `COMMAND_NOT_FOUND` | 本桥没有该编号的执行记录，不能跨桥重启据此认定旧命令从未执行。 |

边界：

- 幂等检查在再次执行、当前版本和执行期限检查之前；同键同参数返回原始回执。回执的时间、版本和代次是**当时的执行结果**，不是最新世界状态，最新状态需另查 `/world/state`。
- 每个桥对象最多保留 128 条成功回执，不自动淘汰、不落盘。满容量后拒绝新命令，避免忘掉旧键后重复执行。停止/重开监听保留回执；进程或桥对象重建后回执丢失，但新桥随机标识使旧代次失效。这不是跨进程“恰好执行一次”。
- 收包不全超过 3 秒会直接断连；客户端超时/断连不等于业务没有执行，应先查询命令结果或使用完全相同的请求重试。这里的 408 只涵盖执行前期限，不声称能中断已经执行的同步操作。
- 需要在真实游戏手工验证时，先正常保存并退出，再使用第 1 节启动命令并在最后额外加 `--gametest-commands`。`health.readOnly=false` 才表示写能力开放。不要同时启动两份游戏操作同一存档；当前自动化不会修改你的正式存档。

### 4.2 真实玩家移动命令

该命令不是改坐标或瞬移。Bridge 每帧向正式玩家输入层写方向，`CharacterBody2D` 仍负责速度、碰撞和实际位置；抵达容差范围才记为 `completed`，撞住、超时、暂停、切图或失去控制权都会停止输入并给出失败结果。

先退出其他游戏实例，再用命令模式启动正式游戏：

```powershell
& 'D:\Godot\Godot_v4.7.2-stable_win64_console.exe' --path 'D:\ChenDian\CeKaiAndAI\upstream\game' -- --gametest-bridge --gametest-commands
```

进入存档后，点击画面下方的白色小人按钮并等待降落完成。必须是玩家可移动、游戏未暂停、没有切换室内外的状态。随后在第二个 PowerShell 窗口一次粘贴：

```powershell
$bridgeUrl = 'http://127.0.0.1:18765'

function Invoke-PlayerMove([double]$dx, [double]$dy) {
    $resumeDeadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $snapshot = Invoke-RestMethod "$bridgeUrl/world/state" -TimeoutSec 3
        if (-not $snapshot.data.lifecycle.paused) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $resumeDeadline)
    if ($snapshot.data.lifecycle.paused) {
        throw "世界仍处于暂停状态：$($snapshot.data.lifecycle.pauseReasons -join ', ')"
    }
    $avatar = $snapshot.data.playerAvatar
    if (-not $avatar.present) { throw '玩家化身尚未进入场景，请先点击白色小人并等降落完成。' }

    $script:moveCommandId = [guid]::NewGuid().ToString()
    $script:moveCommand = @{
        commandId = $script:moveCommandId
        idempotencyKey = $script:moveCommandId
        expectedSessionId = $snapshot.data.sessionId
        expectedWorldGeneration = $snapshot.data.worldGeneration
        expectedStateVersion = $snapshot.data.stateVersion
        timeoutMs = 5000
        spaceId = $avatar.spaceId
        targetPosition = @(
            ([double]$avatar.position[0] + $dx),
            ([double]$avatar.position[1] + $dy)
        )
        tolerance = 6
    }
    $script:moveJson = $script:moveCommand | ConvertTo-Json -Compress
    try {
        Invoke-RestMethod "$bridgeUrl/commands/move-player" -Method Post -ContentType 'application/json' -Body $script:moveJson -TimeoutSec 8
    } catch {
        if ($_.ErrorDetails.Message) { $_.ErrorDetails.Message | ConvertFrom-Json } else { throw }
    }
}

# 先试向右移动 64 像素。返回 TARGET_NOT_WALKABLE 时，可改试 -64,0 或 0,64 或 0,-64。
Write-Host '命令开始后请立刻切回游戏窗口；脚本会等待 background 暂停解除，最长 15 秒。'
$accepted = Invoke-PlayerMove 64 0
$accepted | ConvertTo-Json -Depth 8

if ($accepted.ok) {
    if ($accepted.data.status -eq 'running') {
        do {
            Start-Sleep -Milliseconds 200
            $moveReceipt = Invoke-RestMethod "$bridgeUrl/commands/$moveCommandId" -TimeoutSec 3
            $moveReceipt.data | ConvertTo-Json -Depth 8
        } while ($moveReceipt.data.status -eq 'running')
    }

    # 完全相同的请求再发一次：应返回同一终态，不应让玩家再走一遍。
    Invoke-RestMethod "$bridgeUrl/commands/move-player" -Method Post -ContentType 'application/json' -Body $moveJson -TimeoutSec 8 | ConvertTo-Json -Depth 8
} else {
    Write-Host '命令未受理：不要执行幂等重放，请先按 error.code 修正。'
}
```

新增参数含义：

| 请求参数 | 小白解释 |
|---|---|
| `spaceId` | 玩家当前所在地图空间，直接复制 `playerAvatar.spaceId`；命令不能跨空间移动。 |
| `targetPosition` | 目标坐标 `[x,y]`。它必须属于当前空间的可行走区域。 |
| `tolerance` | 距目标多少像素以内算到达，允许 2～64；示例使用 6。 |
| `timeoutMs` | 移动执行期限，允许 1～30000 毫秒；到期会停止输入并记录 `MOVE_TIMEOUT`。 |

其余会话、代次、版本和幂等字段与速度命令相同。POST 成功受理返回 HTTP 202 且 `status=running`，这时不能提前声称已经到达；查询到 `completed` 才成功。执行期间失败仍通过查询回执返回 HTTP 200、`status=failed` 和 `failure.code`，因为“命令已成功受理”和“游戏动作最终失败”是两层结果。

常见移动错误：

- `AVATAR_NOT_ACTIVE`：仍在旁观或降落阶段；先进入玩家模式。
- `WORLD_PAUSED`：世界暂停。Windows 上切到 PowerShell 后通常会出现 `pauseReasons=["background"]`；使用示例中的等待逻辑并立刻切回游戏，不要让 Bridge 擅自解除暂停。
- `AVATAR_MOVEMENT_BLOCKED`、`AVATAR_TRANSITION_ACTIVE`：对话/冲突锁定或室内外切换中，不能移动。
- `PLAYER_MOVE_BUSY`：已有一个外部移动尚未终止；等查询结果，不要并发抢控制。
- `SPACE_CONFLICT`：客户端使用了旧空间；重新读 `/world/state`。
- `TARGET_NOT_WALKABLE`：目标在墙、水域、地图外或其他不可行走区域；换一个方向或更短距离。
- `MOVE_BLOCKED`、`MOVE_TIMEOUT`：命令已受理但实际未到达，Bridge 已清空虚拟输入；本阶段不会自动绕路。

若得到 `STATE_VERSION_CONFLICT`，说明观察后世界已变化；重新运行 `Invoke-PlayerMove` 获取新快照和新命令编号。若只是客户端超时、没有收到响应，不要立即生成新编号：先查询 `$moveCommandId`，查不到时才用完全相同的 `$moveJson` 重试。

### 4.3 正式会话保存、读档与事件流

`POST /commands/save` 和 `POST /commands/load` 只接受前六个通用命令字段，不接受文件路径、任意槽位或存档内容。保存调用现有 `TownSessionUiService.begin_create_save_async()` 事务，读档只允许当前正式会话所在槽位，并复用现有“退出当前场景—正式恢复—挂载新 TownRuntime”流程；Bridge 不直接读写存档文件，也不在 HTTP 回调栈中同步销毁场景。

两种命令都先返回 HTTP 202、`status=running`，之后通过 `GET /commands/{commandId}` 查询终态。`timeoutMs` 范围为 1～120000 毫秒。Bridge 侧超时只是停止等待并记录失败，不能倒推底层存档事务一定没有发布；遇到客户端超时必须先查原 `commandId`，不要换编号重发。

WebSocket 地址是 `ws://127.0.0.1:18765/events`，与 HTTP 共用端口。它只推送连接建立后的事件：

- `command-completed`：已受理命令达到完成态，带原 `commandId`、类型和结果。
- `command-failed`：已受理命令最终失败，带原 `commandId` 和 `failure.code`。
- `state-version-changed`：状态版本或世界代次变化，同时给出前后版本/代次。

每条事件有全局递增的 `eventSequence` 和唯一 `eventId`。事件不落盘、不补发；断线、序号跳跃或队列过载断开后，应重新查询 `/world/state` 和相关命令回执。每个连接最多排队 64 条事件，不能用无限缓冲掩盖慢消费者。WebSocket 仍执行本机 Host/Origin 限制，不接受扩展、子协议或客户端业务消息。

正式存档联调用脚本已经封装好。它会在**当前打开的 15 人正式存档**上新增一次正常保存，然后读回该存档；不会选择其他槽位，也不会删除文件。先用 `--gametest-bridge --gametest-commands` 启动游戏并进入要验证的存档，再在另一个 PowerShell 运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'D:\ChenDian\CeKaiAndAI\upstream\game\test_bridge\phase_1c_smoke.ps1'
```

这里的 `Bypass` 只作用于新启动的这一次 PowerShell 进程；脚本结束后自动失效，不修改当前用户或整台电脑的永久执行策略。若直接用 `& '...ps1'` 得到 `PSSecurityException / UnauthorizedAccess`，说明脚本还没有开始运行，因此也没有触发保存或读档。

脚本正文刻意只使用 ASCII 提示文本，以兼容 Windows PowerShell 5.1 对“无 BOM UTF-8”文件按系统代码页读取的旧行为；业务输出中的居民数据仍由 HTTP JSON 以 UTF-8 传输。

通过时显示 `PHASE_1C_SMOKE_PASS`、保存版本、读档前后不同的 `worldGeneration`、15 位居民和收到的事件类型。脚本能自动核对协议层、居民数量、会话一致性、保存发布和读档换代；记忆正文有隐私边界，任务与世界日志也未暴露给 Bridge，所以仍需在读档后的游戏 UI 中手工确认：15 位居民、世界日志、任务和居民记忆都保留。命令轮询允许在 120 秒总期限内出现短暂连接失败，并始终查询原 `commandId`，不会因为场景重建期间一次 HTTP 超时就重复发起读档。若脚本最终失败，保留完整红字和命令回执，不要删除存档或反复换编号执行。

## 5. 可重复的离线回归

```powershell
& 'D:\Godot\Godot_v4.7.2-stable_win64_console.exe' --headless --path 'D:\ChenDian\CeKaiAndAI\upstream\game' --script res://tests/game_test_bridge_test.gd -- --gametest-isolated-test
```

测试固定使用 `127.0.0.1:18865`。它从仓库 fixture 构建真实 World，使用真实 TCP 请求验证读接口和速度命令，并用真实 `TownRuntime`/`CharacterBody2D` 验证移动；不读取用户存档，不调用真实 LLM。`--gametest-isolated-test` 会跳过 GameFlowHost 的 UI/Provider 初始化，只应在离线测试中使用。请先等 45 秒联调实例退出，避免占用同一测试端口。

外部客户端联调模式：给上述测试命令加 `--bridge-smoke-server`，出现 `GAMETEST_SMOKE_READY` 后，45 秒内可从 PowerShell 查询 `http://127.0.0.1:18865/world/state`。它是独立测试世界，不是用户正在玩的存档；时间到自动关闭。

覆盖：未就绪、15 人查询、玩家公开状态、未知 ID、中文 JSON 字节长度、非法方法/报文/Host/Origin、部分收包、连续查询、暂停后查询、世界重启代次变化、慢连接超时、连接上限和释放端口；还覆盖速度命令，以及真实玩家逐帧移动、碰撞阻挡、执行超时和终态停止输入。

2026-09-15 验证结果：`GAMETEST_BRIDGE_PASS checks=685`、`TOWN_WORLD_FOUNDATION_PASS checks=1576`、`TOWN_OCCUPATION_PASS checks=419` 均退出码 0；协议 Schema 通过 Draft 2020-12 元 Schema 检查，PowerShell 联调脚本通过静态解析。Bridge 回归覆盖保存/读档异步回执、幂等重放、读档换代、WebSocket 101 握手及完成/失败/版本事件。修复版阶段 1C 脚本在隔离 15 人世界输出 `PHASE_1C_SMOKE_PASS`，保存 revision `7`、世界代次由 `:1` 变为 `:2`；用户正式读档后又从 UI 确认 15 位居民、世界日志、任务状态和居民记忆均保留。这些证明阶段 1 功能闭环，不是损坏存档、持续可用性或压力性能指标。

实现收发语义参照 [Godot StreamPeer 官方说明](https://docs.godotengine.org/en/stable/classes/class_streampeer.html)，使用非阻塞 partial 方法，不使用等待足量字节的阻塞读取。

## 6. 当前进度

2026-09-13：用户完成真实存档联调，15 位居民可查询；暂停时版本 3526 不变，恢复后时间从 04:37 推进至 04:45、版本至 3538，并确认操作正常。阶段 1A 已收口。

阶段 1A、1B、1C 均已完成，阶段 1 已收口并进入阶段 2。正式读档重建期间曾出现单次 5 秒查询超时，修复后的客户端会在总期限内轮询同一命令；该可用性窗口将在阶段 2 量化。

阶段 2 pytest 已建立。以下命令会按模块依次启动只读模式和显式命令模式的隔离 15 人 Godot fixture，测试完成后关闭测试进程，不读取或写入正式存档：

```powershell
conda run -n gametest-phase2 python -m pytest -c game/test_bridge/pytest.ini game/test_bridge/phase2_tests -q
```

2026-09-17 在 Python `3.11.16` 专用环境最新实测为 `26 passed in 51.91s`；新增 Python 文件经 Pylint 检查为 `10.00/10`。其中 5 个写接口用例覆盖改速、结果查询、幂等重放、命令身份冲突、状态版本冲突、非法 Schema、异步保存/读档、并发保存冲突、世界换代和无玩家运行时的移动拒绝，所有响应都经过协议 Schema。pytest 会自动在忽略提交的 `artifacts/` 下追加一次运行的 JSONL，并把相同事件写入共享 `phase2_evidence.sqlite3`。

首次写接口执行曾为 `4 passed / 1 failed`：测试把缺少玩家运行时误写成“化身未激活”，实际接口正确返回 HTTP 503 `PLAYER_RUNTIME_NOT_READY`。修正断言后写接口为 `5 passed`；原失败运行仍保留在数据库。Bridge 保存/读档用例仍使用隔离 Fake FlowHost 验证接受态、完成态、并发冲突、幂等和世界换代；另有 Godot 正式成对存档往返测试使用独立 `user://tests/...` 槽位，以 15 位居民真实验证进行中工作任务、指定世界日志和指定居民记忆在保存后现场改写、再恢复旧修订时保持保存点内容。最新输出为 `SESSION_SAVE_CONTINUE_ROUNDTRIP_PASS checks=141`，不读取正式玩家存档。

只读 Postman 资产位于 `postman/`，含 5 个请求、14 个断言和 2 个本地环境变量。隔离服务启动后可运行：

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$newmanReport = "game/test_bridge/artifacts/newman-$stamp.json"
& 'C:\Users\Administrator\AppData\Roaming\npm\newman.cmd' run 'game/test_bridge/postman/GameTestBridge.postman_collection.json' -e 'game/test_bridge/postman/local-isolated.postman_environment.json' -r cli,json --reporter-json-export $newmanReport
conda run -n gametest-phase2 python -m game.test_bridge.evidence_import --evidence-root game/test_bridge/artifacts newman --report $newmanReport
```

实测为 `5 requests / 14 assertions / 0 failures`，总耗时 `436 ms`。PowerShell 下显式调用 `newman.cmd`，避免本机脚本执行策略拦截 `newman.ps1`；这不会修改永久执行策略。

Locust 冒烟命令如下，`--users` 分别取 `1` 和 `5`。默认是不改状态的只读场景；使用命令模式隔离服务器并追加 `--command-mix` 时，会混合执行读取、改速、完全相同请求重放、终态查询和预期幂等冲突。并发产生的 `STATE_VERSION_CONFLICT` 以及故意构造的 `IDEMPOTENCY_CONFLICT` 只有在 HTTP 状态与错误码都符合预期时才记为业务通过，其他错误仍记为失败。不要把默认 CSV 当成精确终态：它按周期采样，可能少记结束前的最后一批请求；`--evidence-json` 会在停止事件中保存最终计数和业务结果分布：

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$locustReport = "game/test_bridge/artifacts/locust-final-5user-$stamp.json"
conda run -n gametest-phase2 locust -f game/test_bridge/locust/locustfile.py --headless --users 5 --spawn-rate 5 --run-time 10s --host http://127.0.0.1:18865 --only-summary --evidence-json $locustReport
conda run -n gametest-phase2 python -m game.test_bridge.evidence_import --evidence-root game/test_bridge/artifacts locust --final-json $locustReport
```

混合场景把上面命令的报告名改为 `locust-mixed-final-...json`，并在 Locust 参数末尾加入 `--command-mix`。最终快照实测：

- 只读：1 用户为 `68 requests / 0 failures`、p50 `5 ms`、p95 `9 ms`、p99/最大 `12 ms`；5 用户为 `355 requests / 0 failures`、p50 `6 ms`、p95 `9 ms`、p99 `19 ms`、最大 `183 ms`。
- 混合命令：1 用户为 `113 requests / 0 failures`、p50 `7 ms`、p95 `10 ms`、p99 `14 ms`、最大 `226 ms`；12 次命令完成、幂等重放、终态查询和预期幂等冲突均通过。
- 混合命令：5 用户为 `688 requests / 0 failures`、p50 `7 ms`、p95 `15 ms`、p99 `27 ms`、最大 `35 ms`；87 次命令完成并各自通过重放、终态查询和预期幂等冲突，另有 9 次预期状态版本冲突。

第一次混合运行因脚本把成功响应的 `error: null` 当字典读取而产生 worker exception；修复空值处理后通过。另一次复跑发生在隔离服务器正常到期退出之后，表现为 HTTP 0 连接超时；改为每档重新启动服务器后通过。两次失败报告均保留，未导入成成功证据。以上仍只是并发正确性基线，不是容量或 SLA 结论；阶段 6 再用 JMeter 做 1/5/10/20 梯度容量与资源监控。

Airtest Windows 黑盒冒烟使用专用环境中的 `airtest==1.4.3`，依赖清单位于 `requirements-phase2-ui.txt`。脚本只启动独立 Godot 调试进程，不进入存档、不开始新游戏、不改变设置；它验证启动页到加载游戏页、游戏设置页、模型设置页三条路径，并在每条路径后确认返回启动页：

```powershell
& 'D:\Anaconda\envs\gametest-phase2\python.exe' 'D:\ChenDian\CeKaiAndAI\upstream\game\test_bridge\airtest_smoke.py' --repeat 5
```

脚本会短暂将 Godot 置顶，但通过窗口消息点击，不移动实体鼠标。截图裁剪、模板尺寸和坐标会按当前 16:9 窗口与 Windows DPI 缩放；启动时要求画面复杂度达标且连续两帧稳定，避免把灰屏或黑屏当成可操作页面。正式运行 `airtest-20260918T104150299501Z-5c15cf55` 为 `15/15 passed`、`0 failed`、总耗时 `174792.6 ms`，目标页相似度范围 `0.889945～0.955135`，返回页范围 `0.932164～0.999517`，生成 31 张基线/步骤截图并写入独立 JSONL 和共享 SQLite。早期启动未渲染、前台焦点限制和 DPI 尺寸不匹配的失败证据均保留；它们是测试脚手架或环境问题，不是游戏产品缺陷。

该结果只覆盖三个启动页只读导航路径。实际新游戏/读档、玩家交互、错误提示和存档内容仍应由接口断言、隔离存档测试及人工 UI 核对承担，不能用这 15 次通过宣称全部 UI 已覆盖。

使用 SQLite 查看最近运行的成功率：

```powershell
sqlite3 -header -column 'game/test_bridge/artifacts/phase2_evidence.sqlite3' "SELECT tool,status,total,passed,failed,skipped,other,round(CASE WHEN total>0 THEN passed*100.0/total ELSE 0 END,2) AS success_pct,round(duration_ms,1) AS duration_ms FROM runs ORDER BY started_at DESC;"
```

数据库同时保存运行、用例/断言、Bridge 响应、请求、错误、延迟、Locust 业务结果数量和 Airtest 路径结果；取不到的关联字段明确为 `null`。

## 阶段 2 CI：GitHub Actions 与本机复跑

`.github/workflows/phase2-validation.yml` 仍使用 GitHub Actions 托管 Windows runner，分为两个职责明确的 job：`bridge-regression` 安装核心依赖、固定版本 Python `3.11.9`、Node/Newman 和 Godot，调用同一个 `run_phase2_ci.ps1` 执行预检、核心 Python 静态分析、pytest、Godot Headless 和 Newman。Godot stage 依次运行 Bridge 合同套件、咖啡职业回归、网关连续性套件；后两项仅在各自测试子进程运行期间临时禁止真实 provider 网络访问。`locust-static` 另装核心加 Locust 依赖，检查 Locust 场景的语法和导入。两者目前都在每次工作流触发时执行，不使用 `paths` 跳过必需检查；Locust job 只是静态检查，不冒充 1/5 客户端并发实跑。本机 Conda 使用 Python `3.11.16`；两者同属 3.11 系列，报告记录实际补丁版本，不伪称二进制完全一致。核心阶段结果汇总为 `*-ci-summary.json` 并导入 JSONL/SQLite；JUnit、日志、摘要和独立的 Locust 静态日志作为 Actions artifact 保留 7 天。历史 Jenkins 摘要导入入口保留用于读取旧证据，不代表继续使用 Jenkins。

本机在仓库根目录运行（证据目录必须已存在）：

```powershell
& game/test_bridge/run_phase2_ci.ps1 -Stage all -RunId local-check -ExecutionMode local
```

若工具不在默认路径，使用 `-PythonPath`、`-GodotPath`、`-NewmanPath` 显式传入；脚本不会修改永久环境变量。Airtest 需要真实前台图形窗口，仍按上文单独运行，不在无头 runner 上伪装 UI 验收。阶段 2 的 CI 验收还要求远端同一提交连续 3 次成功并保存报告；工作流文件存在或本机单次通过都不算完成。咖啡服务调度问题已提交独立[上游 PR #166](https://github.com/mewamew/my_ai_town/pull/166)，缺陷闭环报告仍需补齐修复前后证据。Airtest 子项已经验收，阶段 2 整体仍不能划线。

2026-09-24 本机整链结果：首次运行在 Pylint 命名检查失败并保留摘要；修正后第二次运行 Pylint `10.00/10`、pytest `27 passed`、Godot `685 checks`、Newman `5 requests / 14 assertions / 0 failures`，并成功导入 CI 摘要。GitHub Actions 首次运行在 checkout 阶段因仓库内历史存档 fixture 的 Windows 长路径失败；第二次在 Python 安装阶段发现 Windows runner 不提供本机的 `3.11.16`。工作流已改为 checkout 前启用 Git longpaths，并使用官方清单中 Windows x64 可安装的 `3.11.9`；后续远端运行结果仍须单独核对。

2026-09-25 第三次远端运行已进入回归步骤，预检却在空证据目录中读取不存在的 `Measure-Object.Sum` 而失败。本机用现有空目录复现同一严格模式错误，修正后空目录为 `0` 字节、非空目录仍返回实际大小；整链 `local-actions-empty-evidence-fix` 再次完成 Pylint `10.00/10`、pytest `27 passed`、Godot `685 checks`、Newman `5 requests / 14 assertions / 0 failures` 和 CI 摘要导入。是否解决 runner 的首次空目录场景，以新提交的远端实跑为准。

2026-09-25 下一次远端运行 [36085623192](https://github.com/haohao88888888/my_ai_town/actions/runs/36085623192) 的空目录预检已通过，但核心 Pylint 因未安装可选 Airtest/Locust 依赖而报 11 个导入错误。这是 CI 依赖与检查目标错位，不是业务回归失败。已将核心 Pylint 目标限制为核心模块、把 Locust 静态检查拆到独立 job；本机两个目标各为 `10.00/10`，PowerShell 语法与工作流 YAML 解析通过。远端是否通过、同一提交连续三次是否成功，仍以新工作流实跑为准。Airtest 的真实 UI 冒烟继续在本机前台执行，不作为无头 job 的通过项。
