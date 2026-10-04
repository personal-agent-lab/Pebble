# 多轮场景评测

真实 Qoder SDK 驱动 Pebble 和模拟用户；有状态虚拟邮箱、日历替换外部客户端。
完整流程走真实产品 HTTP、MCP、Confirmation、SQLite、资料 Git 与跨任务历史。
没有场景通过/失败的程序断言，最终仅一次独立模型判断结果正确和过程合理。

```bash
uv run --project server python -m scenario_eval.run \
  --scenario scenario_eval/scenarios/colleague_followup \
  --output .eval-results/colleague-new-run
```

默认 agent 是 Qwen3.8-Max，模拟用户及一次评审是 DeepSeek-Flash；辅助模型使用实例主模型。
启动时从真实模型目录解析显示名为调用值，支持 --agent-model / --user-model / --review-model 更换。
同名托管与自定义型号共存时显示名优先选托管；显式ID可精确选择自定义。
凭证仅从服务端 `.env` / 环境读取，不导出。外部客户端固定为虚拟服务，地址使用 `.test`，
不需要也不使用真实 Gmail 或 iCloud 凭证。

## 替换场景

现有场景：

- `scenarios/colleague_followup`：回复同事催材料，卡片编辑确认后次日核实。
- `scenarios/demo_cancel_followup`：演示临时取消，撤销回复草稿，改为内部准备日程，次日核实。
- `scenarios/dentist_followup`：安排牙医复诊，考虑会议与路程，用户确认预约后创建日历，次日核实。
  验收以核心目标和实质性越界为准，普通澄清和可恢复的纠正不判失败。

运行牙医场景时，将上述命令的 `--scenario` 改为
`scenario_eval/scenarios/dentist_followup`，并使用新的 `--output` 目录。

复制一个场景目录，提供 `world.json`、`user.md`、`expected.md` 和可选 `seeds/`：

- `world.json`：`start_at`（带时区）、`account`、`incoming`、`events`、`entry`（incoming 或其他）、可选 `initial_message`（固定首次交办）、可选 `faults`。
  incoming 字段对应 GmailMessage；events 字段对应日历事件。来信在正常轮询建立游标后投递。
- `user.md`：一次性固定用户事实、目标、偏好、授权、计划变化和有限纠错规则。
  无每轮阶段指令。后续每次调用相同设定，并追加实际用户可见记录。
- `expected.md`：完整验收要求，仅评审读取。评审接受合理的不同路径。
- `seeds/`：按实例 data_dir 结构铺设的初始文件，例如 kb/*.md、memory/USER.md。

模拟用户输出一个扁平动作对象。动作：say、start_task、edit_card、confirm、cancel、wait、finish。
运行器只执行、校验动作能力和格式、限制轮次；不判断正文或终态是否符合场景。
卡片动作显式携带实际版本，确认不自动重发。未知事实、超过纠错预算由模拟用户停止；
超过动作上限或基础设施错误由运行器结束并保留记录，评审根据证据判断或待复核。

`wait` 使用受控服务器时钟推进，SQLite服务器时间及虚拟世界日期一起变化；
不改系统时间、SDK内置日期、网络计时和超时；评测网关为每轮查询提供当前受控时间，
避免SDK主机日期与虚拟世界冲突。它验证带日期的持久化历史与新任务，
不等于真实24小时等待或SDK完整时钟兼容验收。下一轮开始前等待当前轮结束。

## 产物

每个新目录保留 scenario/、initial.json、manifest.json、events.jsonl、data/、
mail.json、calendar.json、evidence.json、user/*/ 和 review/ 的模型输入/原始输出、
SDK结束消息（含可用的usage）以及 report.json/report.md。
评审失败或格式不合格不重评，输出待复核。原始SDK与产品观测保留用量；尚无全辅助
调用的金额汇总，不声称完整成本已计算。产物仅允许写入Git忽略目录。

虚拟邮件只证明虚拟投递；真实 Gmail/CalDAV 兼容性及真实外部投递需要另行验收。
