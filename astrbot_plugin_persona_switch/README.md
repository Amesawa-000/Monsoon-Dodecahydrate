# astrbot_plugin_persona_switch（人格切换）

给 AstrBot 增加一个 `/persona` 指令：查看人格列表、按名称或序号切换人格、一键恢复默认人格。适用于 QQ（aiocqhttp / OneBot v11）、QQ 官方接口、Telegram、Discord、WebChat 等所有平台。

## 功能

- `/persona`：列出所有人格，并标出当前人格
- `/persona 猫娘`：按名称切换人格（支持序号 `/persona 2`、前缀匹配 `/persona 猫`）
- `/persona reset`：清除当前对话的人格设置，恢复默认人格
- 切换只影响**当前对话**，不会影响其他群聊或其他对话
- 结果写入 AstrBot 数据库，重启后依然生效；`/new` 新建对话时会继承当前人格（AstrBot 4.3.3+）
- 可选：仅管理员可用、列表显示提示词摘要、限制列表显示数量

## 安装

### 方式一：WebUI 上传（推荐）

1. 打开 AstrBot WebUI →「插件」→「安装插件」→ 上传 `astrbot_plugin_persona_switch.zip`
2. 安装完成后在插件列表确认「人格切换」已启用
3. 如果上传的是文件夹，请把整个 `astrbot_plugin_persona_switch` 目录放到 `AstrBot/data/plugins/` 下，然后点插件卡片上的刷新/重载

### 方式二：手动安装

```text
AstrBot/
└── data/
    └── plugins/
        └── astrbot_plugin_persona_switch/
            ├── main.py
            ├── metadata.yaml
            ├── _conf_schema.json
            └── README.md
```

放好后在 WebUI 插件页重载插件即可。

## 使用

先在 WebUI 的「人格」页面创建人格，人格名称（persona_id）就是指令里使用的名称，例如「猫娘」「傲娇女仆」。

```text
/persona                 # 查看人格列表和当前人格
/persona 猫娘            # 切换到「猫娘」
/persona 2               # 按列表序号切换
/persona 猫              # 前缀匹配（唯一时生效，多个匹配会提示）
/persona reset           # 恢复默认人格
```

列表效果示例：

```text
🎭 人格列表
当前人格：猫娘

1. default（系统默认人格）
2. 猫娘（当前）
3. 傲娇女仆

发送 /persona 名称 或 /persona 序号 切换
发送 /persona reset 恢复默认人格
```

指令别名：`人格`、`切换人格`，例如 `/人格 猫娘`。唤醒前缀以 AstrBot 全局配置（默认 `/`）为准。

## 配置

在 WebUI 插件页点击「人格切换」的配置按钮即可修改，对应 `_conf_schema.json`：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `admin_only` | bool | `false` | 仅 AstrBot 管理员（`admins_id`）可使用。群聊建议开启 |
| `show_prompt_preview` | bool | `false` | 列表中显示人格提示词的前 30 个字符 |
| `max_list_items` | int | `30` | 列表最多显示多少个人格（1-100） |

## 工作原理

AstrBot 4.x 把「当前对话使用的人格」保存在 `conversation.persona_id` 中（数据库 `conversations` 表）。插件通过 `context.conversation_manager` 读取和更新这个字段：

- 如果当前还没有对话（例如新会话的第一条消息就是 `/persona`），插件会先创建一个空对话再写入人格，不会丢失后续聊天
- 读取顺序与 AstrBot 保持一致：WebUI 会话强制规则 > 当前对话的 `persona_id` > 全局默认人格
- 因此，如果在 WebUI「会话管理」里给某个会话强制指定了人格，指令切换会被该规则覆盖，插件会在回复中提示

## 注意事项

- **不会清空对话历史**：切换人格只改变 system prompt，之前的上下文仍然保留。如果想要全新开局，切换后发送 `/reset`（AstrBot 4.28+ 的 `/reset` 会新建对话并继承当前人格）或 `/new`
- **群聊权限**：默认任何群成员都能切换本群人格。如果不想被随意切换，请在插件配置里打开 `admin_only`
- **人格名称变更**：在 WebUI 重命名人格后，原来对话里记录的旧名称可能失效，重新发送 `/persona 新名称` 即可
- **保留关键字**：`reset`、`default`、`list`、`help`、`重置`、`默认`、`列表` 等被人格占用时无法按名称选择，请改用序号
- **指令冲突**：AstrBot 4.x 没有内置 `/persona` 指令；若安装了其他占用同名指令的插件，请停用其中一个

## 兼容性

- 需要 AstrBot **4.23.0 及以上**（`metadata.yaml` 中已声明 `astrbot_version: ">=4.23.0"`）
  - 4.14–4.22：官方自带 `/persona` 指令，**不需要也不建议安装本插件**（会出现两个同名指令、回复两次）
  - 4.23 起：官方移除了 `/persona`，只能到 WebUI 里切换，本插件就是为这个版本区间补回指令
  - 4.14 以下：Agent 不读取对话级人格，切换不会生效，因此不支持
- 4.20 起 AstrBot 支持「会话管理」中的强制人格规则，本插件会自动识别并在回复中提示
- 已在 AstrBot 4.29.3 的源码接口上核对，并对人格列表、序号切换、名称切换、重置、权限、无对话等场景做了 36 项单元测试
- 测试脚本：`python tests/test_persona_switch.py`（使用假的 astrbot API，无需安装 AstrBot）

## 许可

MIT
