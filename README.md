# 麦麦恋人（MaiLover）

此插件已不再更新，已经被最新插件麦生活（mailife）替代
**私聊专用虚拟恋人插件** — 基于 MaiBot Plugin SDK 2.5.4

> 麦麦有自己的生活 · 主动找你聊天 · 好感度调节温度

---

## 简介

麦麦恋人是为 MaiBot 设计的私聊专用插件，模拟一个有独立人格、会主动找你聊天的"网恋对象"。

**和普通聊天机器人的区别**：麦麦有自己的日程生活（赖床、做饭、晒太阳、偶尔想你），由 MaiBot 的 planner 大脑自主决策什么时候主动找你说话、说什么。你问"在干嘛"时，她会根据当前正在做的事自然回答。

### 核心机制

| 机制 | 说明 |
|------|------|
| **独立日程生活** | 每天自动生成麦麦一天的活动安排（赖床/做饭/晒太阳/想你），不是用户的日程 |
| **planner 自主发言** | 麦麦的"大脑"（MaiBot planner）自主决定说不说、说什么，插件只负责"提醒大脑该想想了" |
| **好感度系统** | 3 档位（熟悉/亲密/热恋）影响语气，可通过指令调整 |
| **静默时段** | 睡觉时间不打扰，可配置静默起止时间 |

---

## 核心特性

- **白名单守卫** — 仅指定 QQ 号私聊生效
- **麦麦虚拟生活** — 每天凌晨结合人设 + 作息骨架 + LLM 生成当天的活动安排
- **planner 状态注入** — 每次 planner 思考时都能看到"麦麦现在在干嘛"，让回复自然带上当前状态
- **主动聊天** — scheduler 定时提醒 planner"可以考虑说话了"，planner 自主决定发不发
- **早晚安仪式** — 早安/晚安时间窗触发 planner，麦麦主动跟你说早/晚安
- **想念机制** — 你太久没理她，麦麦会跑来说想你（每天最多 1 次）
- **"在干嘛"查询** — 你问"在干嘛"时，planner 调用 Tool 查麦麦当前活动，自然回答
- **静默时段** — 配置睡觉时间，那段时间麦麦完全安静
- **好感度系统** — 3 档位影响语气温度（熟悉/亲密/热恋）
- **人设同步** — 自动读取 MaiBot 主程序的人格配置，日程生成符合麦麦性格

---

## 快速开始

### 1. 安装

将 `mai_lover` 目录放入 MaiBot 的 `plugins/` 目录：

```
MaiBot/
└── plugins/
    └── mai_lover/
```

### 2. 配置白名单

编辑 `config.toml` 或在 MaiBot WebUI 中修改，将 `target_qq` 改为你的 QQ 号：

```toml
[whitelist]
target_qq = 2335260621  # ← 改成你的 QQ 号
```

### 3. 配置麦麦作息（可选）

编辑 `mai_template.json`，预设麦麦工作日和周末的作息骨架。插件每天凌晨会结合人设 + 骨架 → LLM 生成当日完整活动安排。

### 4. 启动

启动 MaiBot 后插件自动加载。首次启动会立即生成今日日程（不用等到第二天凌晨）。

---

## 交互方式

### 用户命令

| 命令 | 功能 |
|------|------|
| `/mai_status` | 查看麦麦状态（好感度/今日触发/日程摘要） |
| `/mai_schedule` | 查看麦麦今日完整活动安排 |
| `/mai_affection <0\|1\|2>` | 调整好感度档位 |
| `/mai_config` | 查看插件配置摘要 |
| `/mai_help` | 列出所有可用命令 |
| `/mai_test` | 发送测试消息验证发送通道 |

### LLM 工具（planner 可调用）

| Tool | 功能 |
|------|------|
| `mai_lover_current_activity` | 查询麦麦现在在干嘛（你问"在干嘛"时 planner 会调这个） |
| `mai_lover_status` | 查看麦麦状态 |
| `mai_lover_schedule` | 查看今日完整日程 |
| `mai_lover_send_message` | 主动发一条恋人消息 |
| `mai_lover_affection` | 调整好感度 |
| `mai_lover_config` | 查看配置 |

### 扩展 API（供其他插件调用）

| API | 功能 |
|-----|------|
| `get_current_activity` | 获取麦麦当前活动 |
| `get_schedule` | 获取今日完整日程 |
| `get_affection_level` | 获取好感度档位 |

---

## 配置说明

所有配置项均可在 `config.toml` 中修改，也可通过 MaiBot WebUI 直接调整。

### 插件设置
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `plugin.enabled` | true | 插件总开关 |
| `plugin.llm_model` | planner | 生成日程用的模型 |

### 白名单
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `whitelist.target_qq` | 123456789 | 绑定的 QQ 号 |

### 调度设置
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `schedule.generate_hour` | 3 | 每天几点生成日程 (0-23) |
| `schedule.check_interval_minutes` | 5 | 巡检间隔（分钟） |
| `schedule.daily_max_speak` | 5 | 每日主动触发上限 |
| `schedule.user_cooldown_minutes` | 5 | 用户发言后冷却（分钟） |
| `schedule.proactive_trigger_enabled` | true | 麦麦会不会主动找你 |

### 概率设置
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `probability.default_speak_rate` | 0.6 | 日常主动找你说话的概率 |
| `probability.miss_speak_rate` | 0.8 | 想念触发概率 |
| `probability.activity_trigger_rate` | 0.6 | 日程节点到点分享的概率 |

### 时间窗口
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `time_windows.morning_start` | 06:00 | 早安窗口开始 |
| `time_windows.morning_end` | 09:00 | 早安窗口结束 |
| `time_windows.night_start` | 22:00 | 晚安窗口开始 |
| `time_windows.night_end` | 23:59 | 晚安窗口结束 |
| `time_windows.miss_trigger_hours` | 6 | 多久不理麦麦她会想你（小时） |
| `time_windows.silence_start` | 00:00 | 静默时段开始（睡觉不打扰） |
| `time_windows.silence_end` | 08:00 | 静默时段结束 |

### 好感度
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `affection.current_level` | 0 | 0=熟悉 / 1=亲密 / 2=热恋 |

---

## 自定义麦麦作息

编辑 `mai_template.json`，预设麦麦工作日和周末的作息骨架：

```json
{
    "workday": [
        {"time": "08:30", "activity": "赖床中，闹钟响了还在赖"},
        {"time": "12:00", "activity": "做午饭，顺便想想下午干嘛"},
        {"time": "16:00", "activity": "想你了，等你消息ing"}
    ],
    "weekend": [
        {"time": "10:00", "activity": "周末赖床，阳光好舒服不想起"}
    ]
}
```

**节点字段**：
- `time` — 时间（HH:MM）
- `activity` — 麦麦在这个时间正在做什么（自然口语化描述）

每天凌晨插件会读取骨架 + 主程序人设 → LLM 生成完整日程（微调时间 + 加随机活动）→ 缓存到 `schedule_cache.json`。

---

## 工作原理

```
用户问"在干嘛" → MaiBot planner → 调 mai_lover_current_activity Tool
                                    → 返回"阳台晒太阳"
                                    → planner 自然回复"在阳台晒太阳呢~"

后台 scheduler → 定时巡检 → 检查静默/早安/晚安/想念/日程节点
              → 触发 ctx.maisaka.proactive.trigger(intent, reason)
              → planner 收到触发 → 思考要不要说话
              → planner.before_request Hook 注入"麦麦正在XX"
              → planner 自主决策发什么 → 发送消息

每天凌晨 → 读取人设 + mai_template.json + 节假日
        → LLM 生成麦麦今日活动 → 缓存
```

所有与主程序的通信均通过 **MaiBot Plugin SDK**（`self.ctx.*`），不直接访问主程序内部模块。

---

## 文件结构

```
mai_lover/
├── plugin.py              # 插件入口（生命周期/Hook/Tool/Command/API）
├── config.py              # WebUI 配置模型
├── _manifest.json         # 插件清单
├── config.toml            # 用户配置
├── constants.py           # 常量（后缀池/Prompt 模板）
├── affection_manager.py   # 好感度管理
├── memory_manager.py      # 记忆管理（预留）
├── llm_service.py         # LLM 调用封装
├── message_service.py     # 消息发送 + 情绪后缀
├── holiday_service.py     # 节假日 API
├── schedule_generator.py  # 日程生成 + 活动查询
├── scheduler.py           # 调度引擎（触发 planner）
├── mai_template.json      # 麦麦作息骨架
├── data/                  # 运行时数据目录（好感度/日程缓存，自动生成）
├── tests/                 # 测试
│   ├── test_refactor.py
│   ├── test_cmd_fix.py
│   └── test_bugfixes.py
├── docs/                  # 设计文档
│   ├── refactor-prd.md
│   └── refactor-design.md
└── README.md              # 本文件
```

---

## 扩展开发

### 调用 mai_lover 的 API

其他插件可以通过 `ctx.api` 调用 mai_lover 暴露的 API：

```python
# 获取麦麦当前活动
activity = await ctx.api.call("maibot-community.mai-lover.get_current_activity")

# 获取今日日程
schedule = await ctx.api.call("maibot-community.mai-lover.get_schedule")

# 获取好感度
level = await ctx.api.call("maibot-community.mai-lover.get_affection_level")
```

### 自定义麦麦作息

编辑 `mai_template.json` 添加你想要的麦麦活动节点。活动描述越生动，planner 生成的回复越自然。

### 自定义好感度后缀

编辑 `constants.py` 的 `AFFECTION_SUFFIXES` 和 `BRACKET_THEATERS`，调整各档位的语气词。

---

## 常见问题

**Q: 为什么麦麦不主动找我？**
A: 检查：① `target_qq` 是否正确；② 是否在静默时段；③ `daily_max_speak` 是否用完；④ `proactive_trigger_enabled` 是否开启；⑤ 是否在冷却期内。

**Q: 麦麦会半夜发消息吵醒我吗？**
A: 不会。默认静默时段 00:00~08:00，这段时间麦麦完全安静。可在 `config.toml` 的 `time_windows.silence_start/silence_end` 修改。

**Q: 如何让麦麦更黏人？**
A: 调高 `probability.default_speak_rate` 和 `activity_trigger_rate`，降低 `schedule.check_interval_minutes`，增加 `schedule.daily_max_speak`。

**Q: 麦麦的人设从哪来？**
A: 自动读取 MaiBot 主程序的 `personality.personality` 配置。在 MaiBot WebUI 的"人格"设置里改，麦麦的日程生成会自动适配。

**Q: "在干嘛"是怎么回复的？**
A: 你问"在干嘛"时，MaiBot 的 planner 会自主决定调用 `mai_lover_current_activity` Tool 查询麦麦当前活动，然后基于结果自然回复。不是固定模板。

---

## 依赖

- **Python** ≥ 3.10
- **MaiBot Plugin SDK** ≥ 2.5.4
- **httpx** — 节假日 API
- **pydantic** — 配置模型（随 SDK 安装）

---

## 许可证

MIT
