# 更新日志


---

## v2.0.0 (2026-07-14)

### 新功能

- **NAI 生图测试面板**：新增独立 Web 面板（`pages/test-panel/`），可在插件管理页直接在线调试生图参数，生成的图片在网页内直接预览，不发送至聊天
  - **双提示词输入**：支持「NAI 风格提示词」+「自然语言提示词」两个输入框，后端自动调用插件配置的转译模型将自然语言转为 NAI 标签，再与风格提示词合并为完整 prompt 发送给站点
  - **合并步骤展示**：生成完成后在结果区展示提示词转译与合并的完整步骤，方便用户理解最终 prompt 的构成
  - **面板状态缓存**：用户在面板中的所有输入和参数选择均通过 `localStorage` 缓存，下次进入面板时自动恢复到上次退出时的状态
  - **面板独立于主配置**：面板生图不合并 Settings 中的角色预设和模板，确保测试参数纯净可控

- **试用生成功能**：面板内置「试用生成」按钮，使用代码内 XOR 混淆的公益密钥，每台设备限用 3 次
  - 密钥以 XOR 加密 + base64 编码形式存储于代码中，非明文放置
  - 试用次数通过本地文件持久化追踪，达上限后自动禁用
  - 特别感谢 [@啊·羽绒服](https://github.com/) 的免费试用额度的密钥分享

### Bug 修复

- **修复横图生成为竖图的问题**：API 站点 `nai.sta1n.cn` 期望接收中文尺寸值（`横图`/`竖图`/`方图`），但前端 `<option>` 传递的是英文值（`landscape`/`portrait`/`square`），且后端 `_resolve_size` 会将中文映射为英文，导致尺寸参数始终被误解。修复方案：前端 `option value` 直接使用中文值，后端移除 `_resolve_size` 映射，直接透传中文 `size` 参数并 URL 编码
- **修复连续生图返回相同图片**：API 调用中 `nocache=0` 导致站点命中缓存，同一提示词多次生成返回同一张图。已将 `_generate_one` 和 `_generate_one_custom` 中的 `nocache` 改为 `1`，并在多图生成时循环独立调用，确保每张图独立生成
- **修复 `scale`/`cfg` 浮点数被截断为整数**：面板参数解析使用 `int()` 导致用户输入的小数（如 `7.5`）被截断。新增 `_opt_float` 辅助函数，`scale` 和 `cfg` 正确按浮点数解析
- **修复面板生图被 Settings 角色预设污染**：`_generate_one_custom` 在面板调用时未显式覆盖 `character_preset` 和 `enable_template`，导致 Settings 中的角色预设和模板被合并进面板生图请求。已在 `_test_panel_generate` 和 `_test_panel_trial_generate` 中显式传 `character_preset=""`、`enable_template=False`，确保面板参数纯净
- **修复页面缓存失效（iframe sandbox 限制）**：面板嵌入在 `allow-scripts` 但无 `allow-same-origin` 的 iframe 中，`localStorage` 不可用导致缓存静默失败。改为后端 API 缓存方案，新增 `/save_cache` 和 `/load_cache` 两个端点，通过 Bridge SDK 读写持久化文件
- **修复上游 API 错误信息不可读**：此前 API 非 200 响应只返回泛化 `http_4xx`，用户无法定位问题。现在 `_generate_one_custom` 会读取上游响应体并截取前 300 字符记录日志，`_format_generate_error` 解析并展示具体错误原因
- **修复 `IndentationError: unexpected indent`（main.py:1536）**：`n = max(1, min(6, n))` 被误置于 `except` 块下方且多了一层缩进，导致插件导入失败。已将其移回 `try` 块内正确缩进位置
- **修复 Bridge SDK 加载时序问题**：前端脚本执行时 `window.AstrBotPluginPage` 可能尚未就绪，新增 5 秒超时重试机制确保 SDK 初始化完成

### 变更

- **默认参数调整**：`steps` 默认值从 40 改为 24，`cfg` 默认值从 0 改为 7
- **移除高级选项**：测试面板不再包含高级选项卡片，参数区更简洁

### 技术细节

- 注册 4 个 Web API 端点（`/config`、`/generate`、`/trial_status`、`/trial_generate`），路由前缀为 `/{plugin_name}/test_panel`
- 前端使用 AstrBot Bridge SDK（`window.AstrBotPluginPage`）进行前后端通信
- UI 遵循 SaaS 风格设计规范，支持主题自适应和响应式布局

---

## v1.3.1 (2026-07-12)

### Bug 修复

- **修复热重载后 `on_decorating_result` 钩子报错**：删除了空实现的 `auto_generate_for_companion` 方法，该方法注册了消息发送前钩子但什么都不做，在插件热重载时可能触发 AstrBot 框架的 `functools.partial` 叠加 bug，导致 `takes 2 positional arguments but 3 were given` 报错
- **修复 `_check_status` 误判上游可用性**：之前只要 HTTP 请求不抛异常就判定"可用"，不检查状态码。现在 404/503 等非 200 状态码会正确判定为"不可用"
- **修复代理接口 `n` 参数缺少异常防护**：客户端传非法值（如 `"n": "abc"`）时不再抛 500，而是回退到默认值 1
- **修复 debug 日志泄露明文 token**：`_generate_one` 中打印请求 URL 时，token 会被替换为 `***`，避免用户开 debug 排障时密钥泄露到日志文件

### 代码清理

- 删除从未被调用的死代码 `_save_companion_image` 方法
- 移除方法内部重复的 `from urllib.parse import quote`，统一提到文件顶部
- 移除 `initialize` 循环体内多余的 `import asyncio as _asyncio`，直接使用顶部已导入的 `asyncio`

---

## v1.3.0 (2026-07-09)

新增工具：`NAI_Generate_Image`.  
现在agent可以自主生成图片。

## v1.2.0（2026-07-07）

新增 ：**服装缓存池**。

之前你写"她今天穿了便装出门"或者"穿搭"这种笼统描述的时候，AI 转译不知道她到底穿什么，只能瞎编。现在不用瞎编了——插件记住你上次提到的具体服装，下次还是用那套。

### 用户能看到的变化

- 设置面板里多了两个新选项：
  - **默认服装**：角色一直穿的那套，比如现代风 + 绿色针织衫 + 靴子这种 tag
  - **缓存池持续时间**：缓存多久后失效，默认 1 小时，填 0 就关掉
- 角色预设那个输入框重写——现在只放角色名 / 身体特征（`muelsyse(Arknights)` ，`mechanical headwear`这种），跟服装分开了
- 转译时如果你的 prompt 提到了具体衣服，插件会"记住"那套，下次生图接着用

### 缓存池工作模式

1. 你 prompt 里说"穿了红色汉服" → 插件记一笔"红色汉服"，从现在开始 1 小时内有效
2. 下次你说"她今天出门的穿搭"（没说具体）→ 插件看一眼缓存，发现"红色汉服"还在，就把它交给 AI 翻译
3. 1 小时后自动失效，回退到默认服装


## v1.1.0（2026-07-07）

加了个**提示词转译中间层**：本来你给的是自然语言描述，插件直接发给 NAI；现在中间多一步，先让一个小模型把这描述翻译成 SD/NAI 标签风格，再发出去。

### 用户能看到的变化

- 设置面板多了两个开关：
  - **是否开启中间模型转译**（建议用便宜的小模型）
  - **转译用的模型供应商**（下拉选择，留空走默认）
- 关掉之后插件行为跟以前一模一样
- 翻译失败会自动用原文发出去，不会卡住

### 翻译规则

针对那种很啰嗦、很具体的自然语言 prompt，让转译模型老老实实输出 SD tags：禁止重复质量词、禁止幻觉细节、禁止堆同义词、禁止把"1:1 方形"塞进 prompt（那部分在尺寸参数里），输出 tag 限 25-40 个、加权语法示范到位。

---

## v1.0.0

最初始发布的版本：
- 走 `nai.sta1n.cn` 出图
- 起一个本地 8765 端口的代理，OpenAI Images API 兼容格式，陪伴插件转过来就能用
- `/image`、`/quota`、`/imgstatus` 三个指令
- 7 种风格、3 种比例
- 各种参数都能在设置面板调：模型、步数、scale、cfg、采样器、negative、artist 模板、代理端口

> ⚠️ 已知的小毛病：bot 主动触发生图的时候偶尔发不出来，作者还在排查，先标记一下。
