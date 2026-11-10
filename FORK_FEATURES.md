# GXinOvO 个人功能清单

这里记录 [GXinOvO/hermes-agent 的 `dev` 分支](https://github.com/GXinOvO/hermes-agent/tree/dev) 在官方 Hermes Agent 基础上自行维护、修复或移植的增强。按用途归并，同一功能的多次修复只占一条；来源包括个人实现和注明出处的外部补丁。

**先看下面的速查表：想起一个功能后，按编号查入口、状态和追溯证据。** 官方已有的通用能力不因本分支修过相关代码就算个人新增。

## 一眼速查

“已合入”指代码进入 `dev`，不证明当前安装、网关或 Desktop 已加载；“自动路径”表示更新到对应版本后由现有流程触发。本表不充当机器部署状态表。

| 编号 | 个人增强／修复 | 对你有什么用 | 入口与状态 |
| --- | --- | --- | --- |
| F01 | 文件树彩色类型图标 | 按文件类型和常见文件名区分图标，更容易扫读目录 | Desktop → Files；已合入 |
| F02 | 鼠标跟随会话焦点开关 | 经过其他会话去 Files 时，可以选择不切走当前会话 | 设置 → 外观 → 鼠标经过时自动切换会话；默认开启，可关闭；已合入 |
| F03 | 文件名与内容搜索、结果跳转 | 在当前项目或子目录搜文件／正文，逐步显示结果并跳到匹配行列 | Files 顶部搜索按钮；支持大小写、正则、范围；已合入 |
| F04 | 会话任务概览面板 | 同时看原始目标、当前要求、todo、剩余事项及验收证据 | `task-overview` 插件；需后端和 Desktop 两端加载 |
| F05 | 子 Agent 交付检查与结果找回 | 核对真实产物，复用有效验证证据；错过回调后读取原结果 | `delegate_task(action="result", delegation_id=...)`；结构化交付按需用检查脚本；已合入 |
| F06 | 后台通知降噪与未完成工作提示 | 已消费的心跳和子任务内部进程退出不反复打断主任务；子任务未做完仍保留进展并提示 | 后台进程／委派自动路径；已合入 |
| F07 | `execute_code` 显式只读并行批次 | 一次组织多个独立文件读取、搜索或网页读取，保留预算、取消和调用身份 | 本地 transport 的 `hermes_tools.parallel`；需 Agent 显式使用；已合入 |
| F08 | 每个子 Agent 的精确工具白名单 | 审查任务只给读取工具，纯文本任务可不给工具；恢复后仍保留限制 | `delegate_task(tasks=[{"allowed_tools": [...], ...}])`；省略保持继承，`[]` 为零工具；已合入 |
| F09 | 后台完成结果持久化与恢复 | 已完成的后台工作可找回结果，减少因结果丢失而重跑；静默任务继续查询原句柄 | 进程、委派及受支持的后台交付路径；已合入，恢复结果不等于恢复执行 |
| F10 | 压缩、重试、中断与工具结果可靠性 | 压缩与辅助请求留下可核回执，中断可见，等待可取消，恢复提示不污染正常历史 | 相关运行自动路径；已合入；部分能力依赖已有配置 |
| F11 | 共享中转账号组恢复与切模型记录 | 同一 URL 多组凭据时按已存模型恢复正确分组；新建聊天切模型也能保存提示 | 会话恢复、切模型自动路径；已合入 |
| F12 | Skill 查找与检查点维护优化 | 减少重复元数据读取和目录扫描；回滚遇已有索引锁或准备失败时停止 | Skill 查找、检查点创建／恢复自动路径；已合入，未在此量化提速 |
| F13 | 长会话读保护、Cron 失败记账及审批继承开关 | 读保护状态跨回合保持一致；中止任务如实记失败；可让无人值守任务重新判定审批 | 读保护与 Cron 自动路径；审批继承默认兼容开启，需配置关闭；已合入 |
| F14 | 更新维护进度与离线数据库验证 | 区分代码更新、后置维护和数据库检查，不把超时／不可检查当损坏 | `hermes update`；`hermes sessions verify --source <离线副本>`；已合入 |
| F15 | 进程归属终端与受保护项目卷 | 以具体命令代次管理子孙进程和完成结果；候选受保护卷约束读后写与发布 | `owned-process` 插件；`owned_process` 需配置与 Linux 依赖；`protected_owned` 仍有候选验收边界 |
| F16 | 日常搜索、PATH 与统计修复 | 搜 `-foo` 不被当命令选项；PATH 去重；Insights 不漏算批量 Skill 操作请求 | `search_files`、本地终端、`hermes insights`；已合入 |
| F17 | 测试隔离与上游同步兼容 | 测试夹具不意外进入真实安装环境；同步上游时保留本分支增强 | 维护／测试路径；已合入，不单独算新产品功能 |
| F18 | 合入后自动补记个人功能 | `dev` 更新后用 DeepSeek 检查差异、归并已有条目并补漏，直接提交本清单 | GitHub Actions → Update personal feature catalog；专用服务器 Runner，需 Secret 和模型变量 |

## 使用时需要记住的边界

- **F03 文件搜索：** 搜索绑定发起时的连接、profile、会话和目录；宿主侧执行有界搜索，支持取消。达到范围、数量或预览上限时会显示不完整，不能把返回片段当成全项目搜尽。
- **F04 任务概览：** 读取持久化会话和原生 todo，展示实现、测试、审查、集成、启用五个阶段；缺少证据显示未验证。Agent 注册的状态不等于独立验收，插件也不会自动替你关闭任务。安装和两端加载方式见[插件说明](plugins/task-overview/README.md)。
- **F05 交付检查：** [检查脚本](skills/autonomous-ai-agents/hermes-agent/scripts/check-delegated-result.py)核对生命周期、结构化结果、声明文件和检查回执；任务本身是否正确仍由父 Agent 验收。`action="result"` 只读本会话／压缩谱系拥有的持久化委派记录，不重跑工作、不改变交付状态。证据复用属于执行指导，不能保证模型每次遵守。
- **F07 并行：** 只开放 `read_file`、`search_files`、`web_search`、`web_extract`，一次最多 16 项、最多 4 个工作线程，结果按输入顺序返回；写入和终端命令不在该并行入口内。格式是 `parallel([{"tool": "read_file", "args": {"path": "..."}}, ...])`，只适用于本地 transport；取消返回时，已开始的读取可能仍在收尾。
- **F08 工具白名单：** 名称必须是父 Agent 已有且当前启用的精确原生／MCP 工具名；不接收别名和通配符。限制覆盖工具发现、调用和恢复；显式限制不支持的外部 Agent 路由会被拒绝。它限制 Hermes 工具调用，不代表任意代码的文件系统、网络或凭据隔离，详见[委派说明](website/docs/user-guide/features/delegation.md)。
- **F09 结果恢复：** `/bg`、`/btw`、Cron 及进程／委派使用各自持久化结果；主进程重启不会自动续跑内存中的子 Agent。静默进程读取现有句柄；最终输出保留截断和总量信息，不保证完整日志永久可用。侧结果和终端结果受七天保留期约束；终端的 64 条历史上限只作用于已消费结果。恢复仍绑定原任务身份和目标，不能把旧结果转送到改过的作业。平台投递的接受、发出与对端确认分别判断，状态不明不能宣称已送达。
- **F10 可靠性修复：** 本分支增加辅助请求路由／wire／进度回执、压缩输入与摘要诊断、中断说明、主动工具结果裁剪的本地无正文日志，以及恢复脚手架的历史过滤。账号策略拒绝不会因切非流式向同一服务商重发；已有分类器允许的其他 provider fallback 保留。安全提示插件兼容批量 Skill 操作，启用后默认仍仅警告；委派上下文拒绝 `skill_manage` 变更，保留技能读取。这些不代表该插件是本分支新创，也不构成终端文件系统沙箱。
- **F11 身份恢复：** 显式请求的分组仍优先；旧会话才按端点和模型恢复身份。旧／新 home 写法只有能证明指向同一现有 home 和绑定数据库时才兼容，不能泛化成任意跨机器迁移。
- **F12 检查点：** 元数据缓存保留可见性和隔离检查；目录计数改为流式。已有索引锁不会被擅自清理，Git 超时先留清理窗口；这不构成自动修复所有锁问题。
- **F13 无人值守审批：** `approvals.unattended_inherit_approvals` 的兼容默认是 `true`；设置为 `false` 后，Cron、单次查询及 webhook/API 不消费交互会话已有授权，而按各自原生策略重新判断。不能把“增加开关”写成“所有无人值守任务默认隔离”。
- **F14 数据库验证：** `sessions verify` 要求一致的离线备份，不接收正在使用的 profile 数据库；不是对活跃 WAL 数据库直接 `cp` 后检查。检查不自动恢复或替换数据，时间预算也不能硬中断阻塞的文件系统 I/O。
- **F15 终端与项目卷：** [插件说明](plugins/owned-process/README.md)列出 `owned_process` 的命名空间、进程生命周期与依赖要求；失败不回退成本地执行，两种后端均不能据此标为生产采用。进程已退出但输出尚未排空时，`process_running=false, output_pending=true` 表示仍需读取最终输出。`protected_owned` 另需匹配的 authority 包、容器镜像及卷配置，仓库说明仍标为候选、待独立审查和生产采用；最近的受保护读取修复也不能单独证明可信身份完整绑定。合入源码不等于完成启用或整体安全验收。
- **F16 统计口径：** Insights 统计的是请求调用／操作次数，包含失败尝试，不是成功创建或修改的 Skill 数量。
- **F18 自动补记：** [工作流](.github/workflows/fork-catalog.yml)只响应本仓库 `dev` 的非纯清单推送，用仓库 Secret `FORK_CATALOG_API_KEY` 和变量 `FORK_CATALOG_BASE_URL`、`FORK_CATALOG_MODEL` 调用模型；当前指定 `deepseek-v4.1-flash`，请求 `reasoning_effort=max`，无其他模型回退。串行运行并从下方隐藏标记聚合未处理差异；只允许原子写入本清单，失败不推进标记，不创建额外 PR。模型输出经编号、来源、章节保留检查，内容仍可能判断有误；未处理差异超出单批上限时失败，不推进处理位置，需要 Agent 人工核对清单并更新标记。专用 Runner 需要服务器在线，当前接口是用户选择的内网 HTTP；工作流结果可在 [Actions](https://github.com/GXinOvO/hermes-agent/actions) 查看。

## 追溯证据

下表给出引入／修复提交和相关测试入口。**测试文件存在只证明可追溯，本次清单整理没有重跑这些功能测试，也不据此宣布整分支通过验收。** F05 的两项外部补丁保留原作者和上游 PR 来源，不冒称全部个人原创。

| 编号 | 来源 | 代表性实现／测试 |
| --- | --- | --- |
| F01 | [PR #3](https://github.com/GXinOvO/hermes-agent/pull/3)、[#4](https://github.com/GXinOvO/hermes-agent/pull/4) | [文件图标](apps/desktop/src/app/right-sidebar/files/file-icon.tsx) · [测试](apps/desktop/src/app/right-sidebar/files/file-icon.test.tsx) |
| F02 | [PR #2](https://github.com/GXinOvO/hermes-agent/pull/2)；后续默认值修复见提交索引 | [焦点偏好](apps/desktop/src/store/focus-follows-mouse.ts) · [测试](apps/desktop/src/store/focus-follows-mouse.test.ts) |
| F03 | [PR #11](https://github.com/GXinOvO/hermes-agent/pull/11)、[#12](https://github.com/GXinOvO/hermes-agent/pull/12)、[#14](https://github.com/GXinOvO/hermes-agent/pull/14)、[#19](https://github.com/GXinOvO/hermes-agent/pull/19)、[#20](https://github.com/GXinOvO/hermes-agent/pull/20) | [搜索面板](apps/desktop/src/app/right-sidebar/files/search-panel.tsx) · [宿主搜索](hermes_cli/file_search.py) · [归属测试](apps/desktop/src/app/right-sidebar/files/search-panel.owner.test.tsx) |
| F04 | [PR #23](https://github.com/GXinOvO/hermes-agent/pull/23) | [插件说明](plugins/task-overview/README.md) · [测试](plugins/task-overview/tests/core.test.mjs) |
| F05 | [PR #6](https://github.com/GXinOvO/hermes-agent/pull/6)、[#15](https://github.com/GXinOvO/hermes-agent/pull/15)、[#28](https://github.com/GXinOvO/hermes-agent/pull/28)；移植来源 [官方 #136266](https://github.com/NousResearch/hermes-agent/pull/136266)、[#148550](https://github.com/NousResearch/hermes-agent/pull/148550) | [执行指导](skills/autonomous-ai-agents/hermes-agent/references/task-execution.md) · [结果找回](tools/delegate_tool_registry.py) · [材料检查测试](tests/skills/test_delegated_result_lifecycle.py) |
| F06 | [PR #7](https://github.com/GXinOvO/hermes-agent/pull/7)、[#22](https://github.com/GXinOvO/hermes-agent/pull/22)、[#24](https://github.com/GXinOvO/hermes-agent/pull/24) | [通知策略测试](tests/tui_gateway/test_child_process_notification_policy.py) · [未完成提示测试](tests/tools/test_delegate_incomplete_notice.py) |
| F07 | [PR #26](https://github.com/GXinOvO/hermes-agent/pull/26)、[#27](https://github.com/GXinOvO/hermes-agent/pull/27)、[#31](https://github.com/GXinOvO/hermes-agent/pull/31) | [批处理实现](tools/code_execution_parallel.py) · [身份测试](tests/tools/test_code_execution_parallel_identity.py) |
| F08 | [PR #34](https://github.com/GXinOvO/hermes-agent/pull/34) | [白名单实现](tools/delegate_tool_allowlist.py) · [恢复测试](tests/tools/test_delegate_allowlist_restore.py) |
| F09 | [PR #30](https://github.com/GXinOvO/hermes-agent/pull/30)；结果恢复等直接提交见索引 | [进程结果恢复](tools/process_registry_result_recovery.py) · [后台记录测试](tests/hermes_state/test_background_results.py) · [静默结果测试](tests/tools/test_process_result_retention.py) |
| F10 | [PR #1](https://github.com/GXinOvO/hermes-agent/pull/1)、[#16](https://github.com/GXinOvO/hermes-agent/pull/16)；后续直接提交见索引 | [压缩回执](agent/compaction_events.py) · [恢复等待测试](tests/agent/test_primary_recovery_interrupt.py) · [历史过滤测试](tests/hermes_state/test_empty_recovery_history_views.py) |
| F11 | [PR #5](https://github.com/GXinOvO/hermes-agent/pull/5)、[#33](https://github.com/GXinOvO/hermes-agent/pull/33)；home 兼容直接提交见索引 | [账号组恢复](hermes_cli/runtime_provider_custom.py) · [测试](tests/hermes_cli/test_shared_relay_resume_identity.py) · [切模型记录测试](tests/tui_gateway/test_model_switch_marker_durability.py) |
| F12 | [PR #13](https://github.com/GXinOvO/hermes-agent/pull/13)、[#17](https://github.com/GXinOvO/hermes-agent/pull/17)、[#18](https://github.com/GXinOvO/hermes-agent/pull/18) | [检查点维护说明](website/docs/developer-guide/checkpoint-git-lifecycle.md) · [缓存测试](tests/tools/test_skill_lookup_cache_contracts.py) · [扫描测试](tests/tools/test_checkpoint_scan_io.py) |
| F13 | [PR #21](https://github.com/GXinOvO/hermes-agent/pull/21)；审批继承直接提交见索引 | [读保护上下文](agent/read_context.py) · [Cron 测试](tests/cron/test_guardrail_session_finalization.py) · [审批测试](tests/tools/test_unattended_consent_isolation.py) |
| F14 | [PR #8](https://github.com/GXinOvO/hermes-agent/pull/8) | [离线验证](hermes_cli/sessions_verify.py) · [维护进度测试](tests/hermes_cli/test_update_maintenance_progress.py) |
| F15 | 直接提交见索引 | [插件及边界说明](plugins/owned-process/README.md) · [原生测试](tests/tools/test_owned_process_native.py) · [受保护读取测试](tests/tools/test_protected_owned_missing_read.py) |
| F16 | [PR #10](https://github.com/GXinOvO/hermes-agent/pull/10)、[#29](https://github.com/GXinOvO/hermes-agent/pull/29)、[#35](https://github.com/GXinOvO/hermes-agent/pull/35) | [搜索操作数测试](tests/tools/test_search_pattern_operands.py) · [统计测试](tests/agent/test_insights.py) |
| F17 | [PR #9](https://github.com/GXinOvO/hermes-agent/pull/9)、[#32](https://github.com/GXinOvO/hermes-agent/pull/32)；同步本身不另记功能 | [环境隔离测试](tests/gateway/test_fixture_child_environment.py) · [客户端环境测试](tests/gateway/test_session_client_env.py) |

## 基线与覆盖范围

最近盘点：**2026-11-10**。以下是盘点时固定的来源，后续提交不会自动被算进这次覆盖。

- 本分支：[`bfc30c4020`](https://github.com/GXinOvO/hermes-agent/commit/bfc30c4020c43db6ffd87588a00455f361dd1a7a)。
- 官方 `NousResearch/hermes-agent/main`：[`b7db40d825`](https://github.com/NousResearch/hermes-agent/commit/b7db40d82568ea68ce321ac1ead3bfea354c7f56)。
- 两者共同祖先：[`8b50838443`](https://github.com/NousResearch/hermes-agent/commit/8b50838443c5c8f1015a10d54bb948270fed8f14)。本分支包含上游同步历史；共同祖先不等于今天官方主线。
- 盘点范围内，`upstream/main..origin/dev` 有 **53 条非合并提交**，归入上面 **17 组**；其中 F17 为测试／维护，不等于新增了 17 个独立产品功能。已逐条归组，完整提交索引如下。
- 当时的 34 个已合入 `dev` 的 fork PR 均在上表追溯；后续直接提交也列在索引。`git cherry` 对这 53 条没有发现 patch-id 等价补丁；这不能排除官方用另一种实现覆盖相同需求。
- **官方语义覆盖状态：待逐项核对。** 本表记录本分支维护历史，不宣称这些用途如今全为 fork 独有。后续确认官方已覆盖时，保留条目并改为“官方已吸收”或“部分覆盖”，注明官方提交和本分支仍需保留的差额。
- 不包含未合入 `dev` 的实验、其他仓库，以及仅存在于本机 profile 的 Skills、MCP、模型映射、Cron 和私人配置。没有在本次盘点里把本机模型层级策略当成已交付到 `dev` 的功能。

<details>
<summary>展开：53 条非合并差异提交的完整归组索引</summary>

| 编号 | 非合并差异提交（合并提交不计入） |
| --- | --- |
| F01 | [f14900a1c4](https://github.com/GXinOvO/hermes-agent/commit/f14900a1c4b60afa1417996bca6f18a1e30fe966) feat(desktop): add colored file-type icons to the Files tree<br>[b1d14e87a9](https://github.com/GXinOvO/hermes-agent/commit/b1d14e87a9287e451ef3524dbe0e2900115ab164) feat(desktop): Material/Bearded file icon palette for the Files tree |
| F02 | [2fd19031c8](https://github.com/GXinOvO/hermes-agent/commit/2fd19031c892896c7237ca8fd76718880fa836a8) fix(desktop): require explicit session focus selection<br>[b15131c77e](https://github.com/GXinOvO/hermes-agent/commit/b15131c77e692d7e450b4cefa9435dd5be793cc3) fix(desktop): make mouse-follow session focus optional<br>[fcbb56320d](https://github.com/GXinOvO/hermes-agent/commit/fcbb56320dbf1df52ce67b31419934842cf230e6) chore(desktop): complete focus preference locale coverage<br>[3ef044ca73](https://github.com/GXinOvO/hermes-agent/commit/3ef044ca73dced50252a71ff0a44d1c6e36795eb) fix(desktop): keep mouse-follow focus on by default, opt out instead |
| F03 | [974364747d](https://github.com/GXinOvO/hermes-agent/commit/974364747daf44c7ace58ad2d4306713b489609d) feat(desktop): scoped file name and content search in the files pane<br>[5a69694999](https://github.com/GXinOvO/hermes-agent/commit/5a69694999dde7bbdb144d5a3f5cf04d90cfd00b) fix(desktop): make the file-search input readable and its results scannable<br>[4e11bf39bf](https://github.com/GXinOvO/hermes-agent/commit/4e11bf39bffab102bf6890a1cd5fc9a74e0df224) fix(desktop): bound file search work and show it in progress<br>[56a01d8115](https://github.com/GXinOvO/hermes-agent/commit/56a01d81152cf806c66ca6479292db3a68b59670) feat(desktop): stream host-side file search and reveal source matches<br>[c647b7406e](https://github.com/GXinOvO/hermes-agent/commit/c647b7406e5ec1830fcc29f9c1314758f3351693) fix(search): fall back safely when scope pruning is too large |
| F04 | [833849dd03](https://github.com/GXinOvO/hermes-agent/commit/833849dd03371b8b1f28bbea93f5bca0961d3a32) feat(plugins): add read-only session task overview |
| F05 | [66c397d183](https://github.com/GXinOvO/hermes-agent/commit/66c397d18324eb37c4d9ffdfb13604d3c984f773) feat(skills): finish authorized tasks with a structured child handoff<br>[6eb7e596d6](https://github.com/GXinOvO/hermes-agent/commit/6eb7e596d6edf9044def4312b880d957524fa20e) fix(skills): reject incomplete delegated result envelopes<br>[dd08817af5](https://github.com/GXinOvO/hermes-agent/commit/dd08817af5010c3894a24b521cea131c18c9681a) fix(delegation): reuse completed worker verification evidence<br>[081a9975d2](https://github.com/GXinOvO/hermes-agent/commit/081a9975d2b92a2c2fd246fdb1e9ae76301682af) feat(delegation): read owned durable results without replay<br>[ca09e64cf7](https://github.com/GXinOvO/hermes-agent/commit/ca09e64cf719e9bb05cd6a3bc903b793685e2067) fix(delegation): retain observed writes through child cleanup<br>[477cd45d9a](https://github.com/GXinOvO/hermes-agent/commit/477cd45d9a9bc83405bb2c5e631703189ff8ef40) fix(delegation): keep structured child summaries free of runtime advisories |
| F06 | [cfb0258a76](https://github.com/GXinOvO/hermes-agent/commit/cfb0258a760446157b34c007124572714ea311bb) fix(process): drop queued heartbeats after terminal output is consumed<br>[466f48d1c7](https://github.com/GXinOvO/hermes-agent/commit/466f48d1c7d5a9c63ee39a7d9257509df6a7c912) fix(tui): filter subagent process notifications before waking parent<br>[64d9e00d02](https://github.com/GXinOvO/hermes-agent/commit/64d9e00d02559d7c3ed44911c99066375ee48f85) fix(delegation): preserve progress and escalate incomplete child work |
| F07 | [e082e15b34](https://github.com/GXinOvO/hermes-agent/commit/e082e15b34a873dffcf0941d23b89a4dd3e201ed) feat(execute-code): add bounded local read-only batches<br>[4bed95c1a9](https://github.com/GXinOvO/hermes-agent/commit/4bed95c1a98bbeb81ffb1793c6cbdb43edf96502) fix(execute-code): check stop after final batch completion<br>[37b9a4b077](https://github.com/GXinOvO/hermes-agent/commit/37b9a4b077b9486e64bf132d2b2614ba04f1f134) fix(execute-code): give parallel host calls distinct hook identities |
| F08 | [1adb91279b](https://github.com/GXinOvO/hermes-agent/commit/1adb91279b1930e51fce9154f289463f140f5764) feat(delegation): enforce per-task tool allowlists |
| F09 | [8e8a434cce](https://github.com/GXinOvO/hermes-agent/commit/8e8a434ccec312190cc7c8197d3b25b57a4d36af) feat(background): recover completed results without replaying work<br>[6cf3952c5b](https://github.com/GXinOvO/hermes-agent/commit/6cf3952c5be2374f61ba383c15d6cd6ada7ad7c2) fix(terminal): inspect silent background jobs without relaunching<br>[61ae1e53fa](https://github.com/GXinOvO/hermes-agent/commit/61ae1e53fad8c358ec111250a373d3f6d781ec71) fix(terminal): preserve final output truncation evidence |
| F10 | [f41d29c9c9](https://github.com/GXinOvO/hermes-agent/commit/f41d29c9c94a2926ce93549908a727ec9e2c8f3a) fix(core): preserve compaction, interruption, and tool-result repairs<br>[8dc0872119](https://github.com/GXinOvO/hermes-agent/commit/8dc0872119f994682e43a63494c54e092e1cac07) fix(agent): make primary recovery wait interruptible<br>[3f68d4241e](https://github.com/GXinOvO/hermes-agent/commit/3f68d4241edc6469615b12cde23dbdc3404f7f99) fix(compression): record proactive prune commits locally<br>[8fdd169e24](https://github.com/GXinOvO/hermes-agent/commit/8fdd169e248dd3fe212ecb3e3fe295f6732260a6) fix(aux): do not resend after an account policy block<br>[7f55ea0aa2](https://github.com/GXinOvO/hermes-agent/commit/7f55ea0aa2984bf7f892fd929142838837c0d675) fix(history): keep recovery scaffolding out of replay and retry |
| F11 | [4524121b8e](https://github.com/GXinOvO/hermes-agent/commit/4524121b8e1d9247d5a5ef70db70438f2fc3b320) fix(runtime): recover shared-relay sessions by endpoint and model<br>[7a3921e63f](https://github.com/GXinOvO/hermes-agent/commit/7a3921e63fd5461b1442b4d79c4d2efffa15b774) fix: recover migrated local receipts and persist model-switch markers atomically<br>[0dffc55b28](https://github.com/GXinOvO/hermes-agent/commit/0dffc55b28daff7f242623a696077d481ab26d68) fix(gateway): persist the model-switch marker on a draft chat |
| F12 | [da9dd86c63](https://github.com/GXinOvO/hermes-agent/commit/da9dd86c6306f3e0a10b83bd35dfc3f6803bf563) fix(runtime): cache skill metadata and preserve checkpoint cleanup<br>[e62ea60ba4](https://github.com/GXinOvO/hermes-agent/commit/e62ea60ba485ff1adfc7e72ccc0cd238964483ff) fix(runtime): reduce skill lookup I/O and fail closed on locked rollback<br>[173b6f150b](https://github.com/GXinOvO/hermes-agent/commit/173b6f150b9f56d2495b3bbb94c4374ce6bd61ab) perf(checkpoints): stream directory accounting |
| F13 | [81954414bc](https://github.com/GXinOvO/hermes-agent/commit/81954414bc28796778d80530d11673eed5f9d9aa) fix: preserve read guard epochs and report halted cron runs accurately<br>[250aef4290](https://github.com/GXinOvO/hermes-agent/commit/250aef42901fb5d3a3d5f49136ab0a914e10b3e3) fix: isolate unattended consent from interactive approvals |
| F14 | [04c5662dea](https://github.com/GXinOvO/hermes-agent/commit/04c5662dead0a5a8ccb261101dd20adcf98fec6d) fix(update): separate structural checks and show maintenance progress |
| F15 | [6da118beef](https://github.com/GXinOvO/hermes-agent/commit/6da118beefae4846266a407a186276e04c506fc4) feat(terminal): add reviewed owned process backend and completion lifecycle<br>[4d03e577cc](https://github.com/GXinOvO/hermes-agent/commit/4d03e577cc4accbaf68423d5ca77414b7ffb4c17) feat(terminal): add reviewed owned process backend and completion lifecycle<br>[59d88e873b](https://github.com/GXinOvO/hermes-agent/commit/59d88e873b1bd1881260c6defd3791225ff77c58) feat(terminal): integrate protected project volumes with owned processes<br>[fdf79ee2a3](https://github.com/GXinOvO/hermes-agent/commit/fdf79ee2a3855776d5ef0d1bd5c1162748646d0b) fix(files): validate protected read targets and authority tickets<br>[48df3f897f](https://github.com/GXinOvO/hermes-agent/commit/48df3f897fef053fadc24e1fe9062987a8ab0690) test(files): exercise protected read failure boundaries |
| F16 | [5b4ae2c646](https://github.com/GXinOvO/hermes-agent/commit/5b4ae2c6466bd336d53efcef8295d61b927d88bb) fix(search): treat dash-leading patterns as regex operands<br>[9f73a24a15](https://github.com/GXinOvO/hermes-agent/commit/9f73a24a15ef1cf4c6377d6b45ad1c45636bab91) fix: avoid duplicate user-local bin aliases in terminal PATH<br>[3c9c06d09d](https://github.com/GXinOvO/hermes-agent/commit/3c9c06d09dc25032429b640c9b20f9234ac17c9b) fix(insights): count batched skill_manage operations |
| F17 | [fdf64d32ac](https://github.com/GXinOvO/hermes-agent/commit/fdf64d32acee2de2681aabd57e10287ead4a13ad) test(gateway): preserve isolated fixture launch environments<br>[7311af78b6](https://github.com/GXinOvO/hermes-agent/commit/7311af78b65e5a7198e0b0c560222cf0d3f9a19d) fix(test): isolate client PATH checks from installed Hermes launcher<br>[f8dfc20a96](https://github.com/GXinOvO/hermes-agent/commit/f8dfc20a9662af35a7eec4a630b4283f4735fe29) test(tools): follow the boundary reset into agent.read_context |

</details>

## 增量维护记录

初始盘点的 53 条提交、34 个 PR 和 17 个分组保持原统计范围。此后新增维护条目单独追溯，不把历史盘点数字改成滚动总量。

| 编号 | 实现与验证入口 |
| --- | --- |
| F18 | [工作流](.github/workflows/fork-catalog.yml) · [模型补记脚本](scripts/ci/update_fork_catalog.py) · [测试](tests/scripts/test_update_fork_catalog.py)；引入 [de05b120f7d](https://github.com/GXinOvO/hermes-agent/commit/de05b120f7d830543ce2172c93a103ba948cd253)，稀疏检出修复 [7826b9496f](https://github.com/GXinOvO/hermes-agent/commit/7826b9496f1cb1b823797b21a772d379d84a0c63)；代码合入和服务器部署状态分别核对 |

### 2026-11-10 上游同步兼容

本轮协调 F05/F08/F09/F10/F15：保留子 Agent 结果找回、工具白名单、辅助请求回执和 owned 进程结果恢复，同时接入官方 [Anthropic timeout 修复](https://github.com/NousResearch/hermes-agent/commit/710b62c59a2c5a1c29d2448826e5071ef8c6b7a1)、[路由客户端共用解析](https://github.com/NousResearch/hermes-agent/commit/42be36381f3115225b8130ea2d278b0bb69279b9)、子任务结果分组和环境回收／退出竞争修复。上游覆盖这些基础能力，不据此认定已吸收本表增强；各条历史和默认值保留。

新增 [owned 进程环境关闭回归](tests/tools/test_owned_process_env_teardown.py)，覆盖自然退出、清理取消、输出待排空时的保活与结果发布，以及后端失联后坏环境回收和新命令重建；未验证的旧进程结果仍不伪造完成。接口快照由合并后的代码重新生成。该测试入口不代表整分支或实际安装通过验收，执行结果随同步 PR 记录；本次 Git 同步不更新运行安装。

## 后续维护规则

1. 新增、改变、移除个人增强时，在**同一交付**更新对应条目：用户用途、入口／开关、默认值、代码／启用状态、PR 或提交、验证证据。后续修复优先补到原编号，新增功能再分配稳定编号。
2. 同步官方主线时检查是否已有等价功能：明确标成“待核对／部分覆盖／官方已吸收”，附官方证据。只有确认差额和回归覆盖后再决定是否删除本地实现；不删除历史条目来掩盖来源。
3. “已合入”“本地测试通过”“当前安装已启用”“真实行为验收通过”“收益已测量”分别记录；未知就写未知。测试入口不冒充测试回执，预计提速不冒充测量结果。
4. README 只保留入口和一句用途；本文件作为唯一功能总表，不再复制一份容易失步的清单。提交历史保留实现细节，本表负责让人记得功能。

根目录 [AGENTS.md](AGENTS.md) 同时提示后续 Agent 更新本表；F18 在合入后补漏。Agent 应在功能交付中主动补记，不必等待自动工作流。自动化验证的是结构与写入范围，不替代人工审阅和功能验收。

<!-- fork-catalog:last-reviewed=7826b9496f1cb1b823797b21a772d379d84a0c63 -->
